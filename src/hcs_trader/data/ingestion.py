"""Bounded, cancellable per-source ingestion with persisted last-success state."""

import asyncio
import sqlite3
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import monotonic
from typing import Any

from hcs_trader.data.feed_status import FeedState, assess_feed
from hcs_trader.data.resilience import (
    CircuitBreaker,
    CircuitOpen,
    RetryBudgetExhausted,
    call_with_retry,
)
from hcs_trader.data.sources import SourceEntry, SourceRegistry
from hcs_trader.domain.market_data import DataQuality


def _utc_now() -> datetime:
    return datetime.now(UTC)


class IngestStateStore:
    """Own sqlite file/table, created idempotently; ':memory:' for tests."""

    def __init__(self, path: str = ":memory:") -> None:
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS ingest_state ("
            "source_id TEXT PRIMARY KEY, last_success TEXT, last_error TEXT, "
            "newest_item_at TEXT, consecutive_failures INTEGER NOT NULL DEFAULT 0, "
            "cursor TEXT)"
        )
        self._db.commit()

    def load(self, source_id: str) -> FeedState:
        row = self._db.execute(
            "SELECT last_success, last_error, newest_item_at, consecutive_failures "
            "FROM ingest_state WHERE source_id = ?",
            (source_id,),
        ).fetchone()
        if row is None:
            return FeedState(source_id, None, None)
        parse = datetime.fromisoformat
        return FeedState(
            source_id,
            parse(row[0]) if row[0] else None,
            row[1],
            parse(row[2]) if row[2] else None,
            row[3],
        )

    def save(self, state: FeedState) -> None:
        self._db.execute(
            "INSERT INTO ingest_state(source_id, last_success, last_error, "
            "newest_item_at, consecutive_failures) VALUES (?,?,?,?,?) "
            "ON CONFLICT(source_id) DO UPDATE SET last_success=excluded.last_success, "
            "last_error=excluded.last_error, newest_item_at=excluded.newest_item_at, "
            "consecutive_failures=excluded.consecutive_failures",
            (
                state.source_id,
                state.last_success.isoformat() if state.last_success else None,
                state.last_error,
                state.newest_item_at.isoformat() if state.newest_item_at else None,
                state.consecutive_failures,
            ),
        )
        self._db.commit()

    def close(self) -> None:
        self._db.close()


@dataclass(slots=True)
class SourceJob:
    entry: SourceEntry
    fetch: Callable[[], Awaitable[Sequence[Any]]]
    sink: Callable[[Sequence[Any]], None]
    newest_at: Callable[[Sequence[Any]], datetime | None] = lambda items: None
    breaker: CircuitBreaker = field(default_factory=CircuitBreaker)


@dataclass(frozen=True, slots=True)
class SourceStatus:
    source_id: str
    quality: DataQuality
    last_success: datetime | None
    last_error: str | None
    consecutive_failures: int
    enabled: bool


class IngestionScheduler:
    def __init__(
        self,
        registry: SourceRegistry,
        jobs: Sequence[SourceJob],
        store: IngestStateStore,
        *,
        utc_now: Callable[[], datetime] = _utc_now,
        clock: Callable[[], float] = monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        max_batch: int = 500,
    ) -> None:
        self._registry = registry
        self._jobs = {j.entry.source_id: j for j in jobs}
        self._store = store
        self._now = utc_now
        self._clock = clock
        self._sleep = sleep
        self._max_batch = max_batch
        self._next_due: dict[str, float] = {}

    async def run_once(self, source_id: str) -> SourceStatus:
        job = self._jobs[source_id]
        entry = self._registry.get(source_id)
        state = self._store.load(source_id)
        if not entry.enabled:
            return self.status(source_id)
        try:
            items = await call_with_retry(
                job.fetch,
                policy=entry.retry,
                breaker=job.breaker,
                sleep=self._sleep,
            )
            if len(items) > self._max_batch:
                raise ValueError("Batch exceeds the configured work bound")
            job.sink(items)
        except (CircuitOpen, RetryBudgetExhausted, ValueError, OSError) as error:
            state = FeedState(
                source_id,
                state.last_success,
                type(error).__name__,
                state.newest_item_at,
                state.consecutive_failures + 1,
            )
        else:
            state = FeedState(
                source_id,
                self._now(),
                None,
                job.newest_at(items) or state.newest_item_at,
                0,
            )
        self._store.save(state)
        self._next_due[source_id] = self._clock() + entry.cadence_seconds
        return self.status(source_id)

    async def run_due(self) -> list[SourceStatus]:
        out = []
        for entry in self._registry.enabled():
            if entry.source_id in self._jobs and (
                self._clock() >= self._next_due.get(entry.source_id, float("-inf"))
            ):
                out.append(await self.run_once(entry.source_id))
        return out

    async def run_forever(self, stop: asyncio.Event, tick: float = 1.0) -> None:
        while not stop.is_set():
            await self.run_due()
            try:
                await asyncio.wait_for(stop.wait(), timeout=tick)
            except TimeoutError:
                pass

    def status(self, source_id: str) -> SourceStatus:
        entry = self._registry.get(source_id)
        state = self._store.load(source_id)
        from datetime import timedelta

        quality = assess_feed(
            state, self._now(), stale_after=timedelta(seconds=entry.stale_after_seconds)
        )
        return SourceStatus(
            source_id,
            quality,
            state.last_success,
            state.last_error,
            state.consecutive_failures,
            entry.enabled,
        )

    def all_status(self) -> list[SourceStatus]:
        return [self.status(e.source_id) for e in self._registry.all()]
