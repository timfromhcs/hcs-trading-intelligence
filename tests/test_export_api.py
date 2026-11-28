"""Downloadable, secret-free JSON exports."""

from pathlib import Path

from fastapi.testclient import TestClient

from hcs_trader.api import create_app
from hcs_trader.storage import AuditStore


def test_audit_export_is_an_attachment(tmp_path: Path) -> None:
    store = AuditStore(tmp_path / "audit.sqlite")
    store.initialize()
    store.record_audit_event("export.check")
    client = TestClient(create_app(store), base_url="http://127.0.0.1")
    response = client.get("/api/v1/export/audit")
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    assert response.headers["cache-control"] == "no-store"
    rows = response.json()
    assert rows and rows[0]["kind"] == "export.check"
    assert "secret" not in response.text.lower()
