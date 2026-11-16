from datetime import UTC, datetime
from decimal import Decimal

import pytest

from hcs_trader.data.kraken import BTC_USD
from hcs_trader.data.market_provider import KrakenMarketDataProvider
from hcs_trader.data.provider import (
    MarketDataProvider,
    ProviderStatus,
    QuoteRateLimited,
)
from hcs_trader.data.quotes import QuoteService
from hcs_trader.domain.market_data import DataQuality, Quote

T0 = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Feed:
    async def get_quote(self, market):  # type: ignore[no-untyped-def]
        return Quote(market, "fx", Decimal("1"), Decimal("2"), T0, T0, T0)


@pytest.mark.anyio
async def test_storage_failure_degrades_instead_of_crashing() -> None:
    def boom(_: Quote) -> None:
        raise RuntimeError("sqlite locked")

    service = QuoteService(
        Feed(), BTC_USD, elapsed=lambda: 0.0, utc_now=lambda: T0, record_quote=boom
    )
    snap = await service.snapshot()
    assert snap.quote is not None
    assert snap.quality is DataQuality.DEGRADED
    assert snap.provider_issue == "storage_unavailable"


class FakeKraken:
    def __init__(self) -> None:
        self.fail = False

    async def get_quote(self, market):  # type: ignore[no-untyped-def]
        if self.fail:
            raise QuoteRateLimited("x")
        return Quote(market, "kraken", Decimal("1"), Decimal("2"), T0, T0, T0)

    async def get_candles(self, market, interval):  # type: ignore[no-untyped-def]
        return []


@pytest.mark.anyio
async def test_kraken_adapter_satisfies_protocol_and_tracks_health() -> None:
    fake = FakeKraken()
    provider = KrakenMarketDataProvider(fake, utc_now=lambda: T0)  # type: ignore[arg-type]
    typed: MarketDataProvider = provider
    assert (await typed.health()).status is ProviderStatus.UNKNOWN
    await typed.get_quote(BTC_USD)
    health = await typed.health()
    assert health.status is ProviderStatus.HEALTHY and health.last_success == T0
    fake.fail = True
    with pytest.raises(QuoteRateLimited):
        await typed.get_quote(BTC_USD)
    health = await typed.health()
    assert health.status is ProviderStatus.DOWN
    assert health.last_error == "QuoteRateLimited"
    assert list(await typed.get_ohlcv(BTC_USD, 60)) == []
