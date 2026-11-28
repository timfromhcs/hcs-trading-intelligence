"""Tests for the shared data-quality library."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from hcs_trader.data.quality import (
    check_candle_series,
    check_freshness,
    check_order_book,
    check_quote,
)
from hcs_trader.domain.market_data import Candle, DataQuality, Quote
from hcs_trader.domain.markets import DEFAULT_MARKET_ID

NOW = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)
AGE = timedelta(seconds=30)


def _quote(
    *, bid: str = "100", ask: str = "101", observed: datetime | None = None
) -> Quote:
    return Quote(
        market=DEFAULT_MARKET_ID,
        source="test",
        bid=Decimal(bid),
        ask=Decimal(ask),
        bid_updated_at=observed or NOW - timedelta(seconds=5),
        ask_updated_at=observed or NOW - timedelta(seconds=5),
        received_at=NOW - timedelta(seconds=4),
    )


def _candle(opened_at: datetime, *, source: str = "test") -> Candle:
    return Candle(
        market=DEFAULT_MARKET_ID,
        source=source,
        interval_minutes=1,
        opened_at=opened_at,
        received_at=opened_at + timedelta(minutes=1, seconds=2),
        open=Decimal("100"),
        high=Decimal("102"),
        low=Decimal("99"),
        close=Decimal("101"),
        volume=Decimal("1.5"),
        trade_count=3,
    )


def test_fresh_observation_is_tradable() -> None:
    report = check_freshness(
        NOW - timedelta(seconds=2), NOW - timedelta(seconds=1), NOW, max_age=AGE
    )
    assert report.status is DataQuality.FRESH
    assert report.tradable
    assert report.codes == ()


def test_stale_observation_is_not_tradable() -> None:
    report = check_freshness(
        NOW - timedelta(minutes=5), NOW - timedelta(minutes=5), NOW, max_age=AGE
    )
    assert report.status is DataQuality.STALE
    assert "stale" in report.codes
    assert not report.tradable


def test_future_observation_is_invalid_not_stale() -> None:
    report = check_freshness(NOW + timedelta(seconds=5), NOW, NOW, max_age=AGE)
    assert report.status is DataQuality.INVALID
    assert "clock_drift" in report.codes


def test_receipt_in_future_is_invalid() -> None:
    report = check_freshness(NOW, NOW + timedelta(seconds=1), NOW, max_age=AGE)
    assert report.status is DataQuality.INVALID


@given(st.integers(min_value=-3600, max_value=3600))
def test_freshness_never_returns_unknown_status(offset: int) -> None:
    report = check_freshness(
        NOW + timedelta(seconds=offset),
        NOW,
        NOW,
        max_age=AGE,
        max_clock_skew=timedelta(0),
    )
    assert report.status in set(DataQuality)


def test_quote_wide_spread_is_degraded() -> None:
    report = check_quote(
        _quote(bid="99", ask="101"), NOW, max_age=AGE, max_spread_bps=150
    )
    assert report.status is DataQuality.DEGRADED
    assert "wide_spread" in report.codes


def test_crossed_quote_from_raw_values_is_invalid() -> None:
    # The domain type rejects crossed quotes, so exercise the book checker directly.
    report = check_order_book(
        [(Decimal("102"), Decimal("1"))],
        [(Decimal("101"), Decimal("1"))],
        received_at=NOW,
        now=NOW,
        max_age=AGE,
    )
    assert report.status is DataQuality.INVALID
    assert "crossed_book" in report.codes


def test_empty_candle_series_is_missing() -> None:
    report = check_candle_series([], interval_minutes=1)
    assert report.status is DataQuality.MISSING


def test_candle_series_duplicate_and_gap() -> None:
    first = _candle(NOW - timedelta(minutes=4))
    duplicate = _candle(NOW - timedelta(minutes=4))
    after_gap = _candle(NOW - timedelta(minutes=1))
    report = check_candle_series([first, duplicate, after_gap], interval_minutes=1)
    assert report.status is DataQuality.INVALID
    assert "duplicate_candle" in report.codes
    assert "gap" in report.codes


def test_clean_candle_series_is_fresh() -> None:
    candles = [_candle(NOW - timedelta(minutes=n)) for n in (3, 2, 1)]
    report = check_candle_series(
        candles,
        interval_minutes=1,
        now=NOW + timedelta(seconds=2),
        max_age=timedelta(minutes=5),
    )
    assert report.status is DataQuality.FRESH


def test_out_of_order_series_is_invalid() -> None:
    candles = [_candle(NOW - timedelta(minutes=2)), _candle(NOW - timedelta(minutes=3))]
    report = check_candle_series(candles, interval_minutes=1)
    assert "out_of_order" in report.codes


def test_unsorted_book_is_invalid() -> None:
    report = check_order_book(
        [(Decimal("100"), Decimal("1")), (Decimal("101"), Decimal("1"))],
        [(Decimal("102"), Decimal("1")), (Decimal("103"), Decimal("1"))],
        received_at=NOW,
        now=NOW,
        max_age=AGE,
    )
    assert report.status is DataQuality.INVALID
    assert "unsorted_book" in report.codes


def test_book_size_cap_is_invalid() -> None:
    report = check_order_book(
        [(Decimal("100"), Decimal("1"))] * 3,
        [],
        received_at=NOW,
        now=NOW,
        max_age=AGE,
        max_levels=2,
    )
    assert "too_many_levels" in report.codes
