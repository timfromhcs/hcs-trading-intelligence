"""Cross-provider quote deviation check (DEGRADES rather than guessing)."""

from dataclasses import dataclass
from decimal import Decimal

from hcs_trader.domain.market_data import DataQuality, Quote


@dataclass(frozen=True, slots=True)
class CrossCheck:
    quality: DataQuality
    deviation: Decimal | None
    reason: str | None


def cross_check(
    primary: Quote | None,
    secondary: Quote | None,
    *,
    max_deviation: Decimal = Decimal("0.005"),
) -> CrossCheck:
    """Compare mid prices; a missing second source leaves data DEGRADED."""
    if primary is None:
        return CrossCheck(DataQuality.MISSING, None, "primary_missing")
    if secondary is None:
        return CrossCheck(DataQuality.DEGRADED, None, "no_second_source")
    if primary.market.base != secondary.market.base or (
        primary.market.quote != secondary.market.quote
    ):
        return CrossCheck(DataQuality.INVALID, None, "market_mismatch")
    if primary.source == secondary.source:
        return CrossCheck(DataQuality.DEGRADED, None, "not_independent")
    mid_a = (primary.bid + primary.ask) / 2
    mid_b = (secondary.bid + secondary.ask) / 2
    deviation = abs(mid_a - mid_b) / min(mid_a, mid_b)
    if deviation > max_deviation:
        return CrossCheck(DataQuality.DEGRADED, deviation, "sources_disagree")
    return CrossCheck(DataQuality.FRESH, deviation, None)
