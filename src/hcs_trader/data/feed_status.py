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
