"""Isolated signer with deterministic policy checks and independent decoding."""

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, ed25519

from hcs_trader.domain.signing import (
    ApprovedRiskDecision,
    KeystoreError,
    SignedPlanConflict,
    SignedTransaction,
    SignerPolicy,
    SignerPolicyViolation,
    SignerTamperingError,
    SigningUnsupported,
    ValidatedTransactionPlan,
    signing_capabilities,
)
from hcs_trader.evm_signing import (
    EvmSigningPolicy,
    decode_evm_payload,
    sign_evm_payload,
)
from hcs_trader.keystore import decrypt_keystore_record


def decode_and_verify_payload(
    plan: ValidatedTransactionPlan, payload_bytes: bytes
) -> None:
    """Independently decode unsigned payload and verify consistency with plan."""
    try:
        data = json.loads(payload_bytes.decode("utf-8"))
    except Exception as err:
        raise SignerTamperingError(
            f"Payload cannot be decoded as valid JSON: {err}"
        ) from err

    if not isinstance(data, dict):
        raise SignerTamperingError("Payload must decode to a dictionary")

    # Verify destination/recipient
    payload_dest = data.get("to") or data.get("destination") or data.get("program_id")
    if payload_dest != plan.destination_id:
        raise SignerTamperingError(
            f"Payload destination '{payload_dest}' does not match "
            f"plan '{plan.destination_id}'"
        )

    # Verify chain
    payload_chain = data.get("chain_id") or data.get("canonical_chain_id")
    if payload_chain != plan.canonical_chain_id:
        raise SignerTamperingError(
            f"Payload chain '{payload_chain}' does not match "
            f"plan '{plan.canonical_chain_id}'"
        )

    # Verify input amount
    payload_amount = data.get("amount") or data.get("amount_in") or data.get("value")
    if payload_amount != plan.amount_in_base_units:
        raise SignerTamperingError(
            f"Payload amount '{payload_amount}' does not match "
            f"plan '{plan.amount_in_base_units}'"
        )

    # Verify sender/signer address
    payload_from = data.get("from") or data.get("signer")
    if payload_from and payload_from != plan.signer_address:
        raise SignerTamperingError(
            f"Payload signer '{payload_from}' does not match "
            f"plan '{plan.signer_address}'"
        )


PauseState = Callable[[], bool]
RiskDecisionLookup = Callable[[str], ApprovedRiskDecision | None]


class SignedPlanLedger(Protocol):
    """Durable record of signed plans; ``commit`` consumes the risk decision."""

    def is_plan_signed(self, idempotency_key: str) -> bool: ...

    def commit_signed_plan(
        self,
        *,
        idempotency_key: str,
        plan_hash: str,
        risk_decision_id: str,
        tx_hash: str,
        signed_at: datetime,
    ) -> None: ...


