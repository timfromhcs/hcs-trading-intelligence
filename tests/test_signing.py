"""Tests for Phase 7 previews, keystore and isolated signing invariants."""

import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from hcs_trader.domain.signing import (
    ApprovedRiskDecision,
    KeystoreError,
    SignerPolicy,
    SignerPolicyViolation,
    SignerTamperingError,
    SigningUnsupported,
    ValidatedTransactionPlan,
    signing_capabilities,
)
from hcs_trader.evm_signing import (
    SEPOLIA_SWAP_ROUTER02,
    SEPOLIA_USDC,
    SEPOLIA_WETH,
    build_swap_payload,
)
from hcs_trader.keystore import (
    create_keystore_record,
    decrypt_keystore_record,
)
from hcs_trader.mcp_server import create_mcp_server
from hcs_trader.risk import RiskEvaluation
from hcs_trader.signing import (
    IsolatedSigner,
    decode_and_verify_payload,
)
from hcs_trader.storage import AuditStore, KillSwitchCause

# Public Hardhat/Anvil test key #0 (controls 0xf39F...2266; never holds real funds).
_SYNTHETIC_EVM_KEY = bytes.fromhex(
    "ac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
)
_SYNTHETIC_SOL_KEY = bytes.fromhex(
    "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
)


def _sample_plan(
    *,
    chain_id: str = "eip155:11155111",
    destination: str = SEPOLIA_SWAP_ROUTER02,
    amount_in: int = 10**16,
    slippage_bps: int = 50,
    fee: int = 10**16,
    deadline: datetime | None = None,
    simulation_success: bool = True,
    token_in: str | None = SEPOLIA_WETH,
    token_out: str | None = SEPOLIA_USDC,
) -> ValidatedTransactionPlan:
    plan = ValidatedTransactionPlan(
        canonical_chain_id=chain_id,
        network="ethereum:sepolia",
        wallet_id="wallet-test-01",
        signer_address="0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266",
        asset_in=f"eip155:11155111/erc20:{SEPOLIA_WETH}",
        asset_out=f"eip155:11155111/erc20:{SEPOLIA_USDC}",
        amount_in_base_units=amount_in,
        min_amount_out_base_units=20_000_000,
        destination_id=destination,
        max_slippage_bps=slippage_bps,
        max_network_fee_base_units=fee,
        max_priority_fee_base_units=2 * 10**9,
        idempotency_key="swap-test-0001",
        risk_decision_id="risk-dec-0001",
        risk_policy_version="v1.0.0",
        simulation_success=simulation_success,
        simulation_block_reference="block-5000000",
        token_in_mint=token_in,
        token_out_mint=token_out,
        deadline=deadline or datetime.now(UTC) + timedelta(minutes=5),
        nonce=1,
    )
    return plan.with_hash()


