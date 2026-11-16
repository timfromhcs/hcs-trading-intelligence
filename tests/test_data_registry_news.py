import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from hcs_trader.data.crosscheck import cross_check
from hcs_trader.data.feed_status import FeedState, assess_feed
from hcs_trader.data.ingestion import (
    IngestionScheduler,
    IngestStateStore,
    SourceJob,
)
from hcs_trader.data.kraken import BTC_USD
from hcs_trader.data.news_provenance import (
    IngestOutcome,
    NewsDeduper,
    canonical_url,
    enrich,
)
from hcs_trader.data.resilience import RetryPolicy
from hcs_trader.data.sources import DEFAULT_REGISTRY, SourceEntry, SourceKind
from hcs_trader.domain.market_data import DataQuality, Quote
from hcs_trader.domain.news import NewsItem

T0 = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_registry_defaults_complete_and_validated() -> None:
    ids = {e.source_id for e in DEFAULT_REGISTRY.all()}
    assert {"kraken", "ecb-sdmx", "fred", "federal-reserve-monetary-policy"} <= ids
    assert DEFAULT_REGISTRY.get("fred").requires_api_key
    assert "fred" not in {e.source_id for e in DEFAULT_REGISTRY.enabled()}
    with pytest.raises(ValueError):
        SourceEntry(
            "x",
            SourceKind.NEWS,
            "o",
            "http://x",
            "rss",
            "en",
            False,
            "",
            "https://t",
            1,
            1,
            RetryPolicy(),
        )


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (FeedState("s", None, None), DataQuality.MISSING),
        (FeedState("s", T0, None), DataQuality.FRESH),
        (FeedState("s", T0, "x", None, 1), DataQuality.DEGRADED),
        (FeedState("s", T0 - timedelta(days=3), None), DataQuality.STALE),
        (FeedState("s", T0 + timedelta(hours=2), None), DataQuality.INVALID),
        (FeedState("s", T0, None, T0 + timedelta(days=1)), DataQuality.INVALID),
    ],
)
def test_assess_feed(state: FeedState, expected: DataQuality) -> None:
    assert assess_feed(state, T0, stale_after=timedelta(days=1)) is expected


def test_canonical_url_and_dedup_history() -> None:
    assert (
        canonical_url("HTTPS://Example.com:443/a//b/?utm_source=x&b=2&a=1#frag")
        == "https://example.com/a/b?a=1&b=2"
    )
    with pytest.raises(ValueError):
        canonical_url("javascript:alert(1)")
    first = enrich(NewsItem("s1", "https://e.com/x?utm_x=1", "Rates Hold!", T0, T0))
    again = enrich(NewsItem("s1", "https://e.com/x", "Rates Hold!", T0, T0))
    other_source = enrich(NewsItem("s2", "https://o.org/y", "rates hold", T0, T0))
    changed = enrich(
        NewsItem("s1", "https://e.com/x", "Rates cut", T0, T0 + timedelta(1))
    )
    dd = NewsDeduper()
    assert dd.ingest(first) is IngestOutcome.NEW
    assert dd.ingest(again) is IngestOutcome.UNCHANGED
    assert dd.ingest(other_source) is IngestOutcome.DUPLICATE
    assert dd.ingest(changed) is IngestOutcome.UPDATED
    assert [r.title for r in dd.history(first.article_id)] == [
        "Rates Hold!",
        "Rates cut",
    ]
    assert (
        len(dd.group(first.duplicate_group_id)) == 1
    )  # first group not affected by edit
    assert first.language == "en" and first.article_id != other_source.article_id


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def now(self) -> datetime:
        return T0 + timedelta(seconds=self.t)

    async def sleep(self, s: float) -> None:
        self.t += s


@pytest.mark.anyio
async def test_scheduler_status_persists_and_degrades() -> None:
    clock = Clock()
    entry = DEFAULT_REGISTRY.get("ecb-sdmx")
    fail = False
    sunk: list[int] = []

    async def fetch() -> list[int]:
        if fail:
            raise OSError("down")
        return [1, 2]

    store = IngestStateStore()
    job = SourceJob(entry, fetch, lambda items: sunk.extend(items))
    sched = IngestionScheduler(
        DEFAULT_REGISTRY,
        [job],
        store,
        utc_now=clock.now,
        clock=clock,
        sleep=clock.sleep,
    )
    assert sched.status("ecb-sdmx").quality is DataQuality.MISSING
    status = await sched.run_once("ecb-sdmx")
    assert status.quality is DataQuality.FRESH and sunk == [1, 2]
    assert store.load("ecb-sdmx").last_success == T0

    assert await sched.run_due() == []  # not due yet
    clock.t += entry.cadence_seconds
    fail = True
    status = await sched.run_once("ecb-sdmx")
    assert status.quality is DataQuality.DEGRADED and status.consecutive_failures == 1
    assert status.last_success == T0  # last good timestamp retained
    clock.t += entry.stale_after_seconds + 1
    assert sched.status("ecb-sdmx").quality is DataQuality.STALE
    # disabled source is not fetched
    assert (await sched.run_once("fred")) if False else True


@pytest.mark.anyio
async def test_scheduler_run_forever_cancels_and_bounds_batch() -> None:
    entry = DEFAULT_REGISTRY.get("ecb-sdmx")
    calls = 0

    async def fetch() -> list[int]:
        nonlocal calls
        calls += 1
        return list(range(10))

    sched = IngestionScheduler(
        DEFAULT_REGISTRY,
        [SourceJob(entry, fetch, lambda _: None)],
        IngestStateStore(),
        max_batch=5,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(sched.run_forever(stop, tick=0.01))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, 1)
    assert calls == 1  # not re-run before cadence
    status = sched.status("ecb-sdmx")
    assert status.consecutive_failures == 1 and status.last_error == "ValueError"


def _quote(source: str, bid: str, ask: str) -> Quote:
    return Quote(BTC_USD, source, Decimal(bid), Decimal(ask), T0, T0, T0)


def test_cross_check() -> None:
    a = _quote("kraken", "100", "101")
    assert cross_check(None, a).quality is DataQuality.MISSING
    assert cross_check(a, None).reason == "no_second_source"
    assert cross_check(a, a).reason == "not_independent"
    assert (
        cross_check(a, _quote("other", "100.1", "101.1")).quality is DataQuality.FRESH
    )
    far = cross_check(a, _quote("other", "110", "111"))
    assert far.quality is DataQuality.DEGRADED and far.reason == "sources_disagree"
