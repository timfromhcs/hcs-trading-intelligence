"""Ingestion runtime: default feed jobs and a start/stop service."""

import asyncio
import contextlib
import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Any

import httpx2

from hcs_trader.data.ecb import fetch_ecb_rates
from hcs_trader.data.fed_news import fetch_fed_news
from hcs_trader.data.feed_status import IngestRecord
from hcs_trader.data.ingestion import IngestionScheduler, SourceJob
from hcs_trader.data.sources import DEFAULT_REGISTRY, SourceRegistry
from hcs_trader.network_policy import NetworkPolicy
from hcs_trader.storage import AuditStore

_logger = logging.getLogger(__name__)


class AuditIngestState:
    """Adapts the audit database (migration 20) to the scheduler's state backend."""

    def __init__(self, store: AuditStore) -> None:
        self._store = store

    def load(self, source_id: str) -> IngestRecord:
        return self._store.load_ingest_state(source_id)

    def save(self, state: IngestRecord) -> None:
        self._store.save_ingest_state(state)


def _newest_news(items: Sequence[Any]) -> datetime | None:
    return max((item.published_at for item in items), default=None)


def default_jobs(
    client: httpx2.AsyncClient, store: AuditStore, registry: SourceRegistry
) -> list[SourceJob]:
    """Existing providers only: Federal Reserve news and ECB reference rates."""
    return [
        SourceJob(
            registry.get("federal-reserve-monetary-policy"),
            lambda: fetch_fed_news(client),
            lambda items: store.record_news(list(items)),
            _newest_news,
        ),
        SourceJob(
            registry.get("ecb-sdmx"),
            lambda: fetch_ecb_rates(client),
            lambda items: store.record_macro(list(items)),
        ),
    ]


class IngestionService:
    """Owns the background scheduler task; explicit opt-in, cleanly cancelled."""

    def __init__(
        self,
        store: AuditStore,
        client: httpx2.AsyncClient,
        network_policy: NetworkPolicy,
        *,
        registry: SourceRegistry = DEFAULT_REGISTRY,
        tick: float = 1.0,
        scheduler: IngestionScheduler | None = None,
        own_client: bool = False,
    ) -> None:
        self.network_policy = network_policy
        self._client = client if own_client else None
        self.scheduler = scheduler or IngestionScheduler(
            registry,
            default_jobs(client, store, registry),
            AuditIngestState(store),
            network_policy=network_policy,
        )
        self._tick = tick
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is not None:
            return
        self.scheduler.restore()
        self._stop.clear()
        self._task = asyncio.create_task(
            self.scheduler.run_forever(self._stop, self._tick), name="hcs-ingestion"
        )

    async def stop(self, timeout: float = 5.0) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(task, timeout)
        except (TimeoutError, asyncio.CancelledError):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        finally:
            if self._client is not None:
                await self._client.aclose()
