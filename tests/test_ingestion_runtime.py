"""Ingestion runtime: persisted state, restart, sink faults, offline, status API."""

import asyncio
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient
from mcp import Client

from hcs_trader.api import create_app
from hcs_trader.data.feed_status import IngestRecord
from hcs_trader.data.ingestion import IngestionScheduler, SourceJob
from hcs_trader.data.provider import SourceRateLimited
from hcs_trader.data.resilience import CircuitBreaker, CircuitState
from hcs_trader.data.runtime import AuditIngestState, IngestionService
from hcs_trader.data.sources import DEFAULT_REGISTRY
from hcs_trader.domain.market_data import DataQuality
from hcs_trader.mcp_server import CapabilityTier, create_mcp_server, tool_tier
from hcs_trader.network_policy import NetworkPolicy, SystemMode, build_http_client
from hcs_trader.storage import AuditStore

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
ECB = DEFAULT_REGISTRY.get("ecb-sdmx")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def now(self) -> datetime:
        return T0 + timedelta(seconds=self.t)

    async def sleep(self, seconds: float) -> None:
        self.t += seconds


class Limited(SourceRateLimited):
    def __init__(self, retry_after: float | None) -> None:
        super().__init__("limited")
        self.retry_after = retry_after


def _store(tmp_path: Path) -> AuditStore:
    store = AuditStore(tmp_path / "audit.sqlite")
    store.initialize()
    return store


def _scheduler(
    store: AuditStore, clock: Clock, job: SourceJob, **kwargs: Any
) -> IngestionScheduler:
    return IngestionScheduler(
        DEFAULT_REGISTRY,
        [job],
        AuditIngestState(store),
        utc_now=clock.now,
        clock=clock,
        sleep=clock.sleep,
        **kwargs,
    )


@pytest.mark.anyio
async def test_429_deadline_survives_restart(tmp_path: Path) -> None:
    clock = Clock()
    calls = 0

    async def limited() -> list[int]:
        nonlocal calls
        calls += 1
        raise Limited(600)

    store = _store(tmp_path)
    first = _scheduler(store, clock, SourceJob(ECB, limited, lambda _: None))
    status = await first.run_once("ecb-sdmx")
    assert calls == 1  # a rate limit is never retried in-process
    assert status.retry_in_seconds == pytest.approx(600)
    assert status.last_error == "Limited" and status.quality is DataQuality.MISSING

    # "Restart": a new store handle, scheduler and job over the same database file.
    reopened = AuditStore(tmp_path / "audit.sqlite")
    restarted_calls = 0

    async def fetch() -> list[int]:
        nonlocal restarted_calls
        restarted_calls += 1
        return [1]

    clock.t += 100
    second = _scheduler(reopened, clock, SourceJob(ECB, fetch, lambda _: None))
    second.restore()
    assert await second.run_due() == []  # not due: deadline restored
    blocked = await second.run_once("ecb-sdmx")
    assert restarted_calls == 0
    assert blocked.retry_in_seconds == pytest.approx(500)

    clock.t += 501
    done = await second.run_once("ecb-sdmx")
    assert restarted_calls == 1 and done.quality is DataQuality.FRESH
    assert done.retry_in_seconds is None and done.consecutive_failures == 0


@pytest.mark.anyio
async def test_sink_failure_is_recorded_and_loop_survives(tmp_path: Path) -> None:
    clock = Clock()
    store = _store(tmp_path)
    broken = True
    sunk: list[int] = []

    def sink(items: Any) -> None:
        if broken:
            raise sqlite3.OperationalError("database is locked")
        sunk.extend(items)

    async def fetch() -> list[int]:
        return [1, 2]

    scheduler = _scheduler(store, clock, SourceJob(ECB, fetch, sink))
    status = await scheduler.run_once("ecb-sdmx")
    assert status.last_error == "OperationalError" and status.consecutive_failures == 1
    assert sunk == []
    clock.t += ECB.cadence_seconds * 4
    broken = False
    status = await scheduler.run_once("ecb-sdmx")
    assert status.consecutive_failures == 0 and sunk == [1, 2]


@pytest.mark.anyio
async def test_run_forever_survives_state_store_errors() -> None:
    class Flaky:
        def __init__(self) -> None:
            self.loads = 0

        def load(self, source_id: str) -> IngestRecord:
            self.loads += 1
            if self.loads <= 2:
                raise sqlite3.OperationalError("locked")
            return IngestRecord(source_id)

        def save(self, state: IngestRecord) -> None:
            raise sqlite3.OperationalError("locked")

    ran = 0

    async def fetch() -> list[int]:
        nonlocal ran
        ran += 1
        return [1]

    scheduler = IngestionScheduler(
        DEFAULT_REGISTRY, [SourceJob(ECB, fetch, lambda _: None)], Flaky()
    )
    stop = asyncio.Event()
    task = asyncio.create_task(scheduler.run_forever(stop, tick=0.01))
    for _ in range(200):
        await asyncio.sleep(0.02)
        if ran:
            break
    assert not task.done()  # load and save faults never end the loop
    assert ran == 1
    stop.set()
    await asyncio.wait_for(task, 2)


