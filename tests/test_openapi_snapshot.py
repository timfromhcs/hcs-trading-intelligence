"""Committed OpenAPI snapshot detects accidental API contract drift."""

import json
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from hcs_trader.api import create_app
from hcs_trader.domain.news import NewsItem
from hcs_trader.storage import AuditStore

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "tests" / "fixtures" / "openapi-snapshot.json"


def test_openapi_matches_committed_snapshot(tmp_path: Path) -> None:
    store = AuditStore(tmp_path / "audit.sqlite")
    store.initialize()
    now = datetime.now(UTC)
    store.record_news(
        [NewsItem("fixture", "https://example.org/a", "Headline", now, now)]
    )
    app = create_app(store)
    with TestClient(app, base_url="http://127.0.0.1"):
        actual = json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"
    expected = SNAPSHOT.read_text(encoding="utf-8")
    assert actual == expected, (
        "OpenAPI schema drift. Regenerate tests/fixtures/openapi-snapshot.json "
        "only together with a reviewed API change."
    )


def test_snapshot_contains_wave3_and_wave5_routes() -> None:
    schema = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    paths = schema["paths"]
    assert "/api/v1/webfetch/sources" in paths
    assert "/api/v1/webfetch/{source_id}" in paths
    assert "/api/v1/news/search" in paths
    search = paths["/api/v1/news/search"]["get"]["parameters"]
    assert {parameter["name"] for parameter in search} >= {"q", "limit", "cursor"}
    assert schema["info"]["version"] == "1.1.0rc1"
