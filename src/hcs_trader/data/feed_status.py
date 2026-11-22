"""FRESH/STALE/DEGRADED/MISSING/INVALID classification for news and macro feeds."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from hcs_trader.domain.market_data import DataQuality


@dataclass(frozen=True, slots=True)
class FeedState:
    source_id: str
    last_success: datetime | None
    last_error: str | None
    newest_item_at: datetime | None = None
    consecutive_failures: int = 0


def assess_feed(
    state: FeedState,
    now: datetime,
    *,
    stale_after: timedelta,
    max_clock_skew: timedelta = timedelta(minutes=5),
) -> DataQuality:
    """Classify a feed; a failing feed past its window is STALE, never FRESH."""
    if state.last_success is None:
        return DataQuality.MISSING
    if state.last_success > now + max_clock_skew:
        return DataQuality.INVALID
    if state.newest_item_at is not None and state.newest_item_at > now + max_clock_skew:
        return DataQuality.INVALID
    if now - state.last_success > stale_after:
        return DataQuality.STALE
    if state.last_error is not None or state.consecutive_failures > 0:
        return DataQuality.DEGRADED
    return DataQuality.FRESH


@dataclass(frozen=True, slots=True)
class IngestRecord:
    """Persisted per-source runtime state (feed outcome, rate-limit and breaker)."""

    source_id: str
    last_success: datetime | None = None
    last_error: str | None = None
    newest_item_at: datetime | None = None
    consecutive_failures: int = 0
    retry_after_until: datetime | None = None
    breaker_state: str = "CLOSED"
    breaker_open_until: datetime | None = None

    @property
    def feed(self) -> FeedState:
        return FeedState(
            self.source_id,
            self.last_success,
            self.last_error,
            self.newest_item_at,
            self.consecutive_failures,
        )
