"""Solana Jupiter swap route verification and instruction inspection.

Jupiter routing is deployed on Solana mainnet only. The instruction inspector
accepts the Jupiter Swap API V2 ``/build`` response
(https://developers.jup.ag/docs/swap/build, base ``https://api.jup.ag/swap/v2``)
and the legacy flat ``instructions`` payload.
"""

import base64
import binascii
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

# Public on-chain program addresses. The S105 suppressions below are for names
# containing "TOKEN"; these values are not secrets.
JUPITER_V6_PROGRAM_ID = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
SPL_TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"  # noqa: S105
SYSTEM_PROGRAM_ID = "11111111111111111111111111111111"
COMPUTE_BUDGET_PROGRAM_ID = "ComputeBudget111111111111111111111111111111"
ASSOCIATED_TOKEN_PROGRAM_ID = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"  # noqa: S105
# Token-2022 is deliberately NOT allowlisted: its extensions (transfer hooks,
# transfer fees, permanent delegates) need a separate review.
TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"  # noqa: S105

# Bump this version whenever the program allowlist or its per-program
# instruction rules change.
JUPITER_PROGRAM_ALLOWLIST_VERSION = "2026-11-15.v2"
ALLOWED_JUPITER_PROGRAMS = frozenset(
    {
        JUPITER_V6_PROGRAM_ID,
        SPL_TOKEN_PROGRAM_ID,
        SYSTEM_PROGRAM_ID,
        COMPUTE_BUDGET_PROGRAM_ID,
        # Allowed only for CreateIdempotent with owner == signer; see
        # JupiterSwapAdapter._check_associated_token_instruction.
        ASSOCIATED_TOKEN_PROGRAM_ID,
    }
)

# Associated Token Account program instruction discriminators: 0 = Create,
# 1 = CreateIdempotent, 2 = RecoverNested. Only CreateIdempotent is accepted.
_ATA_CREATE_IDEMPOTENT_DATA = b"\x01"
_ATA_ACCOUNT_COUNT = 6

# V2 /build instruction fields, in transaction order.
_BUILD_LIST_FIELDS = ("computeBudgetInstructions", "setupInstructions")
_BUILD_TRAILING_LIST_FIELDS = ("otherInstructions",)
_BUILD_KNOWN_INSTRUCTION_FIELDS = frozenset(
    {
        "computeBudgetInstructions",
        "setupInstructions",
        "swapInstruction",
        "cleanupInstruction",
        "otherInstructions",
        "tipInstruction",
    }
)

# Verified Solana mainnet-beta token mint allowlist. Jupiter has no devnet
# deployment, so there is no devnet routing.
ALLOWED_SOLANA_MINTS = frozenset(
    {
        "So11111111111111111111111111111111111111112",  # Wrapped SOL / Native SOL
        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC (Mainnet)
        "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT (Mainnet)
    }
)


class JupiterRouteError(Exception):
    """The Jupiter swap route violates safety policy or allowlists."""


def _validate_api_instruction(field: str, inst: Any) -> dict[str, Any]:
    """Validate a V2 ``ApiInstruction`` (programId, accounts, base64 data)."""
    if not isinstance(inst, dict):
        raise JupiterRouteError(f"{field} entry must be an instruction object")
    program_id = inst.get("programId")
    if not isinstance(program_id, str) or not program_id:
        raise JupiterRouteError(f"{field} instruction is missing programId")
    accounts = inst.get("accounts")
    if not isinstance(accounts, list):
        raise JupiterRouteError(f"{field} instruction accounts must be a list")
    for account in accounts:
        if (
            not isinstance(account, dict)
            or not isinstance(account.get("pubkey"), str)
            or not isinstance(account.get("isSigner"), bool)
            or not isinstance(account.get("isWritable"), bool)
        ):
            raise JupiterRouteError(f"{field} instruction has a malformed account")
    if not isinstance(inst.get("data"), str):
        raise JupiterRouteError(f"{field} instruction data must be a base64 string")
    return inst


