"""Tests for chain-valid EVM (Sepolia) signing. No network access."""

import dataclasses
import itertools
from datetime import UTC, datetime, timedelta

import pytest
import rlp  # type: ignore[import-untyped]
from eth_account import Account
from hypothesis import given, settings
from hypothesis import strategies as st

from hcs_trader.domain.signing import (
    ApprovedRiskDecision,
    SignerError,
    SignerPolicy,
    SignerPolicyViolation,
    SignerTamperingError,
    ValidatedTransactionPlan,
)
from hcs_trader.evm_signing import (
    SELECTOR_APPROVE,
    SEPOLIA_SWAP_ROUTER02,
    SEPOLIA_USDC,
    SEPOLIA_WETH,
    EvmSigningPolicy,
    build_approve_payload,
    build_swap_payload,
    build_unsigned_eip1559,
    decode_evm_payload,
    encode_approve_calldata,
)
from hcs_trader.keystore import create_keystore_record
from hcs_trader.signing import IsolatedSigner

# Well-known public Hardhat/Anvil test key #0 (never holds real funds).
_KEY = bytes.fromhex("ac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80")
_ADDR = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
_PASS = "evm-test-pass-1"
_DEADLINE = datetime(2030, 1, 1, tzinfo=UTC)
_NOW = datetime(2029, 12, 31, tzinfo=UTC)
_EVM = EvmSigningPolicy()
_GAS = 250_000
_MAX_FEE = 30 * 10**9
_PRIO = 10**9
_FEES = {
    "gas_limit": _GAS,
    "max_fee_per_gas": _MAX_FEE,
    "max_priority_fee_per_gas": _PRIO,
}
_DEAD = "0x000000000000000000000000000000000000dEaD"
_DECISIONS: dict[str, ApprovedRiskDecision] = {}
_COUNTER = itertools.count(1)


def _plan(**over: object) -> ValidatedTransactionPlan:
    base: dict[str, object] = {
        "canonical_chain_id": "eip155:11155111",
        "network": "ethereum:sepolia",
        "wallet_id": "wallet-evm-01",
        "signer_address": _ADDR,
        "asset_in": f"eip155:11155111/erc20:{SEPOLIA_WETH}",
        "asset_out": f"eip155:11155111/erc20:{SEPOLIA_USDC}",
        "amount_in_base_units": 10**16,
        "min_amount_out_base_units": 20_000_000,
        "destination_id": SEPOLIA_SWAP_ROUTER02,
        "max_slippage_bps": 50,
        "max_network_fee_base_units": 10**16,
        "max_priority_fee_base_units": 2 * 10**9,
        "idempotency_key": "evm-swap-0001",
        "risk_decision_id": f"risk-dec-evm-{next(_COUNTER)}",
        "risk_policy_version": "v1.0.0",
        "simulation_success": True,
        "simulation_block_reference": "block-1",
        "token_in_mint": SEPOLIA_WETH,
        "token_out_mint": SEPOLIA_USDC,
        "deadline": _DEADLINE,
        "nonce": 7,
    }
    base.update(over)
    plan = ValidatedTransactionPlan(**base).with_hash()  # type: ignore[arg-type]
    # Every plan gets its own approved persisted risk decision (unique id).
    _DECISIONS[plan.risk_decision_id] = ApprovedRiskDecision(
        decision_id=plan.risk_decision_id, plan_hash=plan.plan_hash, allowed=True
    )
    return plan


def _policy(**over: object) -> SignerPolicy:
    base: dict[str, object] = {
        "allowed_chain_ids": frozenset({"eip155:11155111"}),
        "allowed_destinations": frozenset(
            {SEPOLIA_SWAP_ROUTER02, SEPOLIA_WETH, SEPOLIA_USDC}
        ),
        "allowed_tokens": frozenset({SEPOLIA_WETH, SEPOLIA_USDC}),
        "max_slippage_bps_limit": 100,
        "max_network_fee_limit": 10**17,
        "live_mode_enabled": True,
        "kill_switch_active": False,
    }
    base.update(over)
    return SignerPolicy(**base)  # type: ignore[arg-type]


