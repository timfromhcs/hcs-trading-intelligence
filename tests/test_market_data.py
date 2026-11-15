from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from hcs_trader.domain.assets import AssetId, MarketId
from hcs_trader.domain.market_data import DataQuality, Quote

NOW = datetime(2026, 1, 1, tzinfo=UTC)
AGE = timedelta(seconds=30)
SKEW = timedelta(seconds=2)
MARKET = MarketId(
    "fixture",
    AssetId.parse("bitcoin:mainnet/native:BTC"),
    AssetId.parse("fiat:iso4217/currency:USD"),
)


def quote() -> Quote:
    return Quote(MARKET, "fixture", Decimal("1.01"), Decimal("1.02"), NOW, NOW, NOW)


def test_freshness_tracks_both_provider_sides_and_local_receipt() -> None:
    assert (
        quote().quality_at(NOW, max_age=AGE, max_clock_skew=SKEW) == DataQuality.FRESH
    )
    stale_bid = replace(quote(), bid_updated_at=NOW - AGE - SKEW)
    assert (
        stale_bid.quality_at(NOW, max_age=AGE, max_clock_skew=SKEW) == DataQuality.STALE
    )
    assert (
        quote().quality_at(NOW + AGE + SKEW, max_age=AGE, max_clock_skew=SKEW)
        == DataQuality.STALE
    )
    ahead = replace(quote(), ask_updated_at=NOW + SKEW * 2)
    assert (
        ahead.quality_at(NOW, max_age=AGE, max_clock_skew=SKEW) == DataQuality.INVALID
    )
    assert (
        quote().quality_at(NOW - SKEW, max_age=AGE, max_clock_skew=SKEW)
        == DataQuality.INVALID
    )


@pytest.mark.parametrize("bid", ["NaN", "Infinity", "-1", "0", "1.02", "1.03"])
def test_quote_rejects_invalid_prices(bid: str) -> None:
    with pytest.raises(ValueError):
        replace(quote(), bid=Decimal(bid))


def test_quote_requires_provenance_and_utc() -> None:
    with pytest.raises(ValueError, match="source"):
        replace(quote(), source="")
    with pytest.raises(ValueError, match="UTC"):
        replace(quote(), ask_updated_at=NOW.replace(tzinfo=None))
    with pytest.raises(ValueError, match="age"):
        quote().quality_at(NOW, max_age=timedelta(0), max_clock_skew=SKEW)
