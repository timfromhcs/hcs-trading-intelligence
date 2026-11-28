"""Trailing-only market regime features for deterministic research.

Every value at index ``i`` is computed from closes up to and including ``i``.
No future candle can influence an earlier feature; a regression test appends a
different future path and proves prior values are unchanged.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

_BPS = Decimal(10_000)


class InvalidCloses(ValueError):
    """Close prices must be finite, positive and ordered chronologically."""


@dataclass(frozen=True, slots=True)
class RegimePoint:
    index: int
    direction: Literal["up", "down", "flat", "mixed"]
    slope_bps: Decimal
    warm: bool


def validate_closes(closes: Sequence[Decimal]) -> None:
    for value in closes:
        if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
            raise InvalidCloses("Close prices must be positive finite Decimals")


def regime_series(
    closes: Sequence[Decimal], *, lookback: int = 20, flat_bps: Decimal = Decimal("10")
) -> tuple[RegimePoint, ...]:
    """Compute a trailing slope regime over a fixed lookback window."""
    if lookback < 2 or lookback > 500:
        raise ValueError("lookback must be between 2 and 500")
    if not isinstance(flat_bps, Decimal) or not flat_bps.is_finite() or flat_bps < 0:
        raise InvalidCloses("flat_bps must be a non-negative finite Decimal")
    validate_closes(closes)
    points: list[RegimePoint] = []
    for index in range(len(closes)):
        start = max(0, index - lookback + 1)
        window = closes[start : index + 1]
        warm = len(window) == lookback
        if not warm:
            points.append(RegimePoint(index, "mixed", Decimal(0), False))
            continue
        slope = (window[-1] - window[0]) / window[0] * _BPS
        if slope > flat_bps:
            direction: Literal["up", "down", "flat", "mixed"] = "up"
        elif slope < -flat_bps:
            direction = "down"
        else:
            direction = "flat"
        points.append(RegimePoint(index, direction, slope, True))
    return tuple(points)
