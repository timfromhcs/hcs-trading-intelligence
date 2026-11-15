"""Solana Jupiter swap route verification and instruction inspection."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

# Versioned Jupiter program allowlist
JUPITER_V6_PROGRAM_ID = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
ALLOWED_JUPITER_PROGRAMS = frozenset(
    {
        JUPITER_V6_PROGRAM_ID,
        "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",  # SPL Token Program
        "11111111111111111111111111111111",  # System Program
        "ComputeBudget111111111111111111111111111111",  # Compute Budget Program
    }
)

# Verified mainnet/devnet token mint allowlist
ALLOWED_SOLANA_MINTS = frozenset(
    {
        "So11111111111111111111111111111111111111112",  # Wrapped SOL / Native SOL
        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC (Mainnet)
        "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT (Mainnet)
    }
)


class JupiterRouteError(Exception):
    """The Jupiter swap route violates safety policy or allowlists."""


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

    def inspect_swap_instructions(self, transaction_payload: dict[str, Any]) -> None:
        """Inspect all instructions and program invocations in the swap payload."""
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