def _signer(
    key: bytes = _KEY,
    *,
    paused: bool = False,
    evm_policy: EvmSigningPolicy | None = None,
    **over: object,
) -> IsolatedSigner:
    s = IsolatedSigner(
        _policy(**over),
        pause_state=lambda: paused,
        risk_decision_lookup=_DECISIONS.get,
        evm_policy=evm_policy,
    )
    s.register_keystore_record(
        create_keystore_record(
            wallet_id="wallet-evm-01",
            address=_ADDR,
            chain_namespace="eip155",
            secret_bytes=key,
            passphrase=_PASS,
        )
    )
    return s


def _swap_payload(plan: ValidatedTransactionPlan, fee_tier: int = 3000) -> bytes:
    return build_swap_payload(plan, fee_tier=fee_tier, **_FEES)


def _approve_plan() -> ValidatedTransactionPlan:
    return _plan(destination_id=SEPOLIA_WETH, idempotency_key="evm-approve-1")


def _approve_payload(plan: ValidatedTransactionPlan) -> bytes:
    return build_approve_payload(plan, spender=SEPOLIA_SWAP_ROUTER02, **_FEES)


def _raw_approve(amount: int) -> bytes:
    data = (
        SELECTOR_APPROVE
        + bytes(12)
        + bytes.fromhex(SEPOLIA_SWAP_ROUTER02[2:])
        + amount.to_bytes(32, "big")
    )
    return build_unsigned_eip1559(
        chain_id=11155111,
        nonce=7,
        max_priority_fee_per_gas=_PRIO,
        max_fee_per_gas=_MAX_FEE,
        gas_limit=60_000,
        to=SEPOLIA_WETH,
        data=data,
    )


# ---------------------------------------------------------------- sign + recover


def test_swap_sign_and_recover_roundtrip() -> None:
    plan = _plan()
    signed = _signer().sign_transaction(plan, _swap_payload(plan), _PASS, _NOW)
    raw = bytes.fromhex(signed.raw_transaction_hex)
    assert raw[0] == 0x02
    assert Account.recover_transaction(raw) == _ADDR
    assert len(signed.tx_hash) == 64
    assert signed.plan_hash == plan.plan_hash


def test_signing_is_deterministic_and_matches_decoded_fields() -> None:
    plan = _plan()
    a = _signer().sign_transaction(plan, _swap_payload(plan), _PASS, _NOW)
    b = _signer().sign_transaction(plan, _swap_payload(plan), _PASS, _NOW)
    assert a.raw_transaction_hex == b.raw_transaction_hex
    d = decode_evm_payload(plan, _swap_payload(plan), _EVM)
    assert d.chain_id == 11155111 and d.nonce == 7 and d.kind == "swap"
    assert d.to == SEPOLIA_SWAP_ROUTER02.lower()


def test_known_key_address_vector() -> None:
    """Hardhat key #0 must map to its published address (derivation vector)."""
    assert Account.from_key(_KEY).address == _ADDR


def test_approve_exact_amount_signs() -> None:
    plan = _approve_plan()
    signed = _signer().sign_transaction(plan, _approve_payload(plan), _PASS, _NOW)
    raw = bytes.fromhex(signed.raw_transaction_hex)
    assert Account.recover_transaction(raw) == _ADDR


# ---------------------------------------------------------------- rejections


def test_tampered_amount_in_calldata_rejected() -> None:
    plan = _plan()
    other = _plan(amount_in_base_units=10**16 + 1)
    with pytest.raises(SignerTamperingError, match="amountIn"):
        _signer().sign_transaction(plan, _swap_payload(other), _PASS, _NOW)


def test_tampered_min_out_rejected() -> None:
    plan = _plan()
    other = _plan(min_amount_out_base_units=1)
    with pytest.raises(SignerTamperingError, match="amountOutMinimum"):
        _signer().sign_transaction(plan, _swap_payload(other), _PASS, _NOW)


def test_tampered_recipient_rejected() -> None:
    plan = _plan()
    other = _plan(signer_address=_DEAD)  # builder sets recipient = attacker
    with pytest.raises(SignerTamperingError, match="recipient"):
        decode_evm_payload(plan, _swap_payload(other), _EVM)