def normalize_build_instructions(response: dict[str, Any]) -> list[dict[str, Any]]:
    """Map a Jupiter Swap API V2 ``/build`` response to ordered instructions.

    Order: computeBudget, setup, swap, cleanup, other, tip. Unknown top-level
    instruction fields are rejected so new upstream fields fail closed.
    """
    if not isinstance(response, dict):
        raise JupiterRouteError("Build response must be an object")
    for key in response:
        if "instruction" in key.lower() and key not in _BUILD_KNOWN_INSTRUCTION_FIELDS:
            raise JupiterRouteError(f"Unknown instruction field '{key}' in build")

    ordered: list[dict[str, Any]] = []

    def _extend_list(field: str) -> None:
        value = response.get(field)
        if not isinstance(value, list):
            raise JupiterRouteError(f"{field} must be a list")
        ordered.extend(_validate_api_instruction(field, inst) for inst in value)

    def _append_optional(field: str) -> None:
        value = response.get(field)
        if value is not None:
            ordered.append(_validate_api_instruction(field, value))

    for field in _BUILD_LIST_FIELDS:
        _extend_list(field)
    if response.get("swapInstruction") is None:
        raise JupiterRouteError("Build response is missing swapInstruction")
    ordered.append(
        _validate_api_instruction("swapInstruction", response["swapInstruction"])
    )
    _append_optional("cleanupInstruction")
    for field in _BUILD_TRAILING_LIST_FIELDS:
        _extend_list(field)
    _append_optional("tipInstruction")
    return ordered


@dataclass(frozen=True, slots=True)
class JupiterRouteQuote:
    input_mint: str
    output_mint: str
    in_amount: int
    out_amount: int
    other_amount_threshold: int
    slippage_bps: int
    price_impact_pct: Decimal
    route_plan: list[dict[str, Any]]
    quoted_at: datetime


