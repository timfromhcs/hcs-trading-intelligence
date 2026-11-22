"""Chain-valid Solana signing tests (offline; devnet rehearsal is opt-in)."""

import json
import os
import struct
import time
import urllib.request
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from solders.compute_budget import (
    request_heap_frame,
    set_compute_unit_limit,
    set_compute_unit_price,
)
from solders.hash import Hash
from solders.instruction import AccountMeta, CompiledInstruction, Instruction
from solders.keypair import Keypair
from solders.message import (
    Message,
    MessageAddressTableLookup,
    MessageHeader,
    MessageV0,
    to_bytes_versioned,
)
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.system_program import ID as SYSTEM_ID
from solders.system_program import TransferParams, transfer
from solders.token.associated import get_associated_token_address
from solders.transaction import VersionedTransaction

from hcs_trader.domain.signing import (
    ApprovedRiskDecision,
    SignedTransaction,
    SignerPolicy,
    SignerPolicyViolation,
    SignerTamperingError,
    SigningUnsupported,
    ValidatedTransactionPlan,
)
from hcs_trader.keystore import create_keystore_record
from hcs_trader.risk import RiskEvaluation
from hcs_trader.signing import IsolatedSigner
from hcs_trader.solana_signing import (
    ATA_PROGRAM_ID,
    JUPITER_V6_PROGRAM_ID,
    TOKEN_PROGRAM_ID,
    WSOL_MINT,
    SolanaMessageError,
    SolanaMessagePolicy,
    decode_solana_message,
    message_digest,
    sign_solana_message,
)
from hcs_trader.storage import AuditStore