def test_wrong_chain_id_in_payload_rejected() -> None:
    plan = _plan()
    d = decode_evm_payload(plan, _swap_payload(plan), _EVM)
    mainnet = build_unsigned_eip1559(
        chain_id=1,
        nonce=d.nonce,
        max_priority_fee_per_gas=_PRIO,
        max_fee_per_gas=_MAX_FEE,
        gas_limit=_GAS,
        to=d.to,
        data=d.data,
    )
    with pytest.raises(SignerTamperingError, match="chainId"):
        decode_evm_payload(plan, mainnet, _EVM)


def test_mainnet_plan_disabled_by_default() -> None:
    plan = _plan(canonical_chain_id="eip155:1")
    with pytest.raises(SignerPolicyViolation, match="mainnet"):
        decode_evm_payload(plan, b"\x02\xc0", _EVM)
    signer = _signer(allowed_chain_ids=frozenset({"eip155:1"}))
    with pytest.raises(SignerPolicyViolation, match="mainnet"):
        signer.sign_transaction(plan, b"\x02\xc0", _PASS, _NOW)


def test_unlimited_approve_rejected() -> None:
    plan = _approve_plan()
    for amount in (2**256 - 1, 2**255):
        with pytest.raises(SignerPolicyViolation, match="Unlimited"):
            decode_evm_payload(plan, _raw_approve(amount), _EVM)
    with pytest.raises(SignerPolicyViolation):
        encode_approve_calldata(SEPOLIA_SWAP_ROUTER02, 2**256 - 1)


def test_approve_wrong_amount_and_spender_rejected() -> None:
    plan = _approve_plan()
    with pytest.raises(SignerTamperingError, match="amount"):
        decode_evm_payload(plan, _raw_approve(plan.amount_in_base_units + 1), _EVM)
    evil = build_approve_payload(plan, spender=_DEAD, **_FEES)
    with pytest.raises(SignerPolicyViolation, match="spender"):
        decode_evm_payload(plan, evil, _EVM)


def test_non_allowlisted_router_and_token_rejected() -> None:
    plan = _plan(destination_id="0x1111111111111111111111111111111111111111")
    with pytest.raises(SignerPolicyViolation, match="router"):
        decode_evm_payload(plan, _swap_payload(plan), _EVM)
    plan2 = _plan(token_out_mint="0x2222222222222222222222222222222222222222")
    with pytest.raises(SignerPolicyViolation, match="tokenOut"):
        decode_evm_payload(plan2, _swap_payload(plan2), _EVM)


def test_fee_and_gas_caps() -> None:
    plan = _plan()
    with pytest.raises(SignerPolicyViolation, match="Gas limit"):
        big_gas = build_swap_payload(
            plan, fee_tier=3000, **{**_FEES, "gas_limit": 900_000}
        )
        decode_evm_payload(plan, big_gas, _EVM)
    with pytest.raises(SignerPolicyViolation, match="network fee"):
        big_fee = build_swap_payload(
            plan, fee_tier=3000, **{**_FEES, "max_fee_per_gas": 10**12}
        )
        decode_evm_payload(plan, big_fee, _EVM)
    with pytest.raises(SignerPolicyViolation, match="Priority"):
        big_tip = build_swap_payload(
            plan, fee_tier=3000, **{**_FEES, "max_priority_fee_per_gas": 5 * 10**9}
        )
        decode_evm_payload(plan, big_tip, _EVM)


def test_deadline_nonce_fee_tier_and_value_rejected() -> None:
    plan = _plan()
    later = _plan(deadline=_DEADLINE + timedelta(hours=1))
    with pytest.raises(SignerTamperingError, match="deadline"):
        decode_evm_payload(plan, _swap_payload(later), _EVM)
    with pytest.raises(SignerTamperingError, match="nonce"):
        decode_evm_payload(plan, _swap_payload(_plan(nonce=8)), _EVM)
    with pytest.raises(SignerPolicyViolation, match="fee tier"):
        decode_evm_payload(plan, _swap_payload(plan, fee_tier=1234), _EVM)
    items = rlp.decode(_swap_payload(plan)[1:])
    items[6] = b"\x01"
    with pytest.raises(SignerPolicyViolation, match="value"):
        decode_evm_payload(plan, b"\x02" + rlp.encode(items), _EVM)