class JupiterSwapAdapter:
    """Bounded Solana Jupiter swap route validator and instruction inspector."""

    def __init__(
        self,
        allowed_mints: frozenset[str] = ALLOWED_SOLANA_MINTS,
        allowed_programs: frozenset[str] = ALLOWED_JUPITER_PROGRAMS,
        max_quote_age_seconds: int = 30,
    ) -> None:
        self.allowed_mints = allowed_mints
        self.allowed_programs = allowed_programs
        self.max_quote_age_seconds = max_quote_age_seconds

    def validate_route(
        self,
        quote: JupiterRouteQuote,
        max_slippage_bps: int,
        max_price_impact_pct: Decimal,
        at: datetime,
    ) -> None:
        """Validate route mints, slippage, price impact, and freshness."""
        if quote.input_mint not in self.allowed_mints:
            raise JupiterRouteError(
                f"Input mint '{quote.input_mint}' is not allowlisted"
            )
        if quote.output_mint not in self.allowed_mints:
            raise JupiterRouteError(
                f"Output mint '{quote.output_mint}' is not allowlisted"
            )
        if quote.input_mint == quote.output_mint:
            raise JupiterRouteError("Input and output mints cannot be identical")
        if quote.in_amount <= 0 or quote.out_amount <= 0:
            raise JupiterRouteError("Swap amounts must be strictly positive")
        if quote.other_amount_threshold <= 0:
            raise JupiterRouteError(
                "Minimum received amount threshold must be positive"
            )

        # Freshness check
        age = at - quote.quoted_at
        if age > timedelta(seconds=self.max_quote_age_seconds):
            raise JupiterRouteError(
                f"Quote age {age.total_seconds():.1f}s exceeds freshness window "
                f"{self.max_quote_age_seconds}s"
            )

        # Slippage check
        if quote.slippage_bps > max_slippage_bps:
            raise JupiterRouteError(
                f"Route slippage {quote.slippage_bps} bps exceeds policy cap "
                f"{max_slippage_bps} bps"
            )

        # Price impact check
        if quote.price_impact_pct > max_price_impact_pct:
            raise JupiterRouteError(
                f"Route price impact {quote.price_impact_pct:.2f}% exceeds policy cap "
                f"{max_price_impact_pct:.2f}%"
            )

        # Route plan program check
        for step in quote.route_plan:
            swap_info = step.get("swapInfo", {})
            amm_label = swap_info.get("label", "unknown")
            input_token = swap_info.get("inputMint")
            output_token = swap_info.get("outputMint")
            if input_token and input_token not in self.allowed_mints:
                raise JupiterRouteError(
                    f"Route step AMM '{amm_label}' uses unapproved "
                    f"input token '{input_token}'"
                )
            if output_token and output_token not in self.allowed_mints:
                raise JupiterRouteError(
                    f"Route step AMM '{amm_label}' uses unapproved "
                    f"output token '{output_token}'"
                )

    def inspect_swap_instructions(
        self,
        transaction_payload: dict[str, Any],
        *,
        signer: str | None = None,
    ) -> None:
        """Inspect all instructions and program invocations in the swap payload.

        Accepts a Jupiter Swap API V2 ``/build`` response or a legacy payload
        with a flat ``instructions`` list. ``signer`` is the expected hot-wallet
        address; it is required when an Associated Token Account instruction
        is present.
        """
        if "swapInstruction" in transaction_payload:
            instructions = normalize_build_instructions(transaction_payload)
            swap_program = transaction_payload["swapInstruction"]["programId"]
            if swap_program != JUPITER_V6_PROGRAM_ID:
                raise JupiterRouteError(
                    f"swapInstruction invokes '{swap_program}', not the Jupiter program"
                )
        else:
            instructions = transaction_payload.get("instructions", [])
        if not instructions:
            raise JupiterRouteError("Transaction payload contains no instructions")

        for idx, inst in enumerate(instructions):
            program_id = inst.get("program_id") or inst.get("programId")
            if not program_id:
                raise JupiterRouteError(
                    f"Instruction at index {idx} is missing program identifier"
                )
            if program_id not in self.allowed_programs:
                raise JupiterRouteError(
                    f"Instruction at index {idx} invokes unapproved "
                    f"program '{program_id}'"
                )
            if program_id == ASSOCIATED_TOKEN_PROGRAM_ID:
                self._check_associated_token_instruction(idx, inst, signer)

    def _check_associated_token_instruction(
        self, idx: int, inst: dict[str, Any], signer: str | None
    ) -> None:
        """Allow only ATA CreateIdempotent for the signer's own allowlisted mint."""
        if not signer:
            raise JupiterRouteError(
                f"Instruction at index {idx}: associated token instruction "
                "requires an expected signer address"
            )
        raw_data = inst.get("data")
        if not isinstance(raw_data, str):
            raise JupiterRouteError(
                f"Instruction at index {idx}: associated token instruction "
                "data must be a base64 string"
            )
        try:
            data = base64.b64decode(raw_data, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise JupiterRouteError(
                f"Instruction at index {idx}: invalid base64 instruction data"
            ) from exc
        if data != _ATA_CREATE_IDEMPOTENT_DATA:
            raise JupiterRouteError(
                f"Instruction at index {idx}: only associated token "
                "CreateIdempotent is allowed"
            )
        accounts = inst.get("accounts")
        if not isinstance(accounts, list) or len(accounts) != _ATA_ACCOUNT_COUNT:
            raise JupiterRouteError(
                f"Instruction at index {idx}: associated token instruction "
                f"must have exactly {_ATA_ACCOUNT_COUNT} accounts"
            )
        keys = [a.get("pubkey") if isinstance(a, dict) else None for a in accounts]
        payer, _ata, owner, mint, system_program, token_program = keys
        if payer != signer:
            raise JupiterRouteError(
                f"Instruction at index {idx}: associated token payer must be the signer"
            )
        if owner != signer:
            raise JupiterRouteError(
                f"Instruction at index {idx}: associated token owner must be the signer"
            )
        if mint not in self.allowed_mints:
            raise JupiterRouteError(
                f"Instruction at index {idx}: associated token mint "
                f"'{mint}' is not allowlisted"
            )
        if system_program != SYSTEM_PROGRAM_ID:
            raise JupiterRouteError(
                f"Instruction at index {idx}: unexpected system program account"
            )
        if token_program != SPL_TOKEN_PROGRAM_ID:
            raise JupiterRouteError(
                f"Instruction at index {idx}: associated token program uses "
                f"unapproved token program '{token_program}'"
            )