@pytest.mark.anyio
async def test_offline_mode_never_polls(tmp_path: Path) -> None:
    requests: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(str(request.url))
        return httpx2.Response(500)

    policy = NetworkPolicy(SystemMode.OFFLINE)
    client = build_http_client(policy, transport=httpx2.MockTransport(handler))
    store = _store(tmp_path)
    service = IngestionService(store, client, policy, tick=0.01, own_client=True)
    service.start()
    await asyncio.sleep(0.1)
    await service.stop()
    assert requests == []
    statuses = {s.source_id: s for s in service.scheduler.all_status()}
    assert statuses["ecb-sdmx"].quality is DataQuality.MISSING
    assert store.list_ingest_state() == []


def test_breaker_snapshot_round_trips_through_restart() -> None:
    clock = Clock()
    breaker = CircuitBreaker(failure_threshold=1, reset_timeout=30, clock=clock)
    breaker.before_call()
    breaker.record_failure()
    assert breaker.state is CircuitState.OPEN and breaker.retry_in == 30
    revived = CircuitBreaker(failure_threshold=1, reset_timeout=30, clock=clock)
    revived.force_open(12)
    assert revived.state is CircuitState.OPEN and revived.retry_in == pytest.approx(12)
    clock.t += 13
    reopened_state: CircuitState = revived.state
    assert reopened_state is CircuitState.HALF_OPEN


@pytest.mark.anyio
async def test_status_reports_breaker_and_retry_in(tmp_path: Path) -> None:
    clock = Clock()
    store = _store(tmp_path)

    async def fail() -> list[int]:
        raise OSError("down")

    job = SourceJob(
        ECB,
        fail,
        lambda _: None,
        breaker=CircuitBreaker(failure_threshold=2, clock=clock),
    )
    scheduler = _scheduler(store, clock, job)
    status = await scheduler.run_once("ecb-sdmx")
    assert status.breaker_state == "OPEN"  # opened within the retry budget
    assert status.retry_in_seconds is not None and status.retry_in_seconds > 0
    persisted = store.load_ingest_state("ecb-sdmx")
    assert persisted.breaker_state == "OPEN" and persisted.breaker_open_until


def _record(**kwargs: Any) -> IngestRecord:
    return IngestRecord("ecb-sdmx", **kwargs)


def test_migration_20_upgrade_and_backup_restore(tmp_path: Path) -> None:
    path = tmp_path / "audit.sqlite"
    store = AuditStore(path)
    store.initialize()
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("DROP TABLE ingest_state")
        connection.execute("PRAGMA user_version = 19")
    store.initialize()  # upgrade from 19
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (20,)
    saved = _record(
        last_success=T0,
        last_error="Limited",
        consecutive_failures=2,
        retry_after_until=T0 + timedelta(minutes=10),
        breaker_state="OPEN",
        breaker_open_until=T0 + timedelta(seconds=30),
    )
    store.save_ingest_state(saved)
    backup = tmp_path / "backup.sqlite"
    store.backup(backup)
    restored = AuditStore(backup)
    restored.initialize()  # idempotent on an already migrated copy
    assert restored.load_ingest_state("ecb-sdmx") == saved
    assert restored.list_ingest_state() == [saved]
    assert store.load_ingest_state("fred") == IngestRecord("fred")


def test_read_only_store_tolerates_pre_migration_database(tmp_path: Path) -> None:
    path = tmp_path / "audit.sqlite"
    AuditStore(path).initialize()
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("DROP TABLE ingest_state")
        connection.execute("PRAGMA user_version = 19")
    assert AuditStore(path, read_only=True).list_ingest_state() == []


