import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hcs_trader.api import create_app
from hcs_trader.data.kraken import BTC_USD
from hcs_trader.data.provider import QuoteUnavailable
from hcs_trader.data.quotes import QuoteService
from hcs_trader.domain.assets import MarketId
from hcs_trader.domain.execution import SpotOrder, SpotOrderType, SpotSide
from hcs_trader.domain.lifecycle import OrderState
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
        assert connection.execute("PRAGMA user_version").fetchone() == (12,)


def test_v1_upgrade_retains_audit_and_deduplicates_quotes(tmp_path: Path) -> None:
    path = tmp_path / "v1.sqlite"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "CREATE TABLE audit_events (event_id TEXT PRIMARY KEY NOT NULL, "
            "recorded_at TEXT NOT NULL, kind TEXT NOT NULL) STRICT"
        )
        connection.execute(
            "INSERT INTO audit_events VALUES ('fixture', '2026-01-01Z', 'app.started')"
        )
        connection.execute("PRAGMA user_version = 1")

    store = AuditStore(path)
    store.initialize()
    event = store.latest_event()
    assert event is not None and event.event_id == "fixture"
    now = datetime.now(UTC)
    first = Quote(
        BTC_USD, "kraken-spot", Decimal("12.3400"), Decimal("12.3501"), now, now, now
    )
    store.record_quote(first)
    store.record_quote(replace(first, received_at=now + timedelta(seconds=2)))
    second = replace(first, bid=Decimal("12.3401"))
    store.record_quote(second)
    reopened = AuditStore(path)
    reopened.initialize()
    assert reopened.quote_history(BTC_USD, limit=2) == [second, first]
    assert reopened.quote_history(BTC_USD, limit=1) == [second]


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
    service = QuoteService(Provider(), BTC_USD, record_quote=store.record_quote)
    app = create_app(store, quote_service=service)
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
        history = client.get("/api/v1/market/quotes/history?limit=1").json()
        assert history[0]["bid"] == "12.3400"
        assert client.get("/api/v1/market/quotes/history?limit=501").status_code == 422

    with TestClient(create_app(store), base_url="http://127.0.0.1") as client:
        assert client.get("/api/v1/market/quotes/history").json() == history


def test_restart_uses_last_persisted_quote_during_outage(tmp_path: Path) -> None:
    class Outage:
        async def get_quote(self, market: MarketId) -> Quote:
            raise QuoteUnavailable("fixture outage")

    store = AuditStore(tmp_path / "app.sqlite")
    store.initialize()
    stale = datetime.now(UTC) - timedelta(hours=1)
    store.record_quote(
        Quote(
            BTC_USD,
            "kraken-spot",
            Decimal("12.34"),
            Decimal("12.35"),
            stale,
            stale,
            stale,
        )
    )
    app = create_app(store, quote_service=QuoteService(Outage(), BTC_USD))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        quote = client.get("/api/v1/market/quotes").json()
        assert quote["quality"] == "STALE"
        assert quote["provider_issue"] == "unavailable"
        assert quote["bid"] == "12.34"


def test_spot_orders_api_endpoints(tmp_path: Path) -> None:
    store = AuditStore(tmp_path / "app.sqlite")
    store.initialize()
    now = datetime.now(UTC)
    order = SpotOrder(
        client_order_id="cl-ord-api-1",
        order_id="ord-api-1",
        venue="kraken",
        market="BTC/USD",
        side=SpotSide.BUY,
        order_type=SpotOrderType.LIMIT,
        quantity=Decimal("0.02"),
        limit_price=Decimal("49500.00"),
        state=OrderState.ACKNOWLEDGED,
        risk_decision_id="risk-api-01",
        created_at=now,
        updated_at=now,
        venue_order_id="O-API-TEST",
    )
    store.save_spot_order(order)

    app = create_app(store)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        # List open orders
        resp = client.get("/api/v1/orders/spot?venue=kraken")
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 1
        assert items[0]["client_order_id"] == "cl-ord-api-1"
        assert items[0]["state"] == "ACKNOWLEDGED"

        # Get single order
        single = client.get("/api/v1/orders/spot/cl-ord-api-1")
        assert single.status_code == 200
        assert single.json()["venue_order_id"] == "O-API-TEST"

        # Not found
        assert client.get("/api/v1/orders/spot/non-existent").status_code == 404
