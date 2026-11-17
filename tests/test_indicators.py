from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hcs_trader.api import create_app
from hcs_trader.data.kraken import BTC_USD, SOURCE
from hcs_trader.domain.market_data import Candle
from hcs_trader.indicators import calculate_indicators
from hcs_trader.storage import AuditStore

_START = datetime(2020, 1, 1, tzinfo=UTC)
_HOUR = timedelta(hours=1)


def series(values: list[int]) -> list[Candle]:
    return [
        Candle(
            BTC_USD,
            SOURCE,
            60,
            _START + index * _HOUR,
            _START + (index + 1) * _HOUR,
            Decimal(price),
            Decimal(price + 1),
            Decimal(price - 1),
            Decimal(price),
            Decimal(2),
            1,
        )
        for index, price in enumerate(values)
    ]


def test_indicator_warm_up_and_one_direction_series() -> None:
    assert calculate_indicators([]).closed_at is None
    rising = series(list(range(10, 45)))
    assert calculate_indicators(rising[:14]).rsi14 is None
    assert calculate_indicators(rising[:15]).rsi14 == 100
    assert calculate_indicators(rising[:19]).sma20 is None
    assert calculate_indicators(rising[:20]).volatility20 is None
    assert calculate_indicators(rising[:33]).macd is None
    result = calculate_indicators(rising)
    assert result.closed_at == rising[-1].closed_at
    assert result.sma20 == result.ema20 == Decimal("34.5")
    assert result.macd == result.macd_signal == Decimal(7)
    assert result.macd_histogram == 0
    assert result.atr14 == 2
    assert result.volume_sma20 == 2
    assert result.volatility20 is not None and result.volatility20 > 0
    assert calculate_indicators(series(list(range(40, 20, -1)))).rsi14 == 0


def test_flat_prices_and_data_gaps() -> None:
    candles = series([10] * 40)
    result = calculate_indicators(candles)
    assert result.rsi14 == 50
    assert result.bollinger_lower == result.bollinger_upper == 10
    assert result.volatility20 == 0
    gap = calculate_indicators(candles[:24] + candles[30:])
    assert gap.contiguous_candles == 10
    assert gap.ema20 is None and gap.rsi14 is None
    with pytest.raises(ValueError, match="non-overlapping"):
        calculate_indicators([candles[0], candles[0]])
    with pytest.raises(ValueError, match="one source"):
        calculate_indicators([candles[0], replace(candles[1], source="other")])


def test_indicator_api_reads_closed_history_offline(tmp_path: Path) -> None:
    store = AuditStore(tmp_path / "indicators.sqlite")
    store.initialize()
    store.record_candles(series(list(range(10, 45))))
    with TestClient(create_app(store), base_url="http://127.0.0.1") as api:
        result = api.get("/api/v1/market/indicators").json()
        assert result["rsi14"] == "100"
        assert result["source"] == SOURCE
        assert result["interval_minutes"] == 60
        assert datetime.fromisoformat(result["opened_at"]) == _START
        assert Decimal(result["ema20"]) == Decimal("34.5")
        assert result["contiguous_candles"] == 35
        assert api.get("/api/v1/market/indicators?interval=5").json()["rsi14"] is None
        assert api.get("/api/v1/market/indicators?interval=7").status_code == 422


# --- configurable API, reference cross-checks and properties ------------------

from fractions import Fraction  # noqa: E402

from hypothesis import given  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from hcs_trader import indicators as ind  # noqa: E402

_CLASSIC = [
    "44.34", "44.09", "44.15", "43.61", "44.33", "44.83", "45.10", "45.42",
    "45.84", "46.08", "45.89", "46.03", "45.61", "46.28", "46.28",
]  # fmt: skip


def _reference_rsi(closes: list[Fraction], period: int) -> Fraction | None:
    """Independent exact-rational Wilder RSI used only as a test oracle."""
    if len(closes) <= period:
        return None
    deltas = [b - a for a, b in zip(closes, closes[1:], strict=False)]
    gain = sum((max(d, Fraction(0)) for d in deltas[:period]), Fraction(0)) / period
    loss = sum((max(-d, Fraction(0)) for d in deltas[:period]), Fraction(0)) / period
    for d in deltas[period:]:
        gain = (gain * (period - 1) + max(d, Fraction(0))) / period
        loss = (loss * (period - 1) + max(-d, Fraction(0))) / period
    return Fraction(50) if gain + loss == 0 else 100 * gain / (gain + loss)


def test_rsi_matches_published_wilder_vector() -> None:
    value = ind.rsi([Decimal(v) for v in _CLASSIC], 14)
    # Published charting examples quote ~70.5 (rounded averages); the exact
    # Wilder value on this data is checked against the independent oracle.
    assert value is not None and abs(value - Decimal("70.53")) < Decimal("0.1")
    oracle = _reference_rsi([Fraction(v) for v in _CLASSIC], 14)
    assert oracle is not None and abs(Fraction(value) - oracle) < Fraction(1, 10**20)
    assert ind.rsi([Decimal(v) for v in _CLASSIC[:14]], 14) is None


