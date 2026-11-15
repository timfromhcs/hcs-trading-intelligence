"""Tests for Phase 7 previews, keystore and isolated signing invariants."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hcs_trader.domain.signing import (
    KeystoreError,
    SignerPolicy,
    SignerPolicyViolation,
    SignerTamperingError,
    ValidatedTransactionPlan,
)
from hcs_trader.keystore import (
    create_keystore_record,
    decrypt_keystore_record,
)
from hcs_trader.mcp_server import create_mcp_server
from hcs_trader.signing import (
    IsolatedSigner,
    decode_and_verify_payload,
)

_SYNTHETIC_EVM_KEY = bytes.fromhex(
    "4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f36088a"
)
_SYNTHETIC_SOL_KEY = bytes.fromhex(
    "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
)


def _sample_plan(
    *,
    chain_id: str = "eip155:11155111",
    destination: str = "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D",
    amount_in: int = 100000000000000000,
    slippage_bps: int = 50,
    fee: int = 50000,
    deadline: datetime | None = None,
    simulation_success: bool = True,
    token_in: str | None = None,
    token_out: str | None = None,
) -> ValidatedTransactionPlan:
    plan = ValidatedTransactionPlan(
        canonical_chain_id=chain_id,
        network="ethereum:sepolia",
        wallet_id="wallet-test-01",
        signer_address="0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266",
        asset_in="eip155:11155111/native:ETH",
        asset_out="eip155:11155111/erc20:0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984",
        amount_in_base_units=amount_in,
        min_amount_out_base_units=95000000000000000,
        destination_id=destination,
        max_slippage_bps=slippage_bps,
        max_network_fee_base_units=fee,
        max_priority_fee_base_units=10000,
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
                "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D",
                "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",
            }
        ),
        allowed_tokens=frozenset(
            {
                "0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984",
                "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            }
        ),
        max_slippage_bps_limit=100,
        max_network_fee_limit=100000,
        live_mode_enabled=True,
        kill_switch_active=False,
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
        allowed_destinations=frozenset({"0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D"}),
        allowed_tokens=frozenset(),
        max_slippage_bps_limit=100,
        max_network_fee_limit=100000,
        live_mode_enabled=False,
        kill_switch_active=False,
    )
    plan = _sample_plan()
    signer = IsolatedSigner(policy)
    with pytest.raises(SignerPolicyViolation, match="live mode is not enabled"):
        signer.preview_transaction(plan, at=datetime.now(UTC))


def test_invariant_kill_switch_active_rejects() -> None:
    policy = SignerPolicy(
        allowed_chain_ids=frozenset({"eip155:11155111"}),
        allowed_destinations=frozenset({"0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D"}),
        allowed_tokens=frozenset(),
        max_slippage_bps_limit=100,
        max_network_fee_limit=100000,
        live_mode_enabled=True,
        kill_switch_active=True,
    )
    plan = _sample_plan()
    signer = IsolatedSigner(policy)
    with pytest.raises(SignerPolicyViolation, match="kill switch is active"):
        signer.preview_transaction(plan, at=datetime.now(UTC))


def test_invariant_simulation_failure_rejects() -> None:
    policy = _active_policy()
    plan = _sample_plan(simulation_success=False)
    signer = IsolatedSigner(policy)
    with pytest.raises(SignerPolicyViolation, match="simulation did not succeed"):
        signer.preview_transaction(plan, at=datetime.now(UTC))


def test_invariant_destination_not_allowlisted_rejects() -> None:
    policy = _active_policy()
    plan = _sample_plan(destination="0x1111111111111111111111111111111111111111")
    signer = IsolatedSigner(policy)
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
    signer = IsolatedSigner(policy)
    with pytest.raises(SignerTamperingError, match="plan hash mismatch"):
        signer.preview_transaction(tampered_plan, at=datetime.now(UTC))


def test_invariant_expired_deadline_rejects() -> None:
    policy = _active_policy()
    past = datetime.now(UTC) - timedelta(minutes=1)
    plan = _sample_plan(deadline=past)
    signer = IsolatedSigner(policy)
    with pytest.raises(SignerPolicyViolation, match="transaction plan has expired"):
        signer.preview_transaction(plan, at=datetime.now(UTC))


def test_invariant_slippage_excess_rejects() -> None:
    policy = _active_policy()
    plan = _sample_plan(slippage_bps=200)
    signer = IsolatedSigner(policy)
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
    signer = IsolatedSigner(policy)
    now = datetime.now(UTC)
    preview = signer.preview_transaction(plan, at=now)
    assert preview["plan_hash"] == plan.plan_hash
    assert preview["destination"] == plan.destination_id
    assert preview["amount_in"] == plan.amount_in_base_units


def test_evm_signing_execution() -> None:
    policy = _active_policy()
    signer = IsolatedSigner(policy)
    passphrase = "secure-password-123"
    record = create_keystore_record(
        wallet_id="wallet-test-01",
        address="0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266",
        chain_namespace="eip155",
        secret_bytes=_SYNTHETIC_EVM_KEY,
        passphrase=passphrase,
    )
    signer.register_keystore_record(record)
    plan = _sample_plan()
    payload = json.dumps(
        {
            "to": plan.destination_id,
            "chain_id": plan.canonical_chain_id,
            "amount": plan.amount_in_base_units,
            "from": plan.signer_address,
        }
    ).encode("utf-8")

    now = datetime.now(UTC)
    signed = signer.sign_transaction(plan, payload, passphrase, at=now)
    assert signed.plan_hash == plan.plan_hash
    assert len(signed.raw_transaction_hex) > 0
    assert len(signed.tx_hash) == 64
    assert signed.signed_at == now


def test_solana_signing_execution() -> None:
    policy = _active_policy()
    signer = IsolatedSigner(policy)
    passphrase = "solana-password-456"
    sol_address = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
    record = create_keystore_record(
        wallet_id="wallet-sol-01",
        address=sol_address,
        chain_namespace="solana",
        secret_bytes=_SYNTHETIC_SOL_KEY,
        passphrase=passphrase,
    )
    signer.register_keystore_record(record)
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

    payload = json.dumps(
        {
            "program_id": plan.destination_id,
            "canonical_chain_id": plan.canonical_chain_id,
            "amount": plan.amount_in_base_units,
            "signer": plan.signer_address,
        }
    ).encode("utf-8")

    now = datetime.now(UTC)
    signed = signer.sign_transaction(plan, payload, passphrase, at=now)
    assert signed.plan_hash == plan.plan_hash
    assert len(signed.tx_hash) == 64


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
