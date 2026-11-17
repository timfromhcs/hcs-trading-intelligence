"""MarketDataProvider adapter over the Kraken REST provider with health tracking."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import UTC, datetime

from hcs_trader.data.kraken import KrakenQuoteProvider
from hcs_trader.data.provider import (
    ProviderHealth,
    ProviderStatus,
    QuoteRateLimited,
    QuoteUnavailable,
)
from hcs_trader.domain.assets import MarketId
from hcs_trader.domain.market_data import Candle, Quote


def _utc_now() -> datetime:
    return datetime.now(UTC)


class KrakenMarketDataProvider:
    """Implements MarketDataProvider; streaming is polling (no WebSocket in V1)."""

    name = "kraken"

    def __init__(
        self,
        inner: KrakenQuoteProvider,
        *,
        utc_now: Callable[[], datetime] = _utc_now,
        poll_seconds: float = 5.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._inner = inner
        self._now = utc_now
        self._poll = poll_seconds
        self._sleep = sleep
        self._last_success: datetime | None = None
        self._last_error: str | None = None
        self._rate_limited = False

    def _ok(self) -> None:
        self._last_success = self._now()
        self._last_error = None
        self._rate_limited = False

    def _fail(self, error: Exception) -> None:
        self._last_error = type(error).__name__
        self._rate_limited = isinstance(error, QuoteRateLimited)

    async def get_quote(self, market: MarketId) -> Quote:
        try:
            quote = await self._inner.get_quote(market)
        except QuoteUnavailable as error:
            self._fail(error)
            raise
        self._ok()
        return quote

    async def get_ohlcv(
        self,
        market: MarketId,
        interval_minutes: int,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> Sequence[Candle]:
        try:
            candles = await self._inner.get_candles(market, interval_minutes)
        except QuoteUnavailable as error:
            self._fail(error)
            raise
        self._ok()
        return [
            c
            for c in candles
            if (start is None or c.opened_at >= start)
            and (end is None or c.closed_at <= end)
        ]

    async def stream_quotes(self, markets: Sequence[MarketId]) -> AsyncIterator[Quote]:
        while True:
            for market in markets:
                try:
                    yield await self.get_quote(market)
                except QuoteUnavailable:
                    continue
            await self._sleep(self._poll)

    async def health(self) -> ProviderHealth:
        if self._last_error is None and self._last_success is None:
            status = ProviderStatus.UNKNOWN
        elif self._last_error is None:
            status = ProviderStatus.HEALTHY
        elif self._last_success is not None and not self._rate_limited:
            status = ProviderStatus.DEGRADED
        else:
            status = ProviderStatus.DOWN
        return ProviderHealth(
            self.name,
            status,
            self._last_success,
            self._last_error,
            None,
            "Public endpoint; rate limits apply",
        )