def test_rsi_documented_edge_cases() -> None:
    assert ind.rsi([Decimal(10)] * 30, 14) == 50
    assert ind.rsi([Decimal(n) for n in range(30)], 14) == 100
    assert ind.rsi([Decimal(n) for n in range(30, 0, -1)], 14) == 0


def test_configurable_periods_and_warmup() -> None:
    values = [Decimal(n) for n in range(1, 11)]
    assert ind.sma(values, 4) == Decimal("8.5")
    assert ind.sma(values, 11) is None and ind.ema(values, 11) is None
    assert ind.ema(values, 10) == Decimal("5.5")
    assert ind.macd(values, 3, 5, 2) is not None
    assert ind.macd(values[:5], 3, 5, 2) is None
    assert ind.bollinger(values, 4, Decimal(0)) == (
        Decimal("8.5"),
        Decimal("8.5"),
        Decimal("8.5"),
    )
    for bad in (0, -1):
        with pytest.raises(ValueError, match="period"):
            ind.sma(values, bad)
    with pytest.raises(ValueError, match="finite"):
        ind.sma([Decimal("NaN")] * 3, 2)
    with pytest.raises(ValueError, match="fast < slow"):
        ind.macd(values, 5, 3, 2)


def test_configurable_defaults_match_snapshot_indicators() -> None:
    candles = series([100 + (n * 7) % 13 + n // 3 for n in range(60)])
    snapshot = calculate_indicators(candles)
    closes = [c.close for c in candles]
    assert ind.rsi(closes) == snapshot.rsi14
    assert ind.sma(closes, 20) == snapshot.sma20
    assert ind.ema(closes, 20) == snapshot.ema20
    assert ind.atr(candles) == snapshot.atr14
    assert ind.macd(closes) == (
        snapshot.macd,
        snapshot.macd_signal,
        snapshot.macd_histogram,
    )
    lower, _, upper = ind.bollinger(closes) or (None, None, None)
    assert (lower, upper) == (snapshot.bollinger_lower, snapshot.bollinger_upper)


def test_atr_normalized_atr_volume_and_trend() -> None:
    candles = series(list(range(10, 40)))
    assert ind.atr(candles[:14]) is None
    assert ind.atr(candles) == 2
    normalized = ind.normalized_atr(candles)
    assert normalized is not None and abs(normalized - Decimal(2) / 39) < Decimal(
        "1e-20"
    )
    assert ind.volume_change(candles) == 1
    assert ind.volume_change(candles[:5]) is None
    assert ind.trend_state([c.close for c in candles]) == "up"
    assert ind.trend_state([c.close for c in reversed(candles)]) == "down"
    assert ind.trend_state([Decimal(5)] * 30) == "flat"
    assert ind.trend_state([Decimal(5)] * 3) is None


def test_warmup_status_reports_each_indicator() -> None:
    assert not ind.warmup_status(14).rsi14
    assert ind.warmup_status(15).rsi14 and not ind.warmup_status(15).sma20
    assert not ind.warmup_status(33).complete
    assert ind.warmup_status(34).complete


def test_duplicate_or_nan_candles_are_rejected() -> None:
    candles = series(list(range(10, 20)))
    with pytest.raises(ValueError, match="non-overlapping"):
        calculate_indicators([candles[0], candles[0]])
    with pytest.raises(ValueError):
        replace(candles[0], close=Decimal("NaN"))


_prices = st.lists(
    st.decimals(min_value=Decimal("0.01"), max_value=Decimal("100000"), places=2),
    min_size=0,
    max_size=80,
)


@given(_prices, st.integers(min_value=2, max_value=30))
def test_property_rsi_bounded_and_matches_exact_reference(
    prices: list[Decimal], period: int
) -> None:
    value = ind.rsi(prices, period)
    expected = _reference_rsi([Fraction(p) for p in prices], period)
    if expected is None:
        assert value is None
        return
    assert value is not None and 0 <= value <= 100
    assert abs(Fraction(value) - expected) < Fraction(1, 10**20)


@given(_prices, st.integers(min_value=1, max_value=40))
def test_property_sma_bollinger_ordering(prices: list[Decimal], period: int) -> None:
    mean = ind.sma(prices, period)
    bands = ind.bollinger(prices, period)
    assert (mean is None) == (bands is None) == (len(prices) < period)
    if mean is not None and bands is not None:
        lower, middle, upper = bands
        assert lower <= middle <= upper
        assert abs(middle - mean) < Decimal("1e-20")
        assert min(prices[-period:]) <= mean <= max(prices[-period:])


@given(_prices)
def test_property_non_finite_inputs_always_rejected(prices: list[Decimal]) -> None:
    for poison in (Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")):
        with pytest.raises(ValueError, match="finite"):
            ind.sma([*prices, poison], 1)
