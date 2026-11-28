"""Bounded, cancellable per-source ingestion with persisted state.

State (last success, failures, ``Retry-After`` deadline, breaker snapshot) lives in
the audit database so a restart neither forgets a rate limit nor hammers a provider.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any, Protocol

from hcs_trader.data.feed_status import IngestRecord
from hcs_trader.data.provider import SourceRateLimited
from hcs_trader.data.resilience import (
    CircuitBreaker,
    CircuitOpen,
    RetryBudgetExhausted,
    call_with_retry,
)
from hcs_trader.data.source_status import SourceStatus, build_source_status
from hcs_trader.data.sources import SourceEntry, SourceRegistry
from hcs_trader.network_policy import NetworkPolicy, SystemMode

_logger = logging.getLogger(__name__)
_RATE_LIMIT_FLOOR = 60.0
_BACKOFF_CAP = 3600.0


def _utc_now() -> datetime:
    return datetime.now(UTC)


class IngestStateStore:
    """In-memory state store (tests, ephemeral runs).

    The application persists the same records in the audit database through
    ``AuditStore`` (migration 20); both expose ``load`` and ``save``.
    """

    def __init__(self) -> None:
        self._records: dict[str, IngestRecord] = {}

    def load(self, source_id: str) -> IngestRecord:
        return self._records.get(source_id, IngestRecord(source_id))

    def save(self, state: IngestRecord) -> None:
        self._records[state.source_id] = state

    def close(self) -> None:
        self._records.clear()


class IngestStateBackend(Protocol):
    def load(self, source_id: str) -> IngestRecord: ...

    def save(self, state: IngestRecord) -> None: ...


@dataclass(slots=True)
class SourceJob:
    entry: SourceEntry
    fetch: Callable[[], Awaitable[Sequence[Any]]]
    sink: Callable[[Sequence[Any]], object]
    newest_at: Callable[[Sequence[Any]], datetime | None] = lambda items: None
    breaker: CircuitBreaker = field(default_factory=CircuitBreaker)


class IngestionScheduler:
    def __init__(
        self,
        registry: SourceRegistry,
        jobs: Sequence[SourceJob],
        store: IngestStateBackend,
        *,
        utc_now: Callable[[], datetime] = _utc_now,
        clock: Callable[[], float] = monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        max_batch: int = 500,
        network_policy: NetworkPolicy | None = None,
    ) -> None:
        self._registry = registry
        self._jobs = {j.entry.source_id: j for j in jobs}
        self._store = store
        self._now = utc_now
        self._clock = clock
        self._sleep = sleep
        self._max_batch = max_batch
        self._policy = network_policy
        self._next_due: dict[str, float] = {}

    @property
    def network_allowed(self) -> bool:
        return self._policy is None or self._policy.mode is not SystemMode.OFFLINE

    def restore(self) -> None:
        """Re-arm breakers and rate-limit deadlines from persisted state."""
        now = self._now()
        for source_id, job in self._jobs.items():
            record = self._store.load(source_id)
            if record.breaker_state == "OPEN" and record.breaker_open_until:
                job.breaker.force_open(
                    (record.breaker_open_until - now).total_seconds()
                )
            if record.retry_after_until and record.retry_after_until > now:
                wait = (record.retry_after_until - now).total_seconds()
                self._next_due[source_id] = self._clock() + wait

    def _snapshot(self, job: SourceJob, record: IngestRecord) -> IngestRecord:
        wait = job.breaker.retry_in
        return replace(
            record,
            breaker_state=job.breaker.state.value,
            breaker_open_until=(
                self._now() + timedelta(seconds=wait) if wait > 0 else None
            ),
        )

    async def run_once(self, source_id: str) -> SourceStatus:
        job = self._jobs[source_id]
        entry = self._registry.get(source_id)
        if not entry.enabled or not self.network_allowed:
            return self.status(source_id)
        record = self._store.load(source_id)
        now = self._now()
        if record.retry_after_until is not None and record.retry_after_until > now:
            # A persisted Retry-After / 429 deadline is still in force.
            wait = (record.retry_after_until - now).total_seconds()
            self._next_due[source_id] = self._clock() + wait
            return self.status(source_id)
        retry_after_until: datetime | None = None
        items: Sequence[Any] = ()
        error_name: str | None = None
        try:
            items = await call_with_retry(
                job.fetch,
                policy=entry.retry,
                breaker=job.breaker,
                non_retryable=(SourceRateLimited,),
                sleep=self._sleep,
            )
            if len(items) > self._max_batch:
                raise ValueError("Batch exceeds the configured work bound")
            await asyncio.to_thread(job.sink, items)
        except SourceRateLimited as error:
            error_name = type(error).__name__
            pause = error.retry_after if error.retry_after else _RATE_LIMIT_FLOOR
            retry_after_until = self._now() + timedelta(seconds=pause)
        except (CircuitOpen, RetryBudgetExhausted) as error:
            error_name = type(error).__name__
        except Exception as error:  # noqa: BLE001 - sink faults must not kill the loop
            error_name = type(error).__name__
            _logger.warning("ingestion sink or fetch failed: %s", error_name)
        if error_name is None:
            record = replace(
                record,
                last_success=self._now(),
                last_error=None,
                newest_item_at=job.newest_at(items) or record.newest_item_at,
                consecutive_failures=0,
                retry_after_until=None,
            )
        else:
            record = replace(
                record,
                last_error=error_name,
                consecutive_failures=record.consecutive_failures + 1,
                retry_after_until=retry_after_until,
            )
        delay = float(entry.cadence_seconds)
        if error_name is not None:
            delay = min(
                delay * 2 ** min(record.consecutive_failures, 6),
                max(_BACKOFF_CAP, delay),
            )
        if retry_after_until is not None:
            delay = max(delay, (retry_after_until - self._now()).total_seconds())
        self._next_due[source_id] = self._clock() + delay
        record = self._snapshot(job, record)
        try:
            self._store.save(record)
        except Exception as error:  # noqa: BLE001 - persistence failure is non-fatal
            _logger.warning("ingestion state not persisted: %s", type(error).__name__)
        return build_source_status(entry, record, self._now())

    async def run_due(self) -> list[SourceStatus]:
        out: list[SourceStatus] = []
        if not self.network_allowed:
            return out
        for entry in self._registry.enabled():
            if entry.source_id in self._jobs and (
                self._clock() >= self._next_due.get(entry.source_id, float("-inf"))
            ):
                out.append(await self.run_once(entry.source_id))
        return out

    async def run_forever(self, stop: asyncio.Event, tick: float = 1.0) -> None:
        failures = 0
        while not stop.is_set():
            pause = tick
            try:
                await self.run_due()
                failures = 0
            except Exception as error:  # noqa: BLE001 - the loop itself must survive
                failures += 1
                pause = min(tick * 2**failures, 60.0)
                _logger.warning("ingestion cycle failed: %s", type(error).__name__)
            try:
                await asyncio.wait_for(stop.wait(), timeout=pause)
            except TimeoutError:
                pass

    def status(self, source_id: str) -> SourceStatus:
        entry = self._registry.get(source_id)
        try:
            record = self._store.load(source_id)
        except Exception:  # noqa: BLE001 - status must stay readable
            record = IngestRecord(source_id)
        job = self._jobs.get(source_id)
        if job is not None:
            record = self._snapshot(job, record)
        return build_source_status(entry, record, self._now())

    def all_status(self) -> list[SourceStatus]:
        return [self.status(e.source_id) for e in self._registry.all()]