def test_data_sources_api_contract(tmp_path: Path) -> None:
    store = _store(tmp_path)
    now = datetime.now(UTC)
    store.save_ingest_state(
        _record(
            last_success=now - timedelta(seconds=30),
            last_error="Limited",
            consecutive_failures=1,
            retry_after_until=now + timedelta(minutes=5),
        )
    )
    with TestClient(create_app(store), base_url="http://127.0.0.1") as client:
        response = client.get("/api/v1/data/sources")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["ingestion_enabled"] is False and body["ingestion_mode"] is None
    by_id = {row["source_id"]: row for row in body["sources"]}
    assert set(by_id) == {e.source_id for e in DEFAULT_REGISTRY.all()}
    ecb = by_id["ecb-sdmx"]
    assert ecb["quality"] == "DEGRADED"
    assert ecb["breaker_state"] == "CLOSED"
    assert 290 < ecb["retry_in_seconds"] <= 300
    assert by_id["fred"]["enabled"] is False and by_id["fred"]["quality"] == "MISSING"
    assert by_id["fred"]["requires_api_key"] is True
    assert {
        "source_id",
        "kind",
        "quality",
        "last_success",
        "last_error",
        "consecutive_failures",
        "enabled",
        "cadence_seconds",
        "stale_after_seconds",
        "breaker_state",
        "retry_after_until",
        "retry_in_seconds",
    } <= set(ecb)


def test_lifespan_runs_and_cancels_ingestion_end_to_end(tmp_path: Path) -> None:
    store = _store(tmp_path)
    seen: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.url.host)
        if request.url.host == "data-api.ecb.europa.eu":
            return httpx2.Response(429, headers={"Retry-After": "900"})
        return httpx2.Response(500)

    policy = NetworkPolicy(SystemMode.DATA_ONLY)
    client = build_http_client(policy, transport=httpx2.MockTransport(handler))
    service = IngestionService(store, client, policy, tick=0.01, own_client=True)
    with TestClient(
        create_app(store, ingestion=service), base_url="http://127.0.0.1"
    ) as api:
        for _ in range(100):
            body = api.get("/api/v1/data/sources").json()
            ecb = next(s for s in body["sources"] if s["source_id"] == "ecb-sdmx")
            if ecb["retry_in_seconds"]:
                break
            import time

            time.sleep(0.05)
        assert (
            body["ingestion_enabled"] is True and body["ingestion_mode"] == "DATA_ONLY"
        )
        assert 800 < ecb["retry_in_seconds"] <= 900
        assert ecb["last_error"] == "MacroRateLimited"
    assert client.is_closed  # the lifespan stopped the task and closed the client
    assert seen.count("data-api.ecb.europa.eu") == 1
    # A second boot over the same database must not call the limited provider.
    seen.clear()
    client2 = build_http_client(policy, transport=httpx2.MockTransport(handler))
    service2 = IngestionService(store, client2, policy, tick=0.01, own_client=True)
    with TestClient(create_app(store, ingestion=service2), base_url="http://127.0.0.1"):
        import time

        time.sleep(0.3)
    assert "data-api.ecb.europa.eu" not in seen


@pytest.mark.anyio
async def test_mcp_data_source_status_tool(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_ingest_state(_record(last_success=datetime.now(UTC)))
    assert tool_tier("data.get_source_status") is CapabilityTier.READ
    async with Client(create_mcp_server(store.path)) as client:
        result = await client.call_tool("data.get_source_status")
        assert not result.is_error
        text = result.content[0].text  # type: ignore[union-attr]
        report = json.loads(text)
        by_id = {row["source_id"]: row for row in report["sources"]}
        assert by_id["ecb-sdmx"]["quality"] == "FRESH"
        assert by_id["federal-reserve-monetary-policy"]["quality"] == "MISSING"


@pytest.mark.anyio
async def test_quote_service_uses_breaker_and_restores_rate_limit(
    tmp_path: Path,
) -> None:
    from hcs_trader.data.kraken import BTC_USD
    from hcs_trader.data.provider import QuoteRateLimited
    from hcs_trader.data.quotes import QuoteService

    clock = Clock()
    store = _store(tmp_path)
    calls = 0

    class Limited429:
        async def get_quote(self, market: Any) -> Any:
            nonlocal calls
            calls += 1
            raise QuoteRateLimited("limited")

    service = QuoteService(
        Limited429(),
        BTC_USD,
        elapsed=clock,
        utc_now=clock.now,
        record_state=store.save_ingest_state,
    )
    snapshot = await service.snapshot()
    assert snapshot.provider_issue == "rate_limited" and calls == 1
    record = store.load_ingest_state("kraken")
    assert record.last_error == "rate_limited" and record.retry_after_until is not None
    assert record.breaker_state == "CLOSED" and record.consecutive_failures == 1

    # Restart 10 s later: the persisted deadline suppresses the first request.
    clock.t += 10
    revived = QuoteService(
        Limited429(),
        BTC_USD,
        elapsed=clock,
        utc_now=clock.now,
        record_state=store.save_ingest_state,
    )
    revived.restore_state(AuditStore(store.path).load_ingest_state("kraken"))
    await revived.snapshot()
    assert calls == 1
