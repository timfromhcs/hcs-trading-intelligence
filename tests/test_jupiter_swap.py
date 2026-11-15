"""Tests for Solana Jupiter swap route verification and safety bounds."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from hcs_trader.jupiter_swap import (
    JUPITER_V6_PROGRAM_ID,
    JupiterRouteError,
    JupiterRouteQuote,
    JupiterSwapAdapter,
)

_SOL_MINT = "So11111111111111111111111111111111111111112"
_USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
_SUSPICIOUS_MINT = "BadMint111111111111111111111111111111111111"
_SUSPICIOUS_PROGRAM = "MaliciousProgram111111111111111111111111111"


def _sample_quote(
    in_mint: str = _SOL_MINT,
    out_mint: str = _USDC_MINT,
    in_amount: int = 1000000000,  # 1 SOL
    out_amount: int = 150000000,  # 150 USDC
    slippage_bps: int = 50,
    price_impact_pct: str = "0.05",
    age_seconds: int = 5,
    route_plan: list[dict[str, object]] | None = None,
) -> JupiterRouteQuote:
    now = datetime.now(UTC)
    return JupiterRouteQuote(
        input_mint=in_mint,
        output_mint=out_mint,
        in_amount=in_amount,
        out_amount=out_amount,
        other_amount_threshold=int(out_amount * 0.995),
        slippage_bps=slippage_bps,
        price_impact_pct=Decimal(price_impact_pct),
        route_plan=route_plan
        or [
            {
                "swapInfo": {
                    "ammKey": "amm-key-01",
                    "label": "Whirlpool",
                    "inputMint": in_mint,
                    "outputMint": out_mint,
                }
            }
        ],
        quoted_at=now - timedelta(seconds=age_seconds),
    )


def test_validate_route_success() -> None:
    adapter = JupiterSwapAdapter()
    quote = _sample_quote()
    now = datetime.now(UTC)

    # Should pass without raising
    adapter.validate_route(
        quote,
        max_slippage_bps=100,
        max_price_impact_pct=Decimal("0.50"),
        at=now,
    )


def test_validate_route_unapproved_input_mint() -> None:
    adapter = JupiterSwapAdapter()
    quote = _sample_quote(in_mint=_SUSPICIOUS_MINT)
    now = datetime.now(UTC)

    with pytest.raises(JupiterRouteError, match="Input mint.*not allowlisted"):
        adapter.validate_route(
            quote,
            max_slippage_bps=100,
            max_price_impact_pct=Decimal("0.50"),
            at=now,
        )


def test_validate_route_unapproved_output_mint() -> None:
    adapter = JupiterSwapAdapter()
    quote = _sample_quote(out_mint=_SUSPICIOUS_MINT)
    now = datetime.now(UTC)

    with pytest.raises(JupiterRouteError, match="Output mint.*not allowlisted"):
        adapter.validate_route(
            quote,
            max_slippage_bps=100,
            max_price_impact_pct=Decimal("0.50"),
            at=now,
        )


def test_validate_route_identical_mints() -> None:
    adapter = JupiterSwapAdapter()
    quote = _sample_quote(in_mint=_SOL_MINT, out_mint=_SOL_MINT)
    now = datetime.now(UTC)

    with pytest.raises(JupiterRouteError, match="identical"):
        adapter.validate_route(
            quote,
            max_slippage_bps=100,
            max_price_impact_pct=Decimal("0.50"),
            at=now,
        )


def test_validate_route_stale_quote() -> None:
    adapter = JupiterSwapAdapter(max_quote_age_seconds=30)
    quote = _sample_quote(age_seconds=60)
    now = datetime.now(UTC)

    with pytest.raises(JupiterRouteError, match="freshness window"):
        adapter.validate_route(
            quote,
            max_slippage_bps=100,
            max_price_impact_pct=Decimal("0.50"),
            at=now,
        )


def test_validate_route_excess_slippage() -> None:
    adapter = JupiterSwapAdapter()
    quote = _sample_quote(slippage_bps=150)
    now = datetime.now(UTC)

    with pytest.raises(JupiterRouteError, match="slippage.*exceeds policy cap"):
        adapter.validate_route(
            quote,
            max_slippage_bps=100,
            max_price_impact_pct=Decimal("0.50"),
            at=now,
        )


def test_validate_route_excess_price_impact() -> None:
    adapter = JupiterSwapAdapter()
    quote = _sample_quote(price_impact_pct="1.50")
    now = datetime.now(UTC)

    with pytest.raises(JupiterRouteError, match="price impact.*exceeds policy cap"):
        adapter.validate_route(
            quote,
            max_slippage_bps=100,
            max_price_impact_pct=Decimal("0.50"),
            at=now,
        )


def test_validate_route_unapproved_intermediate_token() -> None:
    adapter = JupiterSwapAdapter()
    bad_hop_plan: list[dict[str, object]] = [
        {
            "swapInfo": {
                "label": "Raydium",
                "inputMint": _SOL_MINT,
                "outputMint": _SUSPICIOUS_MINT,
            }
        }
    ]
    quote = _sample_quote(route_plan=bad_hop_plan)
    now = datetime.now(UTC)

    with pytest.raises(JupiterRouteError, match="unapproved output token"):
        adapter.validate_route(
            quote,
            max_slippage_bps=100,
            max_price_impact_pct=Decimal("0.50"),
            at=now,
        )


def test_inspect_swap_instructions_success() -> None:
    adapter = JupiterSwapAdapter()
    payload = {
        "instructions": [
            {"program_id": "ComputeBudget111111111111111111111111111111"},
            {"program_id": JUPITER_V6_PROGRAM_ID},
            {"program_id": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
        ]
    }
    # Should pass without raising
    adapter.inspect_swap_instructions(payload)


def test_inspect_swap_instructions_unapproved_program() -> None:
    adapter = JupiterSwapAdapter()
    payload = {
        "instructions": [
            {"program_id": JUPITER_V6_PROGRAM_ID},
            {"program_id": _SUSPICIOUS_PROGRAM},
        ]
    }
    with pytest.raises(JupiterRouteError, match="unapproved program"):
        adapter.inspect_swap_instructions(payload)


def test_inspect_swap_instructions_empty() -> None:
    adapter = JupiterSwapAdapter()
    with pytest.raises(JupiterRouteError, match="contains no instructions"):
        adapter.inspect_swap_instructions({"instructions": []})
