"""Regime features must never see a future candle."""

from decimal import Decimal

import pytest

from hcs_trader.regime import InvalidCloses, regime_series


def _series(values: list[str]) -> list[Decimal]:
    return [Decimal(value) for value in values]


def test_warmup_and_direction() -> None:
    points = regime_series(_series(["100"] * 19 + ["110"]), lookback=20)
    assert len(points) == 20
    assert all(not point.warm for point in points[:19])
    assert points[-1].warm
    assert points[-1].direction == "up"
    assert points[-1].slope_bps == Decimal("1000")


def test_flat_and_down_regimes() -> None:
    flat = regime_series(_series(["100"] * 20), lookback=20)
    assert flat[-1].direction == "flat"
    down = regime_series(_series(["100"] * 19 + ["90"]), lookback=20)
    assert down[-1].direction == "down"


def test_future_candles_cannot_change_earlier_features() -> None:
    base = _series([str(100 + index) for index in range(30)])
    first = regime_series(base, lookback=10)
    extended = regime_series(base + [Decimal("1"), Decimal("10000")], lookback=10)
    assert extended[: len(first)] == first


def test_invalid_closes_are_rejected() -> None:
    with pytest.raises(InvalidCloses):
        regime_series([Decimal("0")], lookback=2)
    with pytest.raises(InvalidCloses):
        regime_series([Decimal("NaN")], lookback=2)
    with pytest.raises(ValueError):
        regime_series([Decimal("1")], lookback=1)
