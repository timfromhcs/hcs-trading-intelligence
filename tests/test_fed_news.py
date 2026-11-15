from pathlib import Path

import httpx2
import pytest
from fastapi.testclient import TestClient

from hcs_trader.api import create_app
from hcs_trader.data.fed_news import FEED_URL, SOURCE, NewsUnavailable, fetch_fed_news
from hcs_trader.storage import AuditStore

_FEED = """<rss version="2.0"><channel>
<item><title>Monetary policy &amp; markets</title>
<link>https://www.federalreserve.gov/newsevents/pressreleases/monetary20200101a.htm</link>
<pubDate>Wed, 01 Jan 2020 12:00:00 EST</pubDate></item>
<item><title>Foreign destination</title>
<link>https://example.invalid/foreign</link>
<pubDate>Wed, 01 Jan 2020 12:00:00 GMT</pubDate></item>
</channel></rss>"""


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_official_feed_round_trip_and_offline_api(tmp_path: Path) -> None:
    def respond(request: httpx2.Request) -> httpx2.Response:
        assert request.method == "GET" and str(request.url) == FEED_URL
        return httpx2.Response(200, content=_FEED)

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as client:
        items = await fetch_fed_news(client)
    assert len(items) == 1
    assert items[0].source == SOURCE
    assert items[0].title == "Monetary policy & markets"
    assert items[0].published_at.isoformat() == "2020-01-01T17:00:00+00:00"

    store = AuditStore(tmp_path / "news.sqlite")
    store.initialize()
    assert store.record_news(items) == 1
    assert store.record_news(items) == 0
    with TestClient(
        create_app(AuditStore(store.path)), base_url="http://127.0.0.1"
    ) as api:
        assert api.get("/api/v1/system/status").json()["network_enabled"] is False
        (item,) = api.get("/api/v1/news/events").json()
        assert item["url"] == items[0].url
        assert item["published_at"] == "2020-01-01T17:00:00Z"
        assert item["received_at"].endswith("Z")
        assert api.get("/api/v1/news/events?limit=101").status_code == 422


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status", "body"),
    [
        (302, ""),
        (503, ""),
        (200, "<!DOCTYPE rss><rss><channel/></rss>"),
        (200, "<rss>"),
        (200, "<html></html>"),
        (200, " " * 512_001),
    ],
)
async def test_feed_rejects_invalid_or_overlarge_documents(
    status: int, body: str
) -> None:
    def respond(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(status, content=body, headers={"location": FEED_URL})

    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(respond), follow_redirects=True
    ) as client:
        with pytest.raises(NewsUnavailable):
            await fetch_fed_news(client)
