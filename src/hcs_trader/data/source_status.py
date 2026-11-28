"""SourceStatus read model: freshness, breaker state and retry timing per source."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from pydantic import BaseModel

from hcs_trader.data.feed_status import IngestRecord, assess_feed
from hcs_trader.data.sources import SourceEntry
from hcs_trader.domain.market_data import DataQuality


@dataclass(frozen=True, slots=True)
class SourceStatus:
    source_id: str
    quality: DataQuality
    last_success: datetime | None
    last_error: str | None
    consecutive_failures: int
    enabled: bool
    kind: str = ""
    requires_api_key: bool = False
    cadence_seconds: int = 0
    stale_after_seconds: int = 0
    newest_item_at: datetime | None = None
    breaker_state: str = "CLOSED"
    retry_after_until: datetime | None = None
    retry_in_seconds: float | None = None


def _remaining(until: datetime | None, now: datetime) -> float:
    return 0.0 if until is None else max(0.0, (until - now).total_seconds())


def build_source_status(
    entry: SourceEntry, record: IngestRecord, now: datetime
) -> SourceStatus:
    """Derive the read model from one registry entry and its persisted record."""
    breaker = record.breaker_state
    breaker_wait = _remaining(record.breaker_open_until, now)
    if breaker == "OPEN" and breaker_wait <= 0:
        breaker = "HALF_OPEN"  # reset timeout elapsed; the next call is a probe
    rate_wait = _remaining(record.retry_after_until, now)
    wait = max(rate_wait, breaker_wait if breaker == "OPEN" else 0.0)
    quality = assess_feed(
        record.feed, now, stale_after=timedelta(seconds=entry.stale_after_seconds)
    )
    return SourceStatus(
        entry.source_id,
        quality,
        record.last_success,
        record.last_error,
        record.consecutive_failures,
        entry.enabled,
        kind=entry.kind.value,
        requires_api_key=entry.requires_api_key,
        cadence_seconds=entry.cadence_seconds,
        stale_after_seconds=entry.stale_after_seconds,
        newest_item_at=record.newest_item_at,
        breaker_state=breaker,
        retry_after_until=record.retry_after_until if rate_wait > 0 else None,
        retry_in_seconds=wait if wait > 0 else None,
    )


class DataSourcesReport(BaseModel):
    generated_at: datetime
    sources: list[SourceStatus]