def test_trailing_bytes_and_wrong_key_rejected() -> None:
    plan = _plan()
    with pytest.raises(SignerTamperingError):
        decode_evm_payload(plan, _swap_payload(plan) + b"\x00", _EVM)
    with pytest.raises(SignerTamperingError, match="does not control"):
        _signer(key=bytes.fromhex("11" * 32)).sign_transaction(
            plan, _swap_payload(plan), _PASS, _NOW
        )


# ---------------------------------------------------------------- signer binding


def test_pause_live_mode_plan_hash_and_expiry_still_enforced() -> None:
    plan = _plan()
    payload = _swap_payload(plan)
    with pytest.raises(SignerPolicyViolation, match="kill switch"):
        _signer(kill_switch_active=True).sign_transaction(plan, payload, _PASS, _NOW)
    with pytest.raises(SignerPolicyViolation, match="live mode"):
        _signer(live_mode_enabled=False).sign_transaction(plan, payload, _PASS, _NOW)
    stale = dataclasses.replace(plan, amount_in_base_units=1)  # stale hash
    with pytest.raises(SignerTamperingError, match="plan hash mismatch"):
        _signer().sign_transaction(stale, payload, _PASS, _NOW)
    with pytest.raises(SignerPolicyViolation, match="expired"):
        _signer().sign_transaction(
            plan, payload, _PASS, _DEADLINE + timedelta(seconds=1)
        )


def test_idempotent_replay_is_refused_even_with_garbage_payload() -> None:
    plan = _plan()
    signer = _signer()
    signer.sign_transaction(plan, _swap_payload(plan), _PASS, _NOW)
    # A replay never returns a cached signature, whatever the payload or passphrase.
    for payload in (_swap_payload(plan), b"garbage", b""):
        with pytest.raises(SignerPolicyViolation, match="already signed"):
            signer.sign_transaction(plan, payload, "wrong-pass", _NOW)
    changed = _plan(min_amount_out_base_units=21_000_000)
    with pytest.raises(SignerPolicyViolation, match="already signed"):
        signer.sign_transaction(changed, _swap_payload(changed), _PASS, _NOW)


def test_failed_evm_signing_does_not_consume_idempotency_key() -> None:
    plan = _plan()
    signer = _signer()
    with pytest.raises(SignerError):
        signer.sign_transaction(plan, b"garbage", _PASS, _NOW)
    signed = signer.sign_transaction(plan, _swap_payload(plan), _PASS, _NOW)
    assert signed.plan_hash == plan.plan_hash


def test_kill_switch_and_pause_block_resign() -> None:
    plan = _plan()
    signer = _signer()
    signer.sign_transaction(plan, _swap_payload(plan), _PASS, _NOW)
    signer.policy = dataclasses.replace(signer.policy, kill_switch_active=True)
    with pytest.raises(SignerPolicyViolation, match="kill switch"):
        signer.sign_transaction(plan, _swap_payload(plan), _PASS, _NOW)
    plan2 = _plan()
    with pytest.raises(SignerPolicyViolation, match="paused"):
        _signer(paused=True).sign_transaction(plan2, _swap_payload(plan2), _PASS, _NOW)


def test_unapproved_risk_decision_blocks_evm_signing() -> None:
    plan = _plan()
    _DECISIONS.pop(plan.risk_decision_id)
    with pytest.raises(SignerPolicyViolation, match="not found"):
        _signer().sign_transaction(plan, _swap_payload(plan), _PASS, _NOW)


# ---------------------------------------------------------------- review follow-ups


def test_priority_fee_cap_is_per_gas_wei_at_boundary() -> None:
    """max_priority_fee_base_units is wei per gas, not a total."""
    plan = _plan(max_priority_fee_base_units=_PRIO)
    decode_evm_payload(plan, _swap_payload(plan), _EVM)  # exactly at the cap
    tighter = _plan(max_priority_fee_base_units=_PRIO - 1)
    with pytest.raises(SignerPolicyViolation, match="Priority"):
        decode_evm_payload(tighter, _swap_payload(plan), _EVM)