class IsolatedSigner:
    """Isolated signing boundary holding keystore records and immutable policy.

    The signer reads the persisted execution pause and the persisted risk
    decision through injected read-only callables at signing time, so a pause
    or a revoked decision recorded after construction still blocks signing.

    EVM (``eip155``) signing is chain-valid EIP-1559 (see ``evm_signing``) and
    is independent of ``allow_test_only_signing``. The UTXO (``bip122``) path is
    a placeholder that does not produce chain-valid transactions. It raises
    ``SigningUnsupported`` unless ``allow_test_only_signing`` is explicitly set
    (tests only).
    """

    def __init__(
        self,
        policy: SignerPolicy,
        *,
        pause_state: PauseState,
        risk_decision_lookup: RiskDecisionLookup,
        allow_test_only_signing: bool = False,
        signed_plans: SignedPlanLedger | None = None,
        evm_policy: EvmSigningPolicy | None = None,
    ) -> None:
        self.policy = policy
        self.evm_policy = evm_policy or EvmSigningPolicy()
        self._ledger = signed_plans
        self._pause_state = pause_state
        self._risk_decision_lookup = risk_decision_lookup
        self._allow_test_only_signing = allow_test_only_signing
        self._keystore: dict[tuple[str, str], dict[str, Any]] = {}
        self._signed_idempotency_keys: set[str] = set()

    def _assert_not_paused(self) -> None:
        try:
            paused = self._pause_state()
        except Exception as err:
            raise SignerPolicyViolation(
                "Signing rejected: execution pause state unavailable"
            ) from err
        if paused is not False:
            raise SignerPolicyViolation("Signing rejected: execution is paused")

    def _assert_risk_decision(self, plan: ValidatedTransactionPlan) -> None:
        try:
            decision = self._risk_decision_lookup(plan.risk_decision_id)
        except Exception as err:
            raise SignerPolicyViolation(
                f"Signing rejected: risk decision '{plan.risk_decision_id}' "
                "lookup failed"
            ) from err
        if decision is None:
            raise SignerPolicyViolation(
                f"Signing rejected: risk decision '{plan.risk_decision_id}' not found"
            )
        if decision.decision_id != plan.risk_decision_id:
            raise SignerTamperingError(
                f"Signing rejected: risk decision id '{decision.decision_id}' "
                f"does not match plan '{plan.risk_decision_id}'"
            )
        if decision.allowed is not True:
            raise SignerPolicyViolation(
                f"Signing rejected: risk decision '{plan.risk_decision_id}' "
                "is not approved"
            )
        if decision.plan_hash != plan.plan_hash:
            raise SignerTamperingError(
                "Signing rejected: risk decision plan hash does not match plan"
            )

    def _already_signed(self, idempotency_key: str) -> bool:
        if self._ledger is None:
            return idempotency_key in self._signed_idempotency_keys
        try:
            return self._ledger.is_plan_signed(idempotency_key)
        except Exception as err:
            raise SignerPolicyViolation(
                "Signing rejected: signed-plan ledger unavailable"
            ) from err

    def _record_signed(
        self, plan: ValidatedTransactionPlan, tx_hash: str, at: datetime
    ) -> None:
        if self._ledger is None:
            self._signed_idempotency_keys.add(plan.idempotency_key)
            return
        try:
            self._ledger.commit_signed_plan(
                idempotency_key=plan.idempotency_key,
                plan_hash=plan.plan_hash,
                risk_decision_id=plan.risk_decision_id,
                tx_hash=tx_hash,
                signed_at=at,
            )
        except SignedPlanConflict:
            raise
        except Exception as err:
            raise SignerPolicyViolation(
                "Signing rejected: signature could not be recorded durably"
            ) from err

    def _assert_namespace_supported(self, chain_ns: str) -> None:
        capability = signing_capabilities().get(chain_ns)
        if capability is None:
            raise SigningUnsupported(
                f"Signing rejected: no signer exists for namespace '{chain_ns}'"
            )
        if capability.requires_test_only_flag and not self._allow_test_only_signing:
            raise SigningUnsupported(
                f"Signing rejected: '{chain_ns}' signing is a test-only "
                f"placeholder and not chain-valid. {capability.note}"
            )

    def register_keystore_record(self, record: dict[str, Any]) -> None:
        """Register an encrypted keystore record."""
        key = (record["chain_namespace"], record["address"])
        self._keystore[key] = record

    def has_key(self, chain_namespace: str, address: str) -> bool:
        """Check if an encrypted key exists for the given namespace and address."""
        return (chain_namespace, address) in self._keystore

    def preview_transaction(
        self, plan: ValidatedTransactionPlan, at: datetime
    ) -> dict[str, Any]:
        """Generate human-readable preview and verify policy without signing."""
        self.policy.validate_plan(plan, at)
        return {
            "plan_hash": plan.plan_hash,
            "chain": plan.canonical_chain_id,
            "network": plan.network,
            "wallet_id": plan.wallet_id,
            "signer_address": plan.signer_address,
            "asset_in": plan.asset_in,
            "asset_out": plan.asset_out,
            "amount_in": plan.amount_in_base_units,
            "min_amount_out": plan.min_amount_out_base_units,
            "destination": plan.destination_id,
            "max_slippage_bps": plan.max_slippage_bps,
            "max_network_fee": plan.max_network_fee_base_units,
            "idempotency_key": plan.idempotency_key,
            "simulation_success": plan.simulation_success,
            "simulation_block_reference": plan.simulation_block_reference,
            "risk_decision_id": plan.risk_decision_id,
            "risk_policy_version": plan.risk_policy_version,
            "verified_at": at.astimezone(UTC).isoformat(),
        }

    def sign_transaction(
        self,
        plan: ValidatedTransactionPlan,
        unsigned_payload: bytes,
        passphrase: str,
        at: datetime,
    ) -> SignedTransaction:
        """Validate policy, decode payload, and produce a signed transaction."""
        # 1. Persisted kill switch / execution pause (read at signing time)
        self._assert_not_paused()

        # 2. Deterministic policy check
        self.policy.validate_plan(plan, at)

        # 3. Bind to a persisted, approved risk decision for this exact plan
        self._assert_risk_decision(plan)

        # 4. Refuse to sign the same idempotency key twice (durable when a
        #    ledger is injected, so a restart cannot re-sign a plan)
        if self._already_signed(plan.idempotency_key):
            raise SignerPolicyViolation(
                f"Signing rejected: idempotency key '{plan.idempotency_key}' "
                "was already signed"
            )

        # 5. Only namespaces with an available signer (placeholders gated)
        chain_ns = plan.canonical_chain_id.split(":", 1)[0]
        self._assert_namespace_supported(chain_ns)

        # 6. Independent payload decoding & consistency check
        if chain_ns == "eip155":
            return self._sign_evm(plan, unsigned_payload, passphrase, at)
        decode_and_verify_payload(plan, unsigned_payload)

        # 7. Key retrieval
        record_key = (chain_ns, plan.signer_address)
        record = self._keystore.get(record_key)
        if record is None:
            raise KeystoreError(
                f"No keystore record registered for address {plan.signer_address} "
                f"in namespace {chain_ns}"
            )

        # 8. Narrow decryption and signing
        raw_secret = decrypt_keystore_record(record, passphrase)
        try:
            if chain_ns == "solana":
                # Ed25519 signing for Solana
                if len(raw_secret) != 32:
                    raise KeystoreError("Solana private key must be 32 raw seed bytes")
                priv_key = ed25519.Ed25519PrivateKey.from_private_bytes(raw_secret)
                signature = priv_key.sign(unsigned_payload)
            else:
                # TEST-ONLY PLACEHOLDER: SECP256K1 ECDSA over SHA-256 of the
                # JSON envelope. Not keccak/RLP (EVM) nor sighash/PSBT (UTXO);
                # reachable only with allow_test_only_signing=True.
                if len(raw_secret) != 32:
                    raise KeystoreError("SECP256K1 private key must be 32 bytes")
                ec_priv = ec.derive_private_key(
                    int.from_bytes(raw_secret, "big"), ec.SECP256K1()
                )
                signature = ec_priv.sign(unsigned_payload, ec.ECDSA(hashes.SHA256()))
        finally:
            # Overwrite local reference to assist garbage collection
            del raw_secret

        raw_tx_hex = unsigned_payload.hex() + signature.hex()
        tx_hash = hashlib.sha256(bytes.fromhex(raw_tx_hex)).hexdigest()
        # 9. Atomically consume the risk decision and persist the signature
        #    record before the signed bytes leave the signer.
        self._record_signed(plan, tx_hash, at)

        return SignedTransaction(
            plan_hash=plan.plan_hash,
            raw_transaction_hex=raw_tx_hex,
            tx_hash=tx_hash,
            signed_at=at,
        )

    def _sign_evm(
        self,
        plan: ValidatedTransactionPlan,
        unsigned_payload: bytes,
        passphrase: str,
        at: datetime,
    ) -> SignedTransaction:
        """Chain-valid EIP-1559 signing; decode is verified before key access.

        Runs after the pause, policy, risk-decision and idempotency checks, so a
        repeated idempotency key is always refused (no cached replay) and every
        call decodes the payload independently.
        """
        decoded = decode_evm_payload(plan, unsigned_payload, self.evm_policy)
        record = self._keystore.get(("eip155", plan.signer_address))
        if record is None:
            raise KeystoreError(
                f"No keystore record registered for address {plan.signer_address} "
                "in namespace eip155"
            )
        raw_secret = decrypt_keystore_record(record, passphrase)
        try:
            signed = sign_evm_payload(plan, decoded, raw_secret, at)
        finally:
            del raw_secret
        self._record_signed(plan, signed.tx_hash, at)
        return signed
