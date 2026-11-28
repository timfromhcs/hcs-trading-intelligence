"""Wave 5: bounded loopback SSE snapshot stream."""

from pathlib import Path

from fastapi.testclient import TestClient

from hcs_trader.api import create_app
from hcs_trader.storage import AuditStore


def test_system_stream_emits_bounded_snapshots(tmp_path: Path) -> None:
    store = AuditStore(tmp_path / "audit.sqlite")
    store.initialize()
    store.set_execution_pause(True)
    client = TestClient(create_app(store), base_url="http://127.0.0.1")
    with client.stream(
        "GET", "/api/v1/system/stream", params={"max_events": 2}
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-store"
        body = "".join(response.iter_lines())
    assert body.count("event: snapshot") == 2
    assert '"paused": true' in body
    assert '"mode": "PAPER"' in body


def test_system_stream_rejects_unbounded_query(tmp_path: Path) -> None:
    store = AuditStore(tmp_path / "audit.sqlite")
    store.initialize()
    client = TestClient(create_app(store), base_url="http://127.0.0.1")
    response = client.get("/api/v1/system/stream", params={"max_events": 1000})
    assert response.status_code == 422
