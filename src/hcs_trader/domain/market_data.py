"""Provider-independent best bid and offer with explicit freshness."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from hcs_trader.domain.assets import MarketId


class DataQuality(StrEnum):
    FRESH = "FRESH"
    STALE = "STALE"
    DEGRADED = "DEGRADED"
    MISSING = "MISSING"
    INVALID = "INVALID"


@dataclass(frozen=True, slots=True)
class Quote:
    market: MarketId
    source: str
    bid: Decimal
    ask: Decimal
    bid_updated_at: datetime
    ask_updated_at: datetime
    received_at: datetime

    def __post_init__(self) -> None:
        if not self.source:
            raise ValueError("Quote source is required")
        for price in (self.bid, self.ask):
            if not isinstance(price, Decimal) or not price.is_finite() or price <= 0:
                raise ValueError("Quote prices must be positive finite Decimals")
        if self.bid >= self.ask:
            raise ValueError("Locked and crossed quotes are invalid")
        for timestamp in (self.bid_updated_at, self.ask_updated_at, self.received_at):
            _require_utc(timestamp)

    def quality_at(
        self, now: datetime, *, max_age: timedelta, max_clock_skew: timedelta
    ) -> DataQuality:
        _require_utc(now)
        if max_age <= timedelta(0) or max_clock_skew < timedelta(0):
            raise ValueError(
                "Freshness requires a positive age and non-negative clock skew"
            )
        oldest_side = min(self.bid_updated_at, self.ask_updated_at)
        newest_side = max(self.bid_updated_at, self.ask_updated_at)
        if newest_side - self.received_at > max_clock_skew or self.received_at > now:
            return DataQuality.INVALID
        if now - min(oldest_side, self.received_at) > max_age:
            return DataQuality.STALE
        return DataQuality.FRESH


def _require_utc(timestamp: datetime) -> None:
    if timestamp.utcoffset() != UTC.utcoffset(timestamp):
        raise ValueError("Quote timestamps must be timezone-aware UTC")