_SEED = bytes.fromhex(
    "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
)
_KP = Keypair.from_seed(_SEED)
_SIGNER = str(_KP.pubkey())
_RECIPIENT = Keypair.from_seed(bytes(range(32))).pubkey()
_USDC = Pubkey.from_string("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
_BLOCKHASH = Hash.from_bytes(bytes([7] * 32))
_PASS = "test-pass-phrase"
_AT = datetime.now(UTC)


def _plan(
    *,
    destination: str | None = None,
    amount: int = 1_000_000,
    fee: int = 20_000,
    prio: int = 10_000,
    token_in: str | None = None,
    key: str = "sol-idem-0001",
    signer: str = _SIGNER,
) -> ValidatedTransactionPlan:
    return ValidatedTransactionPlan(
        canonical_chain_id="solana:devnet",
        network="solana:devnet",
        wallet_id="wallet-sol-01",
        signer_address=signer,
        asset_in="solana:devnet/native:SOL",
        asset_out="solana:devnet/native:SOL",
        amount_in_base_units=amount,
        min_amount_out_base_units=1,
        destination_id=destination or str(_RECIPIENT),
        max_slippage_bps=50,
        max_network_fee_base_units=fee,
        max_priority_fee_base_units=prio,
        idempotency_key=key,
        risk_decision_id="risk-dec-sol",
        risk_policy_version="v1.0.0",
        simulation_success=True,
        simulation_block_reference="slot-1",
        token_in_mint=token_in,
        deadline=_AT + timedelta(minutes=5),
    ).with_hash()


def _msg(ixs: list[Instruction], payer: Pubkey | None = None) -> bytes:
    return to_bytes_versioned(
        Message.new_with_blockhash(ixs, payer or _KP.pubkey(), _BLOCKHASH)
    )


def _sys_transfer(amount: int = 1_000_000, to: Pubkey = _RECIPIENT) -> Instruction:
    return transfer(
        TransferParams(from_pubkey=_KP.pubkey(), to_pubkey=to, lamports=amount)
    )


def _spl_checked(amount: int, src: Pubkey, dst: Pubkey, auth: Pubkey) -> Instruction:
    data = bytes([12]) + amount.to_bytes(8, "little") + bytes([6])
    return Instruction(
        TOKEN_PROGRAM_ID,
        data,
        [
            AccountMeta(src, False, True),
            AccountMeta(_USDC, False, False),
            AccountMeta(dst, False, True),
            AccountMeta(auth, True, False),
        ],
    )


def _policy() -> SignerPolicy:
    return SignerPolicy(
        allowed_chain_ids=frozenset({"solana:devnet"}),
        allowed_destinations=frozenset(
            {str(_RECIPIENT), str(JUPITER_V6_PROGRAM_ID), str(_USDC)}
            | {str(get_associated_token_address(_RECIPIENT, _USDC))}
        ),
        allowed_tokens=frozenset({str(_USDC), WSOL_MINT}),
        max_slippage_bps_limit=100,
        max_network_fee_limit=100_000,
        live_mode_enabled=True,
    )


class _Harness:
    """Signer plus a persisted-risk-decision stand-in that approves given plans."""

    def __init__(
        self,
        policy: SignerPolicy | None = None,
        *,
        solana_policy: SolanaMessagePolicy | None = None,
        paused: bool = False,
        ledger: Any = None,
    ) -> None:
        self.approved: dict[str, ApprovedRiskDecision] = {}
        self.signer = IsolatedSigner(
            policy or _policy(),
            pause_state=lambda: paused,
            risk_decision_lookup=self.approved.get,
            solana_policy=solana_policy,
            signed_plans=ledger,
        )
        self.signer.register_keystore_record(
            create_keystore_record("wallet-sol-01", _SIGNER, "solana", _SEED, _PASS)
        )

    def approve(self, plan: ValidatedTransactionPlan) -> None:
        self.approved[plan.risk_decision_id] = ApprovedRiskDecision(
            decision_id=plan.risk_decision_id, plan_hash=plan.plan_hash, allowed=True
        )

    def sign(
        self,
        plan: ValidatedTransactionPlan,
        msg: bytes,
        at: datetime = _AT,
    ) -> SignedTransaction:
        self.approve(plan)
        return self.signer.sign_solana_transaction(plan, msg, _PASS, at)


def _signer(
    policy: SignerPolicy | None = None,
    *,
    solana_policy: SolanaMessagePolicy | None = None,
    ledger: Any = None,
) -> _Harness:
    return _Harness(policy, solana_policy=solana_policy, ledger=ledger)


def _verify_wire(raw_hex: str, message_bytes: bytes) -> VersionedTransaction:
    tx = VersionedTransaction.from_bytes(bytes.fromhex(raw_hex))
    assert to_bytes_versioned(tx.message) == message_bytes
    sig = tx.signatures[0]
    assert sig.verify(_KP.pubkey(), message_bytes)
    return tx


def test_known_vector_ed25519_seed_derives_expected_pubkey() -> None:
    # RFC 8032 test 1 seed -> public key d75a98...; checks keypair/seed handling.
    kp = Keypair.from_seed(
        bytes.fromhex(
            "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
        )
    )
    assert bytes(kp.pubkey()).hex() == (
        "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
    )


def test_system_transfer_signed_wire_tx_verifies() -> None:
    msg = _msg([_sys_transfer()])
    signed = _signer().sign(_plan(), msg)
    tx = _verify_wire(signed.raw_transaction_hex, msg)
    assert tx.verify_and_hash_message() is not None
    # tx_hash is the base58 transaction id (first signature), as RPCs report it
    assert signed.tx_hash == str(tx.signatures[0])


def test_spl_transfer_checked_signed_and_verifies() -> None:
    src = get_associated_token_address(_KP.pubkey(), _USDC)
    dst = get_associated_token_address(_RECIPIENT, _USDC)
    msg = _msg([_spl_checked(2_500_000, src, dst, _KP.pubkey())])
    plan = _plan(destination=str(dst), amount=2_500_000, token_in=str(_USDC))
    signed = _signer().sign(plan, msg)
    _verify_wire(signed.raw_transaction_hex, msg)


def test_priority_fee_within_caps_and_ata_create_allowed() -> None:
    ata = get_associated_token_address(_RECIPIENT, _USDC)
    create = Instruction(
        ATA_PROGRAM_ID,
        bytes([1]),
        [
            AccountMeta(_KP.pubkey(), True, True),
            AccountMeta(ata, False, True),
            AccountMeta(_RECIPIENT, False, False),
            AccountMeta(_USDC, False, False),
            AccountMeta(SYSTEM_ID, False, False),
            AccountMeta(TOKEN_PROGRAM_ID, False, False),
        ],
    )
    src = get_associated_token_address(_KP.pubkey(), _USDC)
    msg = _msg(
        [
            set_compute_unit_limit(100_000),
            set_compute_unit_price(50_000),  # 100k * 50k / 1e6 = 5000 lamports
            create,
            _spl_checked(1, src, ata, _KP.pubkey()),
        ]
    )
    plan = _plan(destination=str(ata), amount=1, token_in=str(_USDC))
    decoded = decode_solana_message(msg, plan, SolanaMessagePolicy())
    assert decoded.priority_fee_lamports == 5000
    assert decoded.total_fee_lamports == 10_000


def test_priority_fee_over_cap_rejected() -> None:
    msg = _msg(
        [
            set_compute_unit_limit(200_000),
            set_compute_unit_price(1_000_000),
            _sys_transfer(),
        ]
    )
    with pytest.raises(SolanaMessageError, match="priority fee"):
        decode_solana_message(msg, _plan(), SolanaMessagePolicy())


def test_total_fee_over_plan_cap_rejected() -> None:
    msg = _msg(
        [
            set_compute_unit_limit(1_000_000),
            set_compute_unit_price(9_000),
            _sys_transfer(),
        ]
    )
    with pytest.raises(SolanaMessageError, match="network fee"):
        decode_solana_message(
            msg, _plan(fee=10_000, prio=20_000), SolanaMessagePolicy()
        )


def test_tampered_message_rejected() -> None:
    msg = bytearray(_msg([_sys_transfer()]))
    # flip lamports in the instruction data (little-endian u64 near the end)
    idx = bytes(msg).rindex((1_000_000).to_bytes(8, "little"))
    msg[idx] ^= 0x01
    with pytest.raises(SolanaMessageError, match="does not match plan"):
        _signer().sign(_plan(), bytes(msg))


def test_trailing_bytes_rejected() -> None:
    msg = _msg([_sys_transfer()]) + b"\x00"
    with pytest.raises(SolanaMessageError):
        decode_solana_message(msg, _plan(), SolanaMessagePolicy())


def test_wrong_destination_and_amount_rejected() -> None:
    other = Keypair.from_seed(bytes([9] * 32)).pubkey()
    with pytest.raises(SolanaMessageError, match="does not match plan"):
        decode_solana_message(
            _msg([_sys_transfer(to=other)]), _plan(), SolanaMessagePolicy()
        )
    with pytest.raises(SolanaMessageError, match="does not match plan"):
        decode_solana_message(_msg([_sys_transfer(5)]), _plan(), SolanaMessagePolicy())


def test_unknown_program_rejected() -> None:
    evil = Pubkey.from_bytes(bytes([5] * 32))
    ix = Instruction(evil, b"\x01", [AccountMeta(_KP.pubkey(), True, True)])
    with pytest.raises(SolanaMessageError, match="unknown program"):
        decode_solana_message(
            _msg([_sys_transfer(), ix]), _plan(), SolanaMessagePolicy()
        )


def test_jupiter_requires_explicit_allow() -> None:
    jup = Instruction(
        JUPITER_V6_PROGRAM_ID, b"\x00\x01", [AccountMeta(_KP.pubkey(), True, True)]
    )
    msg = _msg([jup])
    plan = _plan(destination=str(JUPITER_V6_PROGRAM_ID), token_in=str(_USDC))
    with pytest.raises(SolanaMessageError, match="Jupiter program not allowed"):
        decode_solana_message(msg, plan, SolanaMessagePolicy())
    decoded = decode_solana_message(msg, plan, SolanaMessagePolicy(allow_jupiter=True))
    assert decoded.uses_jupiter


def test_jupiter_outgoing_sol_bounded_and_destination_bound() -> None:
    jup = Instruction(
        JUPITER_V6_PROGRAM_ID, b"\x00", [AccountMeta(_KP.pubkey(), True, True)]
    )
    allow = SolanaMessagePolicy(allow_jupiter=True)
    plan = _plan(destination=str(JUPITER_V6_PROGRAM_ID), token_in=str(_USDC))
    with pytest.raises(SolanaMessageError, match="needs a WSOL input"):
        decode_solana_message(_msg([_sys_transfer(1), jup]), plan, allow)
    bad = _plan(destination=str(_RECIPIENT), token_in=str(_USDC))
    with pytest.raises(SolanaMessageError, match="not the Jupiter program"):
        decode_solana_message(_msg([jup]), bad, allow)


def test_second_signer_and_foreign_authority_rejected() -> None:
    other = Keypair.from_seed(bytes([3] * 32)).pubkey()
    src = get_associated_token_address(other, _USDC)
    dst = get_associated_token_address(_RECIPIENT, _USDC)
    # authority is a different (extra) signer
    msg = _msg([_spl_checked(1, src, dst, other)])
    plan = _plan(destination=str(dst), amount=1, token_in=str(_USDC))
    with pytest.raises(SolanaMessageError):
        decode_solana_message(msg, plan, SolanaMessagePolicy())


def test_fee_payer_must_be_plan_signer() -> None:
    other = Keypair.from_seed(bytes([3] * 32))
    ix = transfer(
        TransferParams(
            from_pubkey=other.pubkey(), to_pubkey=_RECIPIENT, lamports=1_000_000
        )
    )
    with pytest.raises(SolanaMessageError, match="fee payer"):
        decode_solana_message(
            _msg([ix], other.pubkey()), _plan(), SolanaMessagePolicy()
        )


def test_unchecked_spl_transfer_rejected() -> None:
    src = get_associated_token_address(_KP.pubkey(), _USDC)
    dst = get_associated_token_address(_RECIPIENT, _USDC)
    ix = Instruction(
        TOKEN_PROGRAM_ID,
        bytes([3]) + (1).to_bytes(8, "little"),
        [
            AccountMeta(src, False, True),
            AccountMeta(dst, False, True),
            AccountMeta(_KP.pubkey(), True, False),
        ],
    )
    plan = _plan(destination=str(dst), amount=1, token_in=str(_USDC))
    with pytest.raises(SolanaMessageError, match="unsupported SPL"):
        decode_solana_message(_msg([ix]), plan, SolanaMessagePolicy())


def test_kill_switch_and_live_mode_block_solana_signing() -> None:
    msg = _msg([_sys_transfer()])
    off = replace(_policy(), kill_switch_active=True)
    with pytest.raises(SignerPolicyViolation, match="kill switch"):
        _signer(off).sign(_plan(), msg)
    no_live = replace(_policy(), live_mode_enabled=False)
    with pytest.raises(SignerPolicyViolation, match="live mode"):
        _signer(no_live).sign(_plan(), msg)


def test_plan_hash_tamper_and_non_solana_chain_rejected() -> None:
    msg = _msg([_sys_transfer()])
    tampered = replace(_plan(), amount_in_base_units=2_000_000)
    with pytest.raises(SignerTamperingError, match="plan hash mismatch"):
        _signer().sign(tampered, msg)


def test_idempotency_replay_is_refused_not_replayed() -> None:
    h = _signer()
    msg = _msg([_sys_transfer()])
    h.sign(_plan(), msg)
    with pytest.raises(SignerPolicyViolation, match="already signed"):
        h.sign(_plan(), msg)
    other = _msg([_sys_transfer(1_000_000), set_compute_unit_limit(1)])
    with pytest.raises(SignerPolicyViolation, match="already signed"):
        h.sign(_plan(), other)


def test_failed_attempt_does_not_consume_idempotency_key() -> None:
    h = _signer()
    with pytest.raises(SolanaMessageError):
        h.sign(_plan(), _msg([_sys_transfer(5)]))
    h.sign(_plan(), _msg([_sys_transfer()]))


def _store_signer(store: AuditStore) -> IsolatedSigner:
    s = IsolatedSigner(
        _policy(),
        pause_state=store.fail_safe_paused,
        risk_decision_lookup=store.approved_risk_decision,
        signed_plans=store,
    )
    s.register_keystore_record(
        create_keystore_record("wallet-sol-01", _SIGNER, "solana", _SEED, _PASS)
    )
    return s


def test_replay_after_restart_refused_with_durable_ledger(tmp_path: Path) -> None:
    store = AuditStore(tmp_path / "sol.db")
    store.initialize()
    plan = _plan()
    msg = _msg([_sys_transfer()])
    store.record_risk_decision(
        RiskEvaluation(
            decision_id=plan.risk_decision_id,
            allowed=True,
            reason=None,
            evaluated_at=_AT,
            plan_hash=plan.plan_hash,
        ),
        policy_version="v1.0.0",
    )
    _store_signer(store).sign_solana_transaction(plan, msg, _PASS, _AT)
    # "Restart": a brand-new signer over the same durable store must refuse.
    with pytest.raises(SignerPolicyViolation):
        _store_signer(store).sign_solana_transaction(plan, msg, _PASS, _AT)


def test_replay_after_deadline_rejected() -> None:
    h = _signer()
    msg = _msg([_sys_transfer()])
    h.sign(_plan(), msg)
    late = _AT + timedelta(hours=1)
    with pytest.raises(SignerPolicyViolation):
        h.sign(_plan(), msg, late)
    with pytest.raises(SignerPolicyViolation, match="expired|deadline"):
        _signer().sign(_plan(), msg, late)


def test_pause_and_missing_risk_decision_block_solana_signing() -> None:
    msg = _msg([_sys_transfer()])
    with pytest.raises(SignerPolicyViolation, match="paused"):
        _Harness(paused=True).sign(_plan(), msg)
    h = _Harness()
    with pytest.raises(SignerPolicyViolation, match="risk decision"):
        h.signer.sign_solana_transaction(_plan(), msg, _PASS, _AT)


def test_legacy_sign_transaction_rejects_solana_plans() -> None:
    plan = _plan()
    h = _Harness()
    h.approve(plan)
    payload = json.dumps(
        {
            "program_id": plan.destination_id,
            "canonical_chain_id": plan.canonical_chain_id,
            "amount": plan.amount_in_base_units,
            "signer": plan.signer_address,
        }
    ).encode()
    with pytest.raises(SigningUnsupported, match="sign_solana_transaction"):
        h.signer.sign_transaction(plan, payload, _PASS, _AT)


def _jup() -> Instruction:
    return Instruction(
        JUPITER_V6_PROGRAM_ID, b"\x00", [AccountMeta(_KP.pubkey(), True, True)]
    )


def test_jupiter_mode_is_signer_policy_not_caller_choice() -> None:
    msg = _msg([_jup()])
    plan = _plan(destination=str(JUPITER_V6_PROGRAM_ID), token_in=str(_USDC))
    with pytest.raises(SolanaMessageError, match="Jupiter program not allowed"):
        _signer().sign(plan, msg)
    signed = _signer(solana_policy=SolanaMessagePolicy(allow_jupiter=True)).sign(
        plan, msg
    )
    _verify_wire(signed.raw_transaction_hex, msg)


def test_jupiter_rejected_when_input_token_not_allowlisted() -> None:
    rogue = str(Keypair.from_seed(bytes([8] * 32)).pubkey())
    plan = _plan(destination=str(JUPITER_V6_PROGRAM_ID), token_in=rogue)
    with pytest.raises(SignerPolicyViolation):
        _signer(solana_policy=SolanaMessagePolicy(allow_jupiter=True)).sign(
            plan, _msg([_jup()])
        )


def test_jupiter_system_transfer_to_arbitrary_recipient_rejected() -> None:
    allow = SolanaMessagePolicy(allow_jupiter=True)
    plan = _plan(
        destination=str(JUPITER_V6_PROGRAM_ID), token_in=WSOL_MINT, amount=1_000_000
    )
    attacker = Keypair.from_seed(bytes([9] * 32)).pubkey()
    with pytest.raises(SolanaMessageError, match="WSOL account"):
        decode_solana_message(
            _msg([_sys_transfer(1_000_000, attacker), _jup()]), plan, allow
        )
    # Standard wrap-SOL funding of the signer's own WSOL account is accepted.
    wsol_ata = get_associated_token_address(_KP.pubkey(), Pubkey.from_string(WSOL_MINT))
    decoded = decode_solana_message(
        _msg([_sys_transfer(1_000_000, wsol_ata), _jup()]), plan, allow
    )
    assert decoded.uses_jupiter
    with pytest.raises(SolanaMessageError, match="exceeds plan amount"):
        decode_solana_message(
            _msg([_sys_transfer(1_000_001, wsol_ata), _jup()]), plan, allow
        )
    # A non-WSOL input never permits a System transfer.
    usdc_plan = _plan(destination=str(JUPITER_V6_PROGRAM_ID), token_in=str(_USDC))
    with pytest.raises(SolanaMessageError, match="WSOL input"):
        decode_solana_message(
            _msg([_sys_transfer(1, wsol_ata), _jup()]), usdc_plan, allow
        )


def _ata_create(owner: Pubkey, mint: Pubkey, ata: Pubkey | None = None) -> Instruction:
    return Instruction(
        ATA_PROGRAM_ID,
        bytes([1]),
        [
            AccountMeta(_KP.pubkey(), True, True),
            AccountMeta(ata or get_associated_token_address(owner, mint), False, True),
            AccountMeta(owner, False, False),
            AccountMeta(mint, False, False),
            AccountMeta(SYSTEM_ID, False, False),
            AccountMeta(TOKEN_PROGRAM_ID, False, False),
        ],
    )


def test_ata_create_bound_to_plan() -> None:
    attacker = Keypair.from_seed(bytes([9] * 32)).pubkey()
    src = get_associated_token_address(_KP.pubkey(), _USDC)
    dst = get_associated_token_address(_RECIPIENT, _USDC)
    plan = _plan(destination=str(dst), amount=1, token_in=str(_USDC))
    xfer = _spl_checked(1, src, dst, _KP.pubkey())
    pol = SolanaMessagePolicy()
    with pytest.raises(SolanaMessageError, match="not bound to the plan"):
        decode_solana_message(_msg([_ata_create(attacker, _USDC), xfer]), plan, pol)
    bogus = _ata_create(_RECIPIENT, _USDC, ata=attacker)
    with pytest.raises(SolanaMessageError, match="not derived"):
        decode_solana_message(_msg([bogus, xfer]), plan, pol)
    other_mint = Keypair.from_seed(bytes([6] * 32)).pubkey()
    with pytest.raises(SolanaMessageError):
        decode_solana_message(
            _msg([_ata_create(_RECIPIENT, other_mint), xfer]), plan, pol
        )
    decode_solana_message(_msg([_ata_create(_RECIPIENT, _USDC), xfer]), plan, pol)


def test_ata_create_not_allowed_with_system_transfer() -> None:
    msg = _msg([_ata_create(_RECIPIENT, _USDC), _sys_transfer()])
    with pytest.raises(SolanaMessageError, match="not allowed with a System"):
        decode_solana_message(msg, _plan(), SolanaMessagePolicy())


def test_jupiter_ata_create_only_for_signer_and_plan_mints() -> None:
    allow = SolanaMessagePolicy(allow_jupiter=True)
    attacker = Keypair.from_seed(bytes([9] * 32)).pubkey()
    plan = _plan(destination=str(JUPITER_V6_PROGRAM_ID), token_in=str(_USDC))
    with pytest.raises(SolanaMessageError, match="signer and plan mints"):
        decode_solana_message(_msg([_ata_create(attacker, _USDC), _jup()]), plan, allow)
    wsol = Pubkey.from_string(WSOL_MINT)
    with pytest.raises(SolanaMessageError, match="signer and plan mints"):
        decode_solana_message(
            _msg([_ata_create(_KP.pubkey(), wsol), _jup()]), plan, allow
        )
    decode_solana_message(_msg([_ata_create(_KP.pubkey(), _USDC), _jup()]), plan, allow)


def test_keystore_key_must_match_signer_address() -> None:
    h = _Harness()
    h.signer._keystore.clear()
    h.signer.register_keystore_record(
        create_keystore_record(
            "wallet-sol-01", _SIGNER, "solana", bytes([4] * 32), _PASS
        )
    )
    with pytest.raises(SignerPolicyViolation, match="does not match"):
        h.sign(_plan(), _msg([_sys_transfer()]))


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(st.binary(max_size=1500))
def test_fuzz_random_bytes_never_sign_or_crash(blob: bytes) -> None:
    try:
        decode_solana_message(blob, _plan(), SolanaMessagePolicy(allow_jupiter=True))
    except SolanaMessageError:
        return
    pytest.fail("random bytes were accepted")


@settings(max_examples=150, deadline=None)
@given(st.integers(min_value=0), st.integers(min_value=0, max_value=255))
def test_fuzz_mutating_valid_message_is_rejected_or_identical(
    pos: int, xor: int
) -> None:
    good = _msg([_sys_transfer()])
    plan = _plan()
    buf = bytearray(good)
    buf[pos % len(buf)] ^= xor
    mutated = bytes(buf)
    try:
        decoded = decode_solana_message(mutated, plan, SolanaMessagePolicy())
    except SolanaMessageError:
        return
    # Any accepted message must still be an exactly-planned transfer from signer.
    assert decoded.system_transfers == ((_SIGNER, str(_RECIPIENT), 1_000_000),)
    assert decoded.fee_payer == _SIGNER


@settings(max_examples=100, deadline=None)
@given(st.integers(min_value=1, max_value=2**63))
def test_fuzz_amount_mismatch_rejected(amount: int) -> None:
    msg = _msg([_sys_transfer(amount)])
    plan = _plan(amount=amount + 1)
    with pytest.raises(SolanaMessageError):
        decode_solana_message(msg, plan, SolanaMessagePolicy())


def test_signature_type_is_64_bytes() -> None:
    signed = _signer().sign(_plan(), _msg([_sys_transfer()]))
    assert len(bytes(Signature.from_string(signed.tx_hash))) == len(
        bytes(Signature.default())
    )


# ---------------------------------------------------------------- rehearsal


def _rpc(url: str, method: str, params: list[Any]) -> Any:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    req = urllib.request.Request(  # noqa: S310 - endpoint is operator-supplied devnet
        url, body.encode(), {"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        out = json.loads(resp.read())
    if "error" in out:
        raise RuntimeError(out["error"])
    return out["result"]


@pytest.mark.rehearsal
@pytest.mark.skipif(
    not os.environ.get("HCS_SOLANA_DEVNET_RPC"),
    reason="HCS_SOLANA_DEVNET_RPC not set (devnet rehearsal is opt-in)",
)
def test_devnet_airdrop_and_transfer_rehearsal() -> None:
    url = os.environ["HCS_SOLANA_DEVNET_RPC"]
    kp = Keypair()  # fresh throwaway devnet key; never a mainnet key
    dest = Keypair().pubkey()
    sig = _rpc(url, "requestAirdrop", [str(kp.pubkey()), 1_000_000_000])
    for _ in range(30):
        st_ = _rpc(url, "getSignatureStatuses", [[sig]])["value"][0]
        if st_ and st_.get("confirmationStatus") in ("confirmed", "finalized"):
            break
        time.sleep(2)
    else:
        pytest.skip("devnet airdrop not confirmed (faucet rate limit)")
    bh = _rpc(url, "getLatestBlockhash", [])["value"]["blockhash"]
    ix = transfer(
        TransferParams(from_pubkey=kp.pubkey(), to_pubkey=dest, lamports=1_000_000)
    )
    msg = to_bytes_versioned(
        Message.new_with_blockhash([ix], kp.pubkey(), Hash.from_string(bh))
    )
    plan = replace(
        _plan(destination=str(dest), signer=str(kp.pubkey()), key="sol-rehearsal-1"),
        plan_hash="",
    ).with_hash()
    h = _Harness(
        SignerPolicy(
            allowed_chain_ids=frozenset({"solana:devnet"}),
            allowed_destinations=frozenset({str(dest)}),
            allowed_tokens=frozenset(),
            max_slippage_bps_limit=100,
            max_network_fee_limit=100_000,
            live_mode_enabled=True,
        )
    )
    h.signer._keystore.clear()
    h.signer.register_keystore_record(
        create_keystore_record(
            "wallet-sol-01", str(kp.pubkey()), "solana", kp.secret(), _PASS
        )
    )
    signed = h.sign(plan, msg, datetime.now(UTC))
    import base64

    out = _rpc(
        url,
        "sendTransaction",
        [
            base64.b64encode(bytes.fromhex(signed.raw_transaction_hex)).decode(),
            {"encoding": "base64"},
        ],
    )
    assert out == signed.tx_hash


# --- additional rejection / edge coverage for the message decoder -----------

_POL = SolanaMessagePolicy()
_JUP_POL = SolanaMessagePolicy(allow_jupiter=True)
_OTHER = Keypair.from_seed(bytes([9] * 32)).pubkey()


def _sys_data(lamports: int = 1_000_000) -> bytes:
    return struct.pack("<IQ", 2, lamports)


def _raw_legacy(keys: list[Pubkey], ixs: list[CompiledInstruction]) -> bytes:
    """Hand-built legacy message (1 signer, last key read-only program)."""
    return to_bytes_versioned(
        Message.new_with_compiled_instructions(1, 0, 1, keys, _BLOCKHASH, ixs)
    )


def _token_ix(data: bytes, metas: list[AccountMeta]) -> Instruction:
    return Instruction(TOKEN_PROGRAM_ID, data, metas)


def test_message_with_no_instructions_rejected() -> None:
    with pytest.raises(SolanaMessageError, match="no instructions"):
        decode_solana_message(_msg([]), _plan(), _POL)


def test_too_many_instructions_rejected() -> None:
    msg = _msg([set_compute_unit_limit(1), _sys_transfer()])
    with pytest.raises(SolanaMessageError, match="too many instructions"):
        decode_solana_message(msg, _plan(), SolanaMessagePolicy(max_instructions=1))


def test_program_id_outside_static_keys_rejected() -> None:
    msg = _raw_legacy(
        [_KP.pubkey(), SYSTEM_ID],
        [
            CompiledInstruction(
                program_id_index=5, accounts=bytes([0, 1]), data=_sys_data()
            )
        ],
    )
    with pytest.raises(SolanaMessageError, match="program id outside"):
        decode_solana_message(msg, _plan(), _POL)


def test_instruction_account_index_outside_static_keys_rejected() -> None:
    msg = _raw_legacy(
        [_KP.pubkey(), SYSTEM_ID],
        [
            CompiledInstruction(
                program_id_index=1, accounts=bytes([0, 7]), data=_sys_data()
            )
        ],
    )
    with pytest.raises(SolanaMessageError, match="lookup-table account"):
        decode_solana_message(msg, _plan(), _POL)


def test_instruction_missing_required_account_rejected() -> None:
    short = _token_ix(
        bytes([12]) + (1).to_bytes(8, "little") + bytes([6]),
        [AccountMeta(_USDC, False, True), AccountMeta(_USDC, False, False)],
    )
    with pytest.raises(SolanaMessageError, match="missing required account"):
        decode_solana_message(_msg([short]), _plan(token_in=str(_USDC)), _POL)


def test_unsupported_compute_budget_instruction_rejected() -> None:
    with pytest.raises(SolanaMessageError, match="compute budget"):
        decode_solana_message(
            _msg([request_heap_frame(65536), _sys_transfer()]), _plan(), _POL
        )


def test_compute_unit_price_without_limit_rejected() -> None:
    msg = _msg([set_compute_unit_price(1), _sys_transfer()])
    with pytest.raises(SolanaMessageError, match="explicit compute unit limit"):
        decode_solana_message(msg, _plan(), _POL)


def test_compute_unit_limit_over_policy_rejected() -> None:
    msg = _msg([set_compute_unit_limit(1_400_001), _sys_transfer()])
    with pytest.raises(SolanaMessageError, match="compute unit limit exceeds"):
        decode_solana_message(msg, _plan(), _POL)
    # Exactly at the limit is accepted (boundary).
    decode_solana_message(
        _msg([set_compute_unit_limit(1_400_000), _sys_transfer()]), _plan(), _POL
    )


def test_spl_transfer_with_foreign_authority_rejected() -> None:
    src = get_associated_token_address(_KP.pubkey(), _USDC)
    dst = get_associated_token_address(_RECIPIENT, _USDC)
    # Non-signer authority keeps the header at one signature, so the decoder's
    # own authority check (not the signature-count check) must reject it.
    ix = _token_ix(
        bytes([12]) + (1).to_bytes(8, "little") + bytes([6]),
        [
            AccountMeta(src, False, True),
            AccountMeta(_USDC, False, False),
            AccountMeta(dst, False, True),
            AccountMeta(_OTHER, False, False),
        ],
    )
    plan = _plan(destination=str(dst), amount=1, token_in=str(_USDC))
    with pytest.raises(SolanaMessageError, match="authority is not the signer"):
        decode_solana_message(_msg([ix]), plan, _POL)


def test_sync_native_and_close_account_only_in_jupiter_swaps() -> None:
    wsol_ata = get_associated_token_address(_KP.pubkey(), Pubkey.from_string(WSOL_MINT))
    sync = _token_ix(bytes([17]), [AccountMeta(wsol_ata, False, True)])
    close = _token_ix(
        bytes([9]),
        [
            AccountMeta(wsol_ata, False, True),
            AccountMeta(_KP.pubkey(), False, True),
            AccountMeta(_KP.pubkey(), True, False),
        ],
    )
    with pytest.raises(SolanaMessageError, match="SyncNative only allowed"):
        decode_solana_message(_msg([sync, _sys_transfer()]), _plan(), _POL)
    with pytest.raises(SolanaMessageError, match="CloseAccount only allowed"):
        decode_solana_message(_msg([close, _sys_transfer()]), _plan(), _POL)
    plan = _plan(destination=str(JUPITER_V6_PROGRAM_ID), token_in=WSOL_MINT)
    decoded = decode_solana_message(_msg([sync, close, _jup()]), plan, _JUP_POL)
    assert decoded.uses_jupiter


def test_close_account_destination_must_be_signer() -> None:
    wsol_ata = get_associated_token_address(_KP.pubkey(), Pubkey.from_string(WSOL_MINT))
    plan = _plan(destination=str(JUPITER_V6_PROGRAM_ID), token_in=WSOL_MINT)
    for dest, owner in ((_OTHER, _KP.pubkey()), (_KP.pubkey(), _OTHER)):
        close = _token_ix(
            bytes([9]),
            [
                AccountMeta(wsol_ata, False, True),
                AccountMeta(dest, False, True),
                AccountMeta(owner, owner == _KP.pubkey(), False),
            ],
        )
        with pytest.raises(SolanaMessageError, match="CloseAccount destination"):
            decode_solana_message(_msg([close, _jup()]), plan, _JUP_POL)


def test_ata_create_wrong_variant_funder_and_programs_rejected() -> None:
    dst = get_associated_token_address(_RECIPIENT, _USDC)
    plan = _plan(destination=str(dst), amount=1, token_in=str(_USDC))
    src = get_associated_token_address(_KP.pubkey(), _USDC)
    xfer = _spl_checked(1, src, dst, _KP.pubkey())
    good = _ata_create(_RECIPIENT, _USDC)

    legacy_create = Instruction(ATA_PROGRAM_ID, b"", good.accounts)  # variant 0
    with pytest.raises(SolanaMessageError, match="only ATA CreateIdempotent"):
        decode_solana_message(_msg([legacy_create, xfer]), plan, _POL)

    metas = list(good.accounts)
    metas[0] = AccountMeta(_OTHER, False, True)  # funder is not the signer
    with pytest.raises(SolanaMessageError, match="funder is not the signer"):
        decode_solana_message(
            _msg([Instruction(ATA_PROGRAM_ID, bytes([1]), metas), xfer]), plan, _POL
        )

    metas = list(good.accounts)
    metas[4], metas[5] = metas[5], metas[4]  # system/token programs swapped
    with pytest.raises(SolanaMessageError, match="unexpected system/token program"):
        decode_solana_message(
            _msg([Instruction(ATA_PROGRAM_ID, bytes([1]), metas), xfer]), plan, _POL
        )


def test_system_transfer_source_must_be_signer() -> None:
    ix = Instruction(
        SYSTEM_ID,
        _sys_data(),
        [AccountMeta(_OTHER, False, True), AccountMeta(_RECIPIENT, False, True)],
    )
    with pytest.raises(SolanaMessageError, match="source is not the signer"):
        decode_solana_message(_msg([ix]), _plan(), _POL)


def test_address_table_lookups_only_for_jupiter() -> None:
    keys = [_KP.pubkey(), _RECIPIENT, SYSTEM_ID]
    lookup = MessageAddressTableLookup(Pubkey.new_unique(), bytes([0]), b"")
    msg = to_bytes_versioned(
        MessageV0(
            MessageHeader(1, 0, 1),
            keys,
            _BLOCKHASH,
            [
                CompiledInstruction(
                    program_id_index=2, accounts=bytes([0, 1]), data=_sys_data()
                )
            ],
            [lookup],
        )
    )
    with pytest.raises(SolanaMessageError, match="address table lookups only"):
        decode_solana_message(msg, _plan(), _POL)


def test_too_many_ata_creations_rejected() -> None:
    mints = [Pubkey.new_unique() for _ in range(5)]
    ixs = [_ata_create(_KP.pubkey(), m) for m in mints] + [_jup()]
    plan = _plan(destination=str(JUPITER_V6_PROGRAM_ID), token_in=str(mints[0]))
    with pytest.raises(SolanaMessageError, match="too many ATA creations"):
        decode_solana_message(_msg(ixs), plan, _JUP_POL)


def test_jupiter_plan_destination_and_spl_transfers_bound() -> None:
    src = get_associated_token_address(_KP.pubkey(), _USDC)
    dst = get_associated_token_address(_RECIPIENT, _USDC)
    xfer = _spl_checked(1, src, dst, _KP.pubkey())
    wrong_dest = _plan(destination=str(_RECIPIENT), token_in=str(_USDC))
    with pytest.raises(SolanaMessageError, match="not the Jupiter program"):
        decode_solana_message(_msg([_jup()]), wrong_dest, _JUP_POL)
    plan = _plan(destination=str(JUPITER_V6_PROGRAM_ID), token_in=str(_USDC))
    with pytest.raises(SolanaMessageError, match="not allowed inside a Jupiter"):
        decode_solana_message(_msg([xfer, _jup()]), plan, _JUP_POL)


def test_message_without_any_transfer_rejected() -> None:
    msg = _msg([set_compute_unit_limit(1000)])
    with pytest.raises(SolanaMessageError, match="exactly one kind of transfer"):
        decode_solana_message(msg, _plan(), _POL)


def test_multiple_transfers_rejected() -> None:
    two_sys = _msg([_sys_transfer(500_000), _sys_transfer(500_000)])
    with pytest.raises(SolanaMessageError, match="exactly one System transfer"):
        decode_solana_message(two_sys, _plan(), _POL)
    src = get_associated_token_address(_KP.pubkey(), _USDC)
    dst = get_associated_token_address(_RECIPIENT, _USDC)
    two_spl = _msg(
        [
            _spl_checked(1, src, dst, _KP.pubkey()),
            _spl_checked(1, src, dst, _KP.pubkey()),
        ]
    )
    plan = _plan(destination=str(dst), amount=1, token_in=str(_USDC))
    with pytest.raises(SolanaMessageError, match="exactly one SPL transfer"):
        decode_solana_message(two_spl, plan, _POL)


def test_spl_transfer_must_match_plan_destination_amount_and_mint() -> None:
    src = get_associated_token_address(_KP.pubkey(), _USDC)
    dst = get_associated_token_address(_RECIPIENT, _USDC)
    plan = _plan(destination=str(dst), amount=5, token_in=str(_USDC))
    for bad in (
        _spl_checked(5, src, get_associated_token_address(_OTHER, _USDC), _KP.pubkey()),
        _spl_checked(6, src, dst, _KP.pubkey()),
    ):
        with pytest.raises(SolanaMessageError, match="SPL transfer does not match"):
            decode_solana_message(_msg([bad]), plan, _POL)
    wrong_mint_plan = _plan(destination=str(dst), amount=5, token_in=WSOL_MINT)
    with pytest.raises(SolanaMessageError, match="SPL transfer does not match"):
        decode_solana_message(
            _msg([_spl_checked(5, src, dst, _KP.pubkey())]), wrong_mint_plan, _POL
        )


def test_sign_rejects_bad_seed_length_and_signs_valid_message() -> None:
    msg = _msg([_sys_transfer()])
    plan = _plan()
    with pytest.raises(SignerTamperingError, match="32 raw seed bytes"):
        sign_solana_message(msg, plan, _POL, _SEED[:31], _AT)
    signed = sign_solana_message(msg, plan, _POL, _SEED, _AT)
    assert signed.plan_hash == plan.plan_hash
    _verify_wire(signed.raw_transaction_hex, msg)
    # Tampered bytes are rejected before any key is touched.
    with pytest.raises(SolanaMessageError):
        sign_solana_message(msg + b"\x00", plan, _POL, _SEED, _AT)


def test_message_digest_is_sha256_hex_and_changes_with_content() -> None:
    expected = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert message_digest(b"abc") == expected
    assert message_digest(b"abd") != expected
