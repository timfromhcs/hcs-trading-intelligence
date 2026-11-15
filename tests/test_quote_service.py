import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from hcs_trader.data.kraken import BTC_USD
from hcs_trader.data.provider import QuoteRateLimited, QuoteUnavailable
from hcs_trader.data.quotes import QuoteService
from hcs_trader.domain.assets import MarketId
from hcs_trader.domain.market_data import DataQuality, Quote

TIME = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass
class Feed:
    seconds: float = 0
    calls: int = 0
    failure: type[QuoteUnavailable] | None = None
    source_offset: float = 0

    def elapsed(self) -> float:
        return self.seconds

    def now(self) -> datetime:
        return TIME + timedelta(seconds=self.seconds)

    async def get_quote(self, market: MarketId) -> Quote:
        self.calls += 1
        await asyncio.sleep(0)
        if self.failure is not None:
            raise self.failure("fixture")
        source_stamp = self.now() + timedelta(seconds=self.source_offset)
        return Quote(
            market,
            "fixture",
            Decimal("12.34"),
            Decimal("12.35"),
            source_stamp,
            source_stamp,
            self.now(),
        )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_concurrent_requests_share_one_refresh_and_preserve_fallback() -> None:
    feed = Feed()
    service = QuoteService(feed, BTC_USD, elapsed=feed.elapsed, utc_now=feed.now)
    results = await asyncio.gather(*(service.snapshot() for _ in range(8)))
    assert feed.calls == 1
    initial = results[0]
    assert initial.quality == DataQuality.FRESH
    assert all(value == initial for value in results)

    feed.seconds = 5
    feed.failure = QuoteRateLimited
    limited = await service.snapshot()
    assert limited.quality == DataQuality.DEGRADED
    assert limited.provider_issue == "rate_limited"
    assert limited.quote == initial.quote
    feed.seconds = 50
    assert (await service.snapshot()).quality == DataQuality.STALE
    assert feed.calls == 2

    feed.seconds = 65
    feed.failure = None
    recovered = await service.snapshot()
    assert recovered.quality == DataQuality.FRESH
    assert recovered.provider_issue is None
    assert recovered.quote != initial.quote
    assert feed.calls == 3


@pytest.mark.anyio
async def test_missing_and_invalid_data_never_appear_fresh() -> None:
    feed = Feed(failure=QuoteUnavailable)
    service = QuoteService(feed, BTC_USD, elapsed=feed.elapsed, utc_now=feed.now)
    missing = await service.snapshot()
    assert missing.quality == DataQuality.MISSING
    assert missing.quote is None
    assert missing.provider_issue == "unavailable"

    feed.seconds = 10
    feed.failure = None
    feed.source_offset = 60
    invalid = await service.snapshot()
    assert invalid.quality == DataQuality.INVALID
    assert invalid.quote is None


@pytest.mark.anyio
async def test_out_of_order_data_does_not_replace_latest_quote() -> None:
    feed = Feed()
    service = QuoteService(feed, BTC_USD, elapsed=feed.elapsed, utc_now=feed.now)
    initial = await service.snapshot()
    feed.seconds = 5
    feed.source_offset = -6
    result = await service.snapshot()
    assert result.quote == initial.quote
    assert result.quality == DataQuality.DEGRADED
    assert result.provider_issue == "invalid"