def test_mainnet_enabled_only_with_explicit_operator_allowlists() -> None:
    mainnet_router = "0x" + "ab" * 20
    mainnet_in = "0x" + "cd" * 20
    mainnet_out = "0x" + "ef" * 20
    over: dict[str, object] = {
        "canonical_chain_id": "eip155:1",
        "destination_id": mainnet_router,
        "token_in_mint": mainnet_in,
        "token_out_mint": mainnet_out,
    }
    plan = _plan(**over)
    payload_policy = EvmSigningPolicy(
        allow_mainnet=True,
        allowed_routers=frozenset({mainnet_router}),
        allowed_tokens=frozenset({mainnet_in, mainnet_out}),
    )
    payload = build_swap_payload(plan, fee_tier=3000, **_FEES)
    # allow_mainnet alone is not enough: default allowlists are Sepolia only.
    with pytest.raises(SignerPolicyViolation):
        decode_evm_payload(plan, payload, EvmSigningPolicy(allow_mainnet=True))
    decoded = decode_evm_payload(plan, payload, payload_policy)
    assert decoded.chain_id == 1 and decoded.kind == "swap"
    with pytest.raises(SignerPolicyViolation, match="mainnet"):
        decode_evm_payload(plan, payload, _EVM)


def test_contract_creation_zero_gas_and_access_list_rejected() -> None:
    plan = _plan()
    items = rlp.decode(_swap_payload(plan)[1:])
    creation = list(items)
    creation[5] = b""
    with pytest.raises(SignerError):
        decode_evm_payload(plan, b"" + rlp.encode(creation), _EVM)
    zero_gas = list(items)
    zero_gas[4] = b""
    with pytest.raises(SignerError):
        decode_evm_payload(plan, b"" + rlp.encode(zero_gas), _EVM)
    with_access_list = list(items)
    with_access_list[8] = [[bytes.fromhex(SEPOLIA_WETH[2:]), []]]
    with pytest.raises(SignerError):
        decode_evm_payload(plan, b"" + rlp.encode(with_access_list), _EVM)


def test_invalid_plan_signer_address_rejected() -> None:
    plan = _plan(signer_address="0x1234")
    with pytest.raises(SignerError):
        decode_evm_payload(plan, _swap_payload(_plan()), _EVM)


# ---------------------------------------------------------------- fuzz


@settings(max_examples=300, deadline=None)
@given(st.binary(max_size=600))
def test_fuzz_arbitrary_bytes_only_raise_signer_errors(data: bytes) -> None:
    try:
        decode_evm_payload(_plan(), data, _EVM)
    except SignerError:
        pass


@settings(max_examples=300, deadline=None)
@given(st.binary(max_size=300))
def test_fuzz_type2_prefixed_bytes(data: bytes) -> None:
    try:
        decode_evm_payload(_plan(), b"\x02" + data, _EVM)
    except SignerError:
        pass


@settings(max_examples=200, deadline=None)
@given(st.integers(min_value=0, max_value=2**256 - 1), st.binary(max_size=200))
def test_fuzz_mutated_calldata_never_accepted_unless_exact(
    amount: int, junk: bytes
) -> None:
    plan = _plan()
    good = decode_evm_payload(plan, _swap_payload(plan), _EVM)
    mutated = build_unsigned_eip1559(
        chain_id=11155111,
        nonce=7,
        max_priority_fee_per_gas=_PRIO,
        max_fee_per_gas=_MAX_FEE,
        gas_limit=_GAS,
        to=good.to,
        data=good.data[:4] + junk,
    )
    with pytest.raises(SignerError):
        decode_evm_payload(plan, mutated, _EVM)
    ap = _approve_plan()
    if amount == ap.amount_in_base_units:
        decode_evm_payload(ap, _raw_approve(amount), _EVM)
    else:
        with pytest.raises(SignerError):
            decode_evm_payload(ap, _raw_approve(amount), _EVM)
