"""Read-only regime endpoint parity with the deterministic module."""

from pathlib import Path

from fastapi.testclient import TestClient

from hcs_trader.api import create_app
from hcs_trader.storage import AuditStore


def _client(tmp_path: Path) -> TestClient:
    store = AuditStore(tmp_path / "audit.sqlite")
    store.initialize()
    return TestClient(create_app(store), base_url="http://127.0.0.1")


def test_regime_endpoint_returns_trailing_points(tmp_path: Path) -> None:
    client = _client(tmp_path)
    response = client.get(
        "/api/v1/quant/regime", params={"interval": 60, "limit": 30, "lookback": 5}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["lookback"] == 5
    assert body["points"] == []  # no stored candles yet


def test_regime_endpoint_rejects_invalid_lookback(tmp_path: Path) -> None:
    client = _client(tmp_path)
    assert client.get("/api/v1/quant/regime", params={"lookback": 1}).status_code == 422
