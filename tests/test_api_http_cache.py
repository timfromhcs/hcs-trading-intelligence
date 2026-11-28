"""Wave 5: ETag/conditional GET and cursor pagination on read endpoints."""

from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from hcs_trader.api import create_app
from hcs_trader.domain.news import NewsItem
from hcs_trader.storage import AuditStore


def _client(tmp_path: Path) -> tuple[TestClient, AuditStore]:
    store = AuditStore(tmp_path / "audit.sqlite")
    store.initialize()
    now = datetime.now(UTC)
    store.record_news(
        [
            NewsItem(
                "fixture",
                f"https://example.org/news/{index}",
                f"Bitcoin note {index}",
                now,
                now,
            )
            for index in range(6)
        ]
    )
    return TestClient(create_app(store), base_url="http://127.0.0.1"), store


def test_get_has_content_etag_and_conditional_304(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    with client:
        first = client.get("/api/v1/news/search", params={"q": "Bitcoin"})
        assert first.status_code == 200
        etag = first.headers.get("etag")
        assert etag and etag.startswith('"')
        second = client.get(
            "/api/v1/news/search",
            params={"q": "Bitcoin"},
            headers={"If-None-Match": etag},
        )
        assert second.status_code == 304
        assert second.headers["etag"] == etag
        assert second.content == b""


def test_etag_changes_when_content_changes(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    with client:
        first = client.get("/api/v1/audit/events", params={"limit": 5})
        etag = first.headers["etag"]
        store.record_audit_event("wave5.check")
        second = client.get("/api/v1/audit/events", params={"limit": 5})
        assert second.status_code == 200
        assert second.headers["etag"] != etag


def test_news_search_cursor_pagination(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    with client:
        first = client.get("/api/v1/news/search", params={"q": "Bitcoin", "limit": 2})
        assert first.status_code == 200
        assert len(first.json()) == 2
        cursor = first.headers["x-next-cursor"]
        second = client.get(
            "/api/v1/news/search",
            params={"q": "Bitcoin", "limit": 2, "cursor": cursor},
        )
        assert second.status_code == 200
        assert {row["url"] for row in first.json()}.isdisjoint(
            row["url"] for row in second.json()
        )


def test_audit_events_cursor_pagination(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    for index in range(6):
        store.record_audit_event(f"wave5.event.{index}")
    with client:
        first = client.get("/api/v1/audit/events", params={"limit": 2})
        cursor = first.headers["x-next-cursor"]
        second = client.get(
            "/api/v1/audit/events", params={"limit": 2, "cursor": cursor}
        )
        assert len(first.json()) == len(second.json()) == 2
        assert {row["event_id"] for row in first.json()}.isdisjoint(
            row["event_id"] for row in second.json()
        )


def test_malformed_cursor_is_rejected(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    with client:
        response = client.get(
            "/api/v1/news/search", params={"q": "Bitcoin", "cursor": "not-a-cursor"}
        )
    assert response.status_code == 422