def _active_policy() -> SignerPolicy:
    return SignerPolicy(
        allowed_chain_ids=frozenset({"eip155:11155111", "solana:devnet"}),
        allowed_destinations=frozenset(
            {
                SEPOLIA_SWAP_ROUTER02,
                "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",
            }
        ),
        allowed_tokens=frozenset(
            {
                SEPOLIA_USDC,
                SEPOLIA_WETH,
                "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            }
        ),
        max_slippage_bps_limit=100,
        max_network_fee_limit=10**17,
        live_mode_enabled=True,
        kill_switch_active=False,
    )


def _signer(
    policy: SignerPolicy,
    *,
    paused: bool = False,
    decisions: dict[str, ApprovedRiskDecision] | None = None,
    allow_test_only_signing: bool = True,
) -> IsolatedSigner:
    store = dict(decisions or {})
    return IsolatedSigner(
        policy,
        pause_state=lambda: paused,
        risk_decision_lookup=store.get,
        allow_test_only_signing=allow_test_only_signing,
    )


def _approve(plan: ValidatedTransactionPlan) -> dict[str, ApprovedRiskDecision]:
    return {
        plan.risk_decision_id: ApprovedRiskDecision(
            decision_id=plan.risk_decision_id,
            plan_hash=plan.plan_hash,
            allowed=True,
        )
    }


def _evm_payload(plan: ValidatedTransactionPlan) -> bytes:
    return build_swap_payload(
        plan,
        fee_tier=3000,
        gas_limit=250_000,
        max_fee_per_gas=30 * 10**9,
        max_priority_fee_per_gas=10**9,
    )


def _evm_record(passphrase: str) -> dict[str, Any]:
    return create_keystore_record(
        wallet_id="wallet-test-01",
        address="0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266",
        chain_namespace="eip155",
        secret_bytes=_SYNTHETIC_EVM_KEY,
        passphrase=passphrase,
    )


def test_keystore_encrypt_decrypt_roundtrip() -> None:
    passphrase = "correct-horse-battery-staple"
    record = create_keystore_record(
        wallet_id="wallet-test-01",
        address="0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266",
        chain_namespace="eip155",
        secret_bytes=_SYNTHETIC_EVM_KEY,
        passphrase=passphrase,
    )
    assert record["version"] == 1
    assert record["kdf"] == "scrypt"
    decrypted = decrypt_keystore_record(record, passphrase)
    assert decrypted == _SYNTHETIC_EVM_KEY


def test_keystore_invalid_passphrase_fails() -> None:
    record = create_keystore_record(
        wallet_id="wallet-test-01",
        address="0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266",
        chain_namespace="eip155",
        secret_bytes=_SYNTHETIC_EVM_KEY,
        passphrase="valid-password",
    )
    with pytest.raises(KeystoreError, match="invalid passphrase"):
        decrypt_keystore_record(record, "wrong-password")


def test_keystore_tampering_fails() -> None:
    record = create_keystore_record(
        wallet_id="wallet-test-01",
        address="0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266",
        chain_namespace="eip155",
        secret_bytes=_SYNTHETIC_EVM_KEY,
        passphrase="valid-password",
    )
    # Alter ciphertext
    raw_ct = bytes.fromhex(record["ciphertext_hex"])
    altered_ct = bytes([raw_ct[0] ^ 0xFF]) + raw_ct[1:]
    record["ciphertext_hex"] = altered_ct.hex()
    with pytest.raises(KeystoreError, match="invalid passphrase or corrupted keystore"):
        decrypt_keystore_record(record, "valid-password")


def test_keystore_associated_data_tampering_fails() -> None:
    record = create_keystore_record(
        wallet_id="wallet-test-01",
        address="0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266",
        chain_namespace="eip155",
        secret_bytes=_SYNTHETIC_EVM_KEY,
        passphrase="valid-password",
    )
    # Alter address in metadata
    record["address"] = "0x0000000000000000000000000000000000000000"
    with pytest.raises(KeystoreError, match="invalid passphrase or corrupted keystore"):
        decrypt_keystore_record(record, "valid-password")


def test_invariant_live_mode_false_rejects() -> None:
    policy = SignerPolicy(
        allowed_chain_ids=frozenset({"eip155:11155111"}),
        allowed_destinations=frozenset({SEPOLIA_SWAP_ROUTER02}),
        allowed_tokens=frozenset(),
        max_slippage_bps_limit=100,
        max_network_fee_limit=10**17,
        live_mode_enabled=False,
        kill_switch_active=False,
    )
    plan = _sample_plan()
    signer = _signer(policy)
    with pytest.raises(SignerPolicyViolation, match="live mode is not enabled"):
        signer.preview_transaction(plan, at=datetime.now(UTC))


def test_invariant_kill_switch_active_rejects() -> None:
    policy = SignerPolicy(
        allowed_chain_ids=frozenset({"eip155:11155111"}),
        allowed_destinations=frozenset({SEPOLIA_SWAP_ROUTER02}),
        allowed_tokens=frozenset(),
        max_slippage_bps_limit=100,
        max_network_fee_limit=10**17,
        live_mode_enabled=True,
        kill_switch_active=True,
    )
    plan = _sample_plan()
    signer = _signer(policy)
    with pytest.raises(SignerPolicyViolation, match="kill switch is active"):
        signer.preview_transaction(plan, at=datetime.now(UTC))


def test_invariant_simulation_failure_rejects() -> None:
    policy = _active_policy()
    plan = _sample_plan(simulation_success=False)
    signer = _signer(policy)
    with pytest.raises(SignerPolicyViolation, match="simulation did not succeed"):
        signer.preview_transaction(plan, at=datetime.now(UTC))


def test_invariant_destination_not_allowlisted_rejects() -> None:
    policy = _active_policy()
    plan = _sample_plan(destination="0x1111111111111111111111111111111111111111")
    signer = _signer(policy)
    with pytest.raises(SignerPolicyViolation, match="destination.*not allowlisted"):
        signer.preview_transaction(plan, at=datetime.now(UTC))


def test_invariant_plan_hash_mismatch_rejects() -> None:
    policy = _active_policy()
    plan = _sample_plan()
    # Tamper with plan amount without recomputing plan_hash
    tampered_plan = ValidatedTransactionPlan(
        canonical_chain_id=plan.canonical_chain_id,
        network=plan.network,
        wallet_id=plan.wallet_id,
        signer_address=plan.signer_address,
        asset_in=plan.asset_in,
        asset_out=plan.asset_out,
        amount_in_base_units=999999999999999999,
        min_amount_out_base_units=plan.min_amount_out_base_units,
        destination_id=plan.destination_id,
        max_slippage_bps=plan.max_slippage_bps,
        max_network_fee_base_units=plan.max_network_fee_base_units,
        max_priority_fee_base_units=plan.max_priority_fee_base_units,
        idempotency_key=plan.idempotency_key,
        risk_decision_id=plan.risk_decision_id,
        risk_policy_version=plan.risk_policy_version,
        simulation_success=plan.simulation_success,
        simulation_block_reference=plan.simulation_block_reference,
        token_in_mint=plan.token_in_mint,
        token_out_mint=plan.token_out_mint,
        deadline=plan.deadline,
        nonce=plan.nonce,
        plan_hash=plan.plan_hash,  # Old hash!
    )
    signer = _signer(policy)
    with pytest.raises(SignerTamperingError, match="plan hash mismatch"):
        signer.preview_transaction(tampered_plan, at=datetime.now(UTC))


def test_invariant_expired_deadline_rejects() -> None:
    policy = _active_policy()
    past = datetime.now(UTC) - timedelta(minutes=1)
    plan = _sample_plan(deadline=past)
    signer = _signer(policy)
    with pytest.raises(SignerPolicyViolation, match="transaction plan has expired"):
        signer.preview_transaction(plan, at=datetime.now(UTC))


def test_invariant_slippage_excess_rejects() -> None:
    policy = _active_policy()
    plan = _sample_plan(slippage_bps=200)
    signer = _signer(policy)
    with pytest.raises(
        SignerPolicyViolation, match="slippage 200 bps exceeds policy limit"
    ):
        signer.preview_transaction(plan, at=datetime.now(UTC))


def test_invariant_payload_tampering_rejects() -> None:
    plan = _sample_plan()
    # Different amount in payload
    tampered_payload = json.dumps(
        {
            "to": plan.destination_id,
            "chain_id": plan.canonical_chain_id,
            "amount": 999999999999,
            "from": plan.signer_address,
        }
    ).encode("utf-8")
    with pytest.raises(
        SignerTamperingError, match="Payload amount.*does not match plan"
    ):
        decode_and_verify_payload(plan, tampered_payload)

    # Different destination in payload
    tampered_dest = json.dumps(
        {
            "to": "0x0000000000000000000000000000000000000000",
            "chain_id": plan.canonical_chain_id,
            "amount": plan.amount_in_base_units,
            "from": plan.signer_address,
        }
    ).encode("utf-8")
    with pytest.raises(
        SignerTamperingError, match="Payload destination.*does not match plan"
    ):
        decode_and_verify_payload(plan, tampered_dest)


def test_preview_transaction_success() -> None:
    policy = _active_policy()
    plan = _sample_plan()
    signer = _signer(policy)
    now = datetime.now(UTC)
    preview = signer.preview_transaction(plan, at=now)
    assert preview["plan_hash"] == plan.plan_hash
    assert preview["destination"] == plan.destination_id
    assert preview["amount_in"] == plan.amount_in_base_units


def test_legacy_json_path_rejects_solana_plans() -> None:
    policy = _active_policy()
    passphrase = "solana-password-456"
    sol_address = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
    record = create_keystore_record(
        wallet_id="wallet-sol-01",
        address=sol_address,
        chain_namespace="solana",
        secret_bytes=_SYNTHETIC_SOL_KEY,
        passphrase=passphrase,
    )
    plan = ValidatedTransactionPlan(
        canonical_chain_id="solana:devnet",
        network="solana:devnet",
        wallet_id="wallet-sol-01",
        signer_address=sol_address,
        asset_in="solana:devnet/native:SOL",
        asset_out="solana:devnet/token:EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        amount_in_base_units=1000000000,
        min_amount_out_base_units=145000000,
        destination_id="JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",
        max_slippage_bps=50,
        max_network_fee_base_units=5000,
        max_priority_fee_base_units=1000,
        idempotency_key="sol-swap-0001",
        risk_decision_id="risk-dec-0002",
        risk_policy_version="v1.0.0",
        simulation_success=True,
        simulation_block_reference="slot-250000000",
        token_out_mint="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        deadline=datetime.now(UTC) + timedelta(minutes=5),
    ).with_hash()
    signer = _signer(policy, decisions=_approve(plan), allow_test_only_signing=False)
    signer.register_keystore_record(record)

    payload = json.dumps(
        {
            "program_id": plan.destination_id,
            "canonical_chain_id": plan.canonical_chain_id,
            "amount": plan.amount_in_base_units,
            "signer": plan.signer_address,
        }
    ).encode("utf-8")

    now = datetime.now(UTC)
    # The JSON-envelope path is not chain-valid for Solana and would bypass the
    # message decoder; chain-valid signing is sign_solana_transaction only.
    with pytest.raises(SigningUnsupported, match="sign_solana_transaction"):
        signer.sign_transaction(plan, payload, passphrase, at=now)


def test_mcp_server_has_no_secret_export_or_signing_tools(tmp_path: Path) -> None:
    """Verify Invariant 9: MCP clients cannot access signing keys."""

    server = create_mcp_server(tmp_path / "test.db")
    tool_names = set(server._tool_manager._tools.keys())
    for name in tool_names:
        assert "sign" not in name.lower()
        assert "key" not in name.lower()
        assert "seed" not in name.lower()
        assert "export" not in name.lower()
        assert "withdraw" not in name.lower()
        assert "transfer" not in name.lower()


def test_persisted_pause_blocks_signing() -> None:
    passphrase = "pause-password"
    plan = _sample_plan()
    signer = _signer(_active_policy(), paused=True, decisions=_approve(plan))
    signer.register_keystore_record(_evm_record(passphrase))
    with pytest.raises(SignerPolicyViolation, match="execution is paused"):
        signer.sign_transaction(
            plan, _evm_payload(plan), passphrase, at=datetime.now(UTC)
        )


def test_pause_state_is_read_at_signing_time() -> None:
    passphrase = "pause-password"
    plan = _sample_plan()
    state = {"paused": False}
    decisions = _approve(plan)
    signer = IsolatedSigner(
        _active_policy(),
        pause_state=lambda: state["paused"],
        risk_decision_lookup=decisions.get,
        allow_test_only_signing=True,
    )
    signer.register_keystore_record(_evm_record(passphrase))
    state["paused"] = True
    with pytest.raises(SignerPolicyViolation, match="execution is paused"):
        signer.sign_transaction(
            plan, _evm_payload(plan), passphrase, at=datetime.now(UTC)
        )


def test_pause_state_error_fails_closed() -> None:
    passphrase = "pause-password"
    plan = _sample_plan()
    decisions = _approve(plan)

    def broken() -> bool:
        raise RuntimeError("database locked")

    signer = IsolatedSigner(
        _active_policy(),
        pause_state=broken,
        risk_decision_lookup=decisions.get,
        allow_test_only_signing=True,
    )
    signer.register_keystore_record(_evm_record(passphrase))
    with pytest.raises(SignerPolicyViolation, match="pause state unavailable"):
        signer.sign_transaction(
            plan, _evm_payload(plan), passphrase, at=datetime.now(UTC)
        )


def test_unknown_risk_decision_blocks_signing() -> None:
    passphrase = "risk-password"
    plan = _sample_plan()
    signer = _signer(_active_policy(), decisions={})
    signer.register_keystore_record(_evm_record(passphrase))
    with pytest.raises(SignerPolicyViolation, match="risk decision.*not found"):
        signer.sign_transaction(
            plan, _evm_payload(plan), passphrase, at=datetime.now(UTC)
        )


def test_rejected_risk_decision_blocks_signing() -> None:
    passphrase = "risk-password"
    plan = _sample_plan()
    decisions = {
        plan.risk_decision_id: ApprovedRiskDecision(
            decision_id=plan.risk_decision_id,
            plan_hash=plan.plan_hash,
            allowed=False,
        )
    }
    signer = _signer(_active_policy(), decisions=decisions)
    signer.register_keystore_record(_evm_record(passphrase))
    with pytest.raises(SignerPolicyViolation, match="risk decision.*not approved"):
        signer.sign_transaction(
            plan, _evm_payload(plan), passphrase, at=datetime.now(UTC)
        )


def test_risk_decision_for_different_plan_hash_blocks_signing() -> None:
    passphrase = "risk-password"
    plan = _sample_plan()
    decisions = {
        plan.risk_decision_id: ApprovedRiskDecision(
            decision_id=plan.risk_decision_id,
            plan_hash="0" * 64,
            allowed=True,
        )
    }
    signer = _signer(_active_policy(), decisions=decisions)
    signer.register_keystore_record(_evm_record(passphrase))
    with pytest.raises(SignerTamperingError, match="risk decision plan hash"):
        signer.sign_transaction(
            plan, _evm_payload(plan), passphrase, at=datetime.now(UTC)
        )


def test_risk_decision_id_mismatch_blocks_signing() -> None:
    passphrase = "risk-password"
    plan = _sample_plan()
    decisions = {
        plan.risk_decision_id: ApprovedRiskDecision(
            decision_id="risk-dec-other",
            plan_hash=plan.plan_hash,
            allowed=True,
        )
    }
    signer = _signer(_active_policy(), decisions=decisions)
    signer.register_keystore_record(_evm_record(passphrase))
    with pytest.raises(SignerTamperingError, match="risk decision id"):
        signer.sign_transaction(
            plan, _evm_payload(plan), passphrase, at=datetime.now(UTC)
        )


def test_duplicate_idempotency_key_rejected() -> None:
    passphrase = "dup-password"
    plan = _sample_plan()
    signer = _signer(_active_policy(), decisions=_approve(plan))
    signer.register_keystore_record(_evm_record(passphrase))
    now = datetime.now(UTC)
    signer.sign_transaction(plan, _evm_payload(plan), passphrase, at=now)
    with pytest.raises(SignerPolicyViolation, match="idempotency key.*already signed"):
        signer.sign_transaction(plan, _evm_payload(plan), passphrase, at=now)


def test_failed_signing_does_not_consume_idempotency_key() -> None:
    passphrase = "dup-password"
    plan = _sample_plan()
    signer = _signer(_active_policy(), decisions=_approve(plan))
    signer.register_keystore_record(_evm_record(passphrase))
    now = datetime.now(UTC)
    with pytest.raises(KeystoreError):
        signer.sign_transaction(plan, _evm_payload(plan), "wrong-password", at=now)
    signed = signer.sign_transaction(plan, _evm_payload(plan), passphrase, at=now)
    assert signed.plan_hash == plan.plan_hash


def test_evm_signing_does_not_require_test_only_flag() -> None:
    passphrase = "evm-password"
    plan = _sample_plan()
    signer = _signer(
        _active_policy(), decisions=_approve(plan), allow_test_only_signing=False
    )
    signer.register_keystore_record(_evm_record(passphrase))
    signed = signer.sign_transaction(
        plan, _evm_payload(plan), passphrase, at=datetime.now(UTC)
    )
    assert signed.raw_transaction_hex.startswith("02")


def test_evm_json_envelope_payload_is_rejected() -> None:
    passphrase = "evm-password"
    plan = _sample_plan()
    signer = _signer(_active_policy(), decisions=_approve(plan))
    signer.register_keystore_record(_evm_record(passphrase))
    envelope = json.dumps(
        {
            "to": plan.destination_id,
            "chain_id": plan.canonical_chain_id,
            "amount": plan.amount_in_base_units,
            "from": plan.signer_address,
        }
    ).encode("utf-8")
    with pytest.raises(SignerTamperingError):
        signer.sign_transaction(plan, envelope, passphrase, at=datetime.now(UTC))


def test_utxo_signing_unsupported_without_test_only_flag() -> None:
    plan = ValidatedTransactionPlan(
        canonical_chain_id="bip122:000000000933ea01ad0ee984209779ba",
        network="bitcoin:testnet",
        wallet_id="wallet-btc-01",
        signer_address="tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx",
        asset_in="bip122:000000000933ea01ad0ee984209779ba/slip44:0",
        asset_out="bip122:000000000933ea01ad0ee984209779ba/slip44:0",
        amount_in_base_units=10000,
        min_amount_out_base_units=9000,
        destination_id="tb1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3q0sl5k7",
        max_slippage_bps=0,
        max_network_fee_base_units=500,
        max_priority_fee_base_units=0,
        idempotency_key="btc-spend-0001",
        risk_decision_id="risk-dec-0003",
        risk_policy_version="v1.0.0",
        simulation_success=True,
        simulation_block_reference="block-2500000",
        deadline=datetime.now(UTC) + timedelta(minutes=5),
    ).with_hash()
    policy = SignerPolicy(
        allowed_chain_ids=frozenset({plan.canonical_chain_id}),
        allowed_destinations=frozenset({plan.destination_id}),
        allowed_tokens=frozenset(),
        max_slippage_bps_limit=100,
        max_network_fee_limit=100000,
        live_mode_enabled=True,
    )
    passphrase = "btc-password"
    signer = _signer(policy, decisions=_approve(plan), allow_test_only_signing=False)
    signer.register_keystore_record(
        create_keystore_record(
            wallet_id=plan.wallet_id,
            address=plan.signer_address,
            chain_namespace="bip122",
            secret_bytes=_SYNTHETIC_EVM_KEY,
            passphrase=passphrase,
        )
    )
    payload = json.dumps(
        {
            "to": plan.destination_id,
            "chain_id": plan.canonical_chain_id,
            "amount": plan.amount_in_base_units,
        }
    ).encode("utf-8")
    with pytest.raises(SigningUnsupported, match="bip122"):
        signer.sign_transaction(plan, payload, passphrase, at=datetime.now(UTC))


def test_signing_capabilities_report_evm_valid_and_utxo_unsupported() -> None:
    caps = signing_capabilities()
    assert caps["eip155"].chain_valid is True
    assert caps["eip155"].requires_test_only_flag is False
    assert caps["bip122"].chain_valid is False
    assert "placeholder" in caps["bip122"].note.lower()
    assert set(caps) >= {"eip155", "bip122", "solana"}


# --- Durable (store-backed) signer state: RE-02 / RE-04 ---


def _store_signer(
    store: AuditStore, *, allow_test_only_signing: bool = True
) -> IsolatedSigner:
    return IsolatedSigner(
        _active_policy(),
        pause_state=store.fail_safe_paused,
        risk_decision_lookup=store.approved_risk_decision,
        allow_test_only_signing=allow_test_only_signing,
        signed_plans=store,
    )


def _persist_decision(
    store: AuditStore, plan: ValidatedTransactionPlan, *, allowed: bool = True
) -> None:
    store.record_risk_decision(
        RiskEvaluation(
            decision_id=plan.risk_decision_id,
            allowed=allowed,
            reason=None if allowed else "policy said no",
            evaluated_at=datetime.now(UTC),
            plan_hash=plan.plan_hash,
        ),
        policy_version="v1.0.0",
    )


def test_signer_only_signs_decisions_present_in_the_store(tmp_path: Path) -> None:
    store = AuditStore(tmp_path / "s.sqlite")
    store.initialize()
    plan = _sample_plan()
    signer = _store_signer(store)
    signer.register_keystore_record(_evm_record("pw"))
    with pytest.raises(SignerPolicyViolation, match="not found"):
        signer.sign_transaction(plan, _evm_payload(plan), "pw", datetime.now(UTC))
    _persist_decision(store, plan)
    signed = signer.sign_transaction(plan, _evm_payload(plan), "pw", datetime.now(UTC))
    assert signed.plan_hash == plan.plan_hash


def test_rejected_stored_decision_blocks_signing(tmp_path: Path) -> None:
    store = AuditStore(tmp_path / "s.sqlite")
    store.initialize()
    plan = _sample_plan()
    _persist_decision(store, plan, allowed=False)
    signer = _store_signer(store)
    signer.register_keystore_record(_evm_record("pw"))
    with pytest.raises(SignerPolicyViolation, match="not approved"):
        signer.sign_transaction(plan, _evm_payload(plan), "pw", datetime.now(UTC))
    assert not store.is_plan_signed(plan.idempotency_key)


def test_signed_plan_survives_restart_and_blocks_resigning(tmp_path: Path) -> None:
    path = tmp_path / "s.sqlite"
    store = AuditStore(path)
    store.initialize()
    plan = _sample_plan()
    _persist_decision(store, plan)
    first = _store_signer(store)
    first.register_keystore_record(_evm_record("pw"))
    signed = first.sign_transaction(plan, _evm_payload(plan), "pw", datetime.now(UTC))

    reopened = AuditStore(path)
    reopened.initialize()
    assert reopened.is_plan_signed(plan.idempotency_key)
    second = _store_signer(reopened)  # fresh process: no in-memory memory of signing
    second.register_keystore_record(_evm_record("pw"))
    with pytest.raises(SignerPolicyViolation, match="not approved"):
        second.sign_transaction(plan, _evm_payload(plan), "pw", datetime.now(UTC))
    # A fresh, approved decision for a plan reusing the key is still refused.
    replay = replace(plan, risk_decision_id="risk-dec-0002").with_hash()
    _persist_decision(reopened, replay)
    with pytest.raises(SignerPolicyViolation, match="already signed"):
        second.sign_transaction(replay, _evm_payload(replay), "pw", datetime.now(UTC))
    with closing(sqlite3.connect(path)) as connection:
        row = connection.execute(
            "SELECT tx_hash, plan_hash FROM signed_plans WHERE idempotency_key = ?",
            (plan.idempotency_key,),
        ).fetchone()
    assert row == (signed.tx_hash, plan.plan_hash)


def test_one_risk_decision_cannot_sign_a_second_plan_instance(tmp_path: Path) -> None:
    store = AuditStore(tmp_path / "s.sqlite")
    store.initialize()
    plan = _sample_plan()
    _persist_decision(store, plan)
    signer = _store_signer(store)
    signer.register_keystore_record(_evm_record("pw"))
    signer.sign_transaction(plan, _evm_payload(plan), "pw", datetime.now(UTC))
    # Same plan content under a new idempotency key shares the plan hash only
    # if the key is excluded; the decision is bound to it and already consumed.
    with pytest.raises(SignerPolicyViolation):
        store.commit_signed_plan(
            idempotency_key="another-key",
            plan_hash=plan.plan_hash,
            risk_decision_id=plan.risk_decision_id,
            tx_hash="ab" * 32,
            signed_at=datetime.now(UTC),
        )
    decision = store.approved_risk_decision(plan.risk_decision_id)
    assert decision is not None and decision.allowed is False


def test_commit_signed_plan_rejects_hash_mismatch_and_leaves_decision(
    tmp_path: Path,
) -> None:
    store = AuditStore(tmp_path / "s.sqlite")
    store.initialize()
    plan = _sample_plan()
    _persist_decision(store, plan)
    with pytest.raises(SignerPolicyViolation):
        store.commit_signed_plan(
            idempotency_key=plan.idempotency_key,
            plan_hash="0" * 64,
            risk_decision_id=plan.risk_decision_id,
            tx_hash="cd" * 32,
            signed_at=datetime.now(UTC),
        )
    approved = store.approved_risk_decision(plan.risk_decision_id)
    assert approved is not None and approved.allowed is True


def test_persisted_kill_switch_blocks_signing_across_restart(tmp_path: Path) -> None:
    path = tmp_path / "s.sqlite"
    store = AuditStore(path)
    store.initialize()
    plan = _sample_plan()
    _persist_decision(store, plan)
    store.trip_kill_switch(KillSwitchCause.RECONCILIATION_MISMATCH, "balance drift")
    restarted = AuditStore(path)
    restarted.initialize()
    signer = _store_signer(restarted)
    signer.register_keystore_record(_evm_record("pw"))
    with pytest.raises(SignerPolicyViolation, match="paused"):
        signer.sign_transaction(plan, _evm_payload(plan), "pw", datetime.now(UTC))


def test_unavailable_ledger_fails_closed() -> None:
    class BrokenLedger:
        def is_plan_signed(self, idempotency_key: str) -> bool:
            raise RuntimeError("disk I/O error")

        def commit_signed_plan(self, **_: object) -> None:
            raise RuntimeError("disk I/O error")

    plan = _sample_plan()
    decisions = _approve(plan)
    signer = IsolatedSigner(
        _active_policy(),
        pause_state=lambda: False,
        risk_decision_lookup=decisions.get,
        allow_test_only_signing=True,
        signed_plans=BrokenLedger(),
    )
    signer.register_keystore_record(_evm_record("pw"))
    with pytest.raises(SignerPolicyViolation, match="ledger unavailable"):
        signer.sign_transaction(plan, _evm_payload(plan), "pw", datetime.now(UTC))
