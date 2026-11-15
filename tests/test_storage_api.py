import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hcs_trader.api import create_app
from hcs_trader.data.kraken import BTC_USD
from hcs_trader.data.quotes import QuoteService
from hcs_trader.domain.assets import MarketId
from hcs_trader.domain.market_data import Quote
from hcs_trader.storage import AuditStore


def test_migration_persists_history_and_backup_restores_it(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    store = AuditStore(path)
    store.initialize()
    assert store.latest_event() is None
    event = store.record_startup()

    restarted = AuditStore(path)
    restarted.initialize()
    assert restarted.latest_event() == event

    backup = tmp_path / "backup.sqlite"
    restarted.backup(backup)
    restored = AuditStore(backup)
    restored.initialize()
    assert restored.latest_event() == event
    with pytest.raises(FileExistsError):
        restored.backup(backup)

    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)


def test_migration_rejects_unknown_future_schema(tmp_path: Path) -> None:
    path = tmp_path / "future.sqlite"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA user_version = 99")
    with pytest.raises(RuntimeError, match="Unsupported"):
        AuditStore(path).initialize()


def test_api_exposes_loopback_status_and_openapi(tmp_path: Path) -> None:
    store = AuditStore(tmp_path / "app.sqlite")
    with TestClient(create_app(store), base_url="http://127.0.0.1") as client:
        assert client.get("/api/v1/health").json() == {"status": "ok"}
        assert client.get("/api/v1/system/status").json()["mode"] == "PAPER"
        assert client.get("/api/v1/system/config").json() == {
            "host": "127.0.0.1",
            "port": 8765,
            "default_mode": "PAPER",
        }
        assert client.get("/api/v1/system/capabilities").json()["live_orders"] is False
        assert client.get("/api/v1/market/quotes").status_code == 503
        assert "/api/v1/health" in client.get("/openapi.json").json()["paths"]
        assert client.post("/api/v1/orders/live").status_code == 404
        assert (
            client.get("/api/v1/health", headers={"Host": "untrusted.test"}).status_code
            == 400
        )
        assert (
            client.get(
                "/api/v1/health", headers={"Origin": "https://untrusted.test"}
            ).headers.get("access-control-allow-origin")
            is None
        )
        event = store.latest_event()
        assert event is not None
        assert event.kind == "app.started"

        with closing(sqlite3.connect(store.path)) as connection, connection:
            connection.execute("DROP TABLE audit_events")
        assert client.get("/api/v1/health").json() == {
            "detail": "Audit storage unavailable"
        }
        assert client.get("/api/v1/health").status_code == 503


def test_static_ui_shares_origin_with_api(tmp_path: Path) -> None:
    assets = tmp_path / "ui"
    assets.mkdir()
    (assets / "index.html").write_text("<h1>Local dashboard</h1>", encoding="utf-8")
    store = AuditStore(tmp_path / "app.sqlite")
    with TestClient(create_app(store, assets), base_url="http://127.0.0.1") as client:
        assert client.get("/").text == "<h1>Local dashboard</h1>"
        assert client.get("/api/v1/health").json() == {"status": "ok"}


def test_opt_in_quote_api_serializes_exact_prices_and_quality(tmp_path: Path) -> None:
    class Provider:
        async def get_quote(self, market: MarketId) -> Quote:
            now = datetime.now(UTC)
            return Quote(
                market,
                "kraken-spot",
                Decimal("12.3400"),
                Decimal("12.3501"),
                now,
                now,
                now,
            )

    store = AuditStore(tmp_path / "app.sqlite")
    app = create_app(store, quote_service=QuoteService(Provider(), BTC_USD))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/api/v1/system/status").json() == {
            "mode": "DATA_ONLY",
            "execution_available": False,
            "network_enabled": True,
        }
        assert client.get("/api/v1/system/capabilities").json()["market_data"] is True
        response = client.get("/api/v1/market/quotes")
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["quality"] == "FRESH"
        assert response.json()["bid"] == "12.3400"
        assert response.json()["ask"] == "12.3501"
