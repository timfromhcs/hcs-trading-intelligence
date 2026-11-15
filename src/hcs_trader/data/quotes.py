"""Bounded on-demand quote refresh with explicit cached-data quality."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import monotonic

from hcs_trader.data.provider import (
    InvalidQuote,
    QuoteProvider,
    QuoteRateLimited,
    QuoteUnavailable,
)
from hcs_trader.domain.assets import MarketId
from hcs_trader.domain.market_data import DataQuality, Quote


@dataclass(frozen=True, slots=True)
class QuoteSnapshot:
    quote: Quote | None
    quality: DataQuality
    provider_issue: str | None


@dataclass(frozen=True, slots=True)
class QuotePolicy:
    refresh_seconds: float = 5
    max_age: timedelta = timedelta(seconds=30)
    max_clock_skew: timedelta = timedelta(seconds=2)
    rate_limit_cooldown: float = 60


def _utc_now() -> datetime:
    return datetime.now(UTC)


class QuoteService:
    def __init__(
        self,
        provider: QuoteProvider,
        market: MarketId,
        policy: QuotePolicy = QuotePolicy(),
        *,
        elapsed: Callable[[], float] = monotonic,
        utc_now: Callable[[], datetime] = _utc_now,
        record_quote: Callable[[Quote], None] | None = None,
    ) -> None:
        self._provider = provider
        self._market = market
        self._policy = policy
        self._elapsed = elapsed
        self._utc_now = utc_now
        self._record_quote = record_quote
        self._lock = asyncio.Lock()
        self._quote: Quote | None = None
        self._issue: str | None = None
        self._next_refresh = float("-inf")
        self._failure_count = 0

    def restore(self, quote: Quote) -> None:
        if (
            quote.market == self._market
            and self._quality(quote) is not DataQuality.INVALID
        ):
            self._quote = quote

    async def snapshot(self) -> QuoteSnapshot:
        async with self._lock:
            if self._elapsed() >= self._next_refresh:
                await self._refresh()
            if self._quote is None:
                quality = (
                    DataQuality.INVALID
                    if self._issue == "invalid"
                    else DataQuality.MISSING
                )
            else:
                quality = self._quality(self._quote)
                if quality is DataQuality.FRESH and self._issue is not None:
                    quality = DataQuality.DEGRADED
            return QuoteSnapshot(self._quote, quality, self._issue)

    async def _refresh(self) -> None:
        delay = self._policy.refresh_seconds
        try:
            candidate = await self._provider.get_quote(self._market)
            if (
                candidate.market != self._market
                or self._quality(candidate) is DataQuality.INVALID
            ):
                raise InvalidQuote("Provider quote identity or clock is invalid")
            if self._quote is not None and (
                candidate.bid_updated_at < self._quote.bid_updated_at
                or candidate.ask_updated_at < self._quote.ask_updated_at
            ):
                raise InvalidQuote("Provider quote went backwards")
        except QuoteUnavailable as error:
            self._failure_count = min(4, self._failure_count + 1)
            delay *= 2**self._failure_count
            if isinstance(error, QuoteRateLimited):
                self._issue = "rate_limited"
                delay = max(delay, self._policy.rate_limit_cooldown)
            elif isinstance(error, InvalidQuote):
                self._issue = "invalid"
            else:
                self._issue = "unavailable"
        else:
            if self._record_quote is not None:
                self._next_refresh = self._elapsed() + delay
                self._issue = "storage_unavailable"
                await asyncio.to_thread(self._record_quote, candidate)
            self._issue = None
            self._failure_count = 0
            self._quote = candidate
        self._next_refresh = self._elapsed() + delay

    def _quality(self, quote: Quote) -> DataQuality:
        return quote.quality_at(
            self._utc_now(),
            max_age=self._policy.max_age,
            max_clock_skew=self._policy.max_clock_skew,
        )
