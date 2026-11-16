"""Market data provider contract and normalized failures."""

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum
from typing import Protocol

from hcs_trader.domain.assets import MarketId
from hcs_trader.domain.market_data import Candle, Quote


class QuoteUnavailable(Exception):
    """The provider could not supply the requested quote."""


class QuoteRateLimited(QuoteUnavailable):
    """The provider requested a pause in polling."""


class InvalidQuote(QuoteUnavailable):
    """The provider response did not meet the quote contract."""


class SourceRateLimited(Exception):
    """A news/macro source answered 429/503, with an optional Retry-After."""

    retry_after: float | None = None


def parse_retry_after(
    value: str | None, *, now: datetime | None = None
) -> float | None:
    """Parse a Retry-After header (seconds or HTTP date) into bounded seconds."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(min(int(value), 3600))
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if when.utcoffset() is None or now is None:
        return None
    return max(0.0, min((when - now).total_seconds(), 3600.0))


class ProviderStatus(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    DOWN = "DOWN"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    provider: str
    status: ProviderStatus
    last_success: datetime | None = None
    last_error: str | None = None
    quota_remaining: int | None = None
    quota_note: str | None = None


class QuoteProvider(Protocol):
    async def get_quote(self, market: MarketId) -> Quote: ...


class MarketDataProvider(Protocol):
    """AGENTS.md 4.1 provider contract; adapters normalise at the edge."""

    async def get_quote(self, market: MarketId) -> Quote: ...

    async def get_ohlcv(
        self,
        market: MarketId,
        interval_minutes: int,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> Sequence[Candle]: ...

    def stream_quotes(self, markets: Sequence[MarketId]) -> AsyncIterator[Quote]: ...

    async def health(self) -> ProviderHealth: ...
