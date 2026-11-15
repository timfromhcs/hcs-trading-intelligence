"""Domain types and invariants for validated transaction plans and signing."""

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

_IDENTIFIER = re.compile(r"^[a-zA-Z0-9_\-:.]{1,128}$")


class SignerError(Exception):
    """Base exception for isolated signing and keystore operations."""


class SignerPolicyViolation(SignerError):
    """The plan violates an immutable signing policy rule."""


class SignerTamperingError(SignerError):
    """The plan payload, hash or transaction bytes do not match."""


class KeystoreError(SignerError):
    """Keystore encryption, decryption or access failed."""


@dataclass(frozen=True, slots=True)
class ValidatedTransactionPlan:
    canonical_chain_id: str
    network: str
    wallet_id: str
    signer_address: str
    asset_in: str
    asset_out: str
    amount_in_base_units: int
    min_amount_out_base_units: int
    destination_id: str
    max_slippage_bps: int
    max_network_fee_base_units: int
    max_priority_fee_base_units: int
    idempotency_key: str
    risk_decision_id: str
    risk_policy_version: str
    simulation_success: bool
    simulation_block_reference: str
    token_in_mint: str | None = None
    token_out_mint: str | None = None
    deadline: datetime | None = None
    nonce: int | None = None
    plan_hash: str = ""

    def __post_init__(self) -> None:
        if not _IDENTIFIER.match(self.canonical_chain_id):
            raise ValueError("Invalid canonical chain ID")
        if not _IDENTIFIER.match(self.network):
            raise ValueError("Invalid network identifier")
        if not _IDENTIFIER.match(self.wallet_id):
            raise ValueError("Invalid wallet ID")
        if not _IDENTIFIER.match(self.destination_id):
            raise ValueError("Invalid destination ID")
        if not _IDENTIFIER.match(self.idempotency_key):
            raise ValueError("Invalid idempotency key")
        if not _IDENTIFIER.match(self.risk_decision_id):
            raise ValueError("Invalid risk decision ID")
        if not _IDENTIFIER.match(self.risk_policy_version):
            raise ValueError("Invalid risk policy version")
        if self.amount_in_base_units <= 0:
            raise ValueError("amount_in_base_units must be positive")
        if self.min_amount_out_base_units <= 0:
            raise ValueError("min_amount_out_base_units must be positive")
        if not 0 <= self.max_slippage_bps <= 5000:
            raise ValueError("max_slippage_bps must be between 0 and 5000 (50%)")
        if self.max_network_fee_base_units < 0:
            raise ValueError("max_network_fee_base_units cannot be negative")
        if self.max_priority_fee_base_units < 0:
            raise ValueError("max_priority_fee_base_units cannot be negative")
        if self.nonce is not None and self.nonce < 0:
            raise ValueError("nonce cannot be negative")
        if self.deadline is not None and self.deadline.utcoffset() is None:
            raise ValueError("deadline must have a UTC offset")

    def canonical_dict(self) -> dict[str, Any]:
        """Serialize plan fields deterministically excluding plan_hash."""
        data = asdict(self)
        data.pop("plan_hash", None)
        if self.deadline is not None:
            data["deadline"] = self.deadline.astimezone(UTC).isoformat()
        return data

    def compute_plan_hash(self) -> str:
        """Calculate canonical SHA-256 hash over the plan content."""
        canonical_json = json.dumps(
            self.canonical_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

    def with_hash(self) -> "ValidatedTransactionPlan":
        """Return a copy of the plan with computed plan_hash populated."""
        return ValidatedTransactionPlan(
            canonical_chain_id=self.canonical_chain_id,
            network=self.network,
            wallet_id=self.wallet_id,
            signer_address=self.signer_address,
            asset_in=self.asset_in,
            asset_out=self.asset_out,
            amount_in_base_units=self.amount_in_base_units,
            min_amount_out_base_units=self.min_amount_out_base_units,
            destination_id=self.destination_id,
            max_slippage_bps=self.max_slippage_bps,
            max_network_fee_base_units=self.max_network_fee_base_units,
            max_priority_fee_base_units=self.max_priority_fee_base_units,
            idempotency_key=self.idempotency_key,
            risk_decision_id=self.risk_decision_id,
            risk_policy_version=self.risk_policy_version,
            simulation_success=self.simulation_success,
            simulation_block_reference=self.simulation_block_reference,
            token_in_mint=self.token_in_mint,
            token_out_mint=self.token_out_mint,
            deadline=self.deadline,
            nonce=self.nonce,
            plan_hash=self.compute_plan_hash(),
        )


@dataclass(frozen=True, slots=True)
class SignerPolicy:
    """Deterministic policy envelope evaluated independently by the isolated signer."""

    allowed_chain_ids: frozenset[str]
    allowed_destinations: frozenset[str]
    allowed_tokens: frozenset[str]
    max_slippage_bps_limit: int
    max_network_fee_limit: int
    live_mode_enabled: bool = False
    kill_switch_active: bool = False

    def validate_plan(self, plan: ValidatedTransactionPlan, at: datetime) -> None:
        """Assert that the plan strictly meets policy constraints before signing."""
        if self.kill_switch_active:
            raise SignerPolicyViolation("Signing rejected: kill switch is active")
        if not self.live_mode_enabled:
            raise SignerPolicyViolation("Signing rejected: live mode is not enabled")
        if not plan.simulation_success:
            raise SignerPolicyViolation("Signing rejected: simulation did not succeed")
        if plan.canonical_chain_id not in self.allowed_chain_ids:
            raise SignerPolicyViolation(
                f"Signing rejected: chain '{plan.canonical_chain_id}' "
                "is not allowlisted"
            )
        if plan.destination_id not in self.allowed_destinations:
            raise SignerPolicyViolation(
                f"Signing rejected: destination '{plan.destination_id}' "
                "is not allowlisted"
            )
        if plan.token_in_mint and plan.token_in_mint not in self.allowed_tokens:
            raise SignerPolicyViolation(
                f"Signing rejected: token_in '{plan.token_in_mint}' is not allowlisted"
            )
        if plan.token_out_mint and plan.token_out_mint not in self.allowed_tokens:
            raise SignerPolicyViolation(
                f"Signing rejected: token_out '{plan.token_out_mint}' "
                "is not allowlisted"
            )
        if plan.max_slippage_bps > self.max_slippage_bps_limit:
            raise SignerPolicyViolation(
                f"Signing rejected: slippage {plan.max_slippage_bps} bps "
                f"exceeds policy limit {self.max_slippage_bps_limit} bps"
            )
        if plan.max_network_fee_base_units > self.max_network_fee_limit:
            raise SignerPolicyViolation(
                f"Signing rejected: network fee {plan.max_network_fee_base_units} "
                f"exceeds policy limit {self.max_network_fee_limit}"
            )
        if plan.deadline is not None:
            if at.utcoffset() is None:
                raise ValueError("Evaluation time 'at' must have a UTC offset")
            if at > plan.deadline:
                raise SignerPolicyViolation(
                    "Signing rejected: transaction plan has expired"
                )
        computed_hash = plan.compute_plan_hash()
        if plan.plan_hash != computed_hash:
            raise SignerTamperingError(
                f"Signing rejected: plan hash mismatch "
                f"(declared: {plan.plan_hash}, computed: {computed_hash})"
            )


@dataclass(frozen=True, slots=True)
class SignedTransaction:
    plan_hash: str
    raw_transaction_hex: str
    tx_hash: str
    signed_at: datetime
