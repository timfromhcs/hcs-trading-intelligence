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
from hcs_trader.promotion import PromotionStage, StrategyCandidate
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
        assert connection.execute("PRAGMA user_version").fetchone() == (17,)


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


def test_risk_pause_resume_endpoints(tmp_path: Path) -> None:
    store = AuditStore(tmp_path / "app.sqlite")
    store.initialize()
    app = create_app(store)

    with TestClient(app, base_url="http://127.0.0.1") as client:
        # Initial status is not paused
        status_resp = client.get("/api/v1/risk/status")
        assert status_resp.status_code == 200
        assert status_resp.json()["paused"] is False

        # Cross-site origin rejected
        cross_resp = client.post(
            "/api/v1/risk/pause",
            headers={"sec-fetch-site": "cross-site", "origin": "http://evil.com"},
        )
        assert cross_resp.status_code == 403

        # Trigger pause
        pause_resp = client.post("/api/v1/risk/pause")
        assert pause_resp.status_code == 200
        assert pause_resp.json()["paused"] is True
        assert pause_resp.json()["changed_at"] is not None

        # Confirm status updated
        assert client.get("/api/v1/risk/status").json()["paused"] is True

        # Resume execution
        resume_resp = client.post("/api/v1/risk/resume")
        assert resume_resp.status_code == 200
        assert resume_resp.json()["paused"] is False

        # Confirm status resumed
        assert client.get("/api/v1/risk/status").json()["paused"] is False


def test_audit_events_endpoint(tmp_path: Path) -> None:
    store = AuditStore(tmp_path / "app.sqlite")
    store.initialize()
    store.record_startup()
    store.set_execution_pause(True)

    app = create_app(store)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        resp = client.get("/api/v1/audit/events?limit=10")
        assert resp.status_code == 200
        events = resp.json()
        assert len(events) >= 2
        kinds = [e["kind"] for e in events]
        assert "execution.paused" in kinds
        assert "app.started" in kinds


def test_skills_endpoint(tmp_path: Path) -> None:
    store = AuditStore(tmp_path / "app.sqlite")
    store.initialize()
    app = create_app(store)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        resp = client.get("/api/v1/skills")
        assert resp.status_code == 200
        skills = resp.json()
        assert len(skills) == 5
        names = {s["name"] for s in skills}
        assert "market-research" in names
        assert "strategy-lab" in names
        assert "portfolio-operator" in names
        assert "risk-auditor" in names
        assert "release-engineer" in names


def test_strategy_candidates_storage_and_api(tmp_path: Path) -> None:
    store = AuditStore(tmp_path / "app.sqlite")
    store.initialize()
    app = create_app(store)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        create_resp = client.post(
            "/api/v1/strategies/candidates",
            json={
                "name": "RSI Trend Candidate",
                "version": "1.0.0",
                "parameters": {"fast": 14, "slow": 28},
                "dataset_hash": "sha-cand-01",
            },
        )
        assert create_resp.status_code == 200
        cand_data = create_resp.json()
        cand_id = cand_data["candidate_id"]
        assert cand_data["stage"] == "RESEARCH"
        before = store.get_strategy_candidate(cand_id)
        assert before is not None

        list_resp = client.get("/api/v1/strategies/candidates")
        assert list_resp.status_code == 200
        assert len(list_resp.json()) == 1

        promote_resp = client.post(
            f"/api/v1/strategies/candidates/{cand_id}/promote",
            json={"target_stage": "UNIT_TESTED"},
        )
        assert promote_resp.status_code == 200
        res = promote_resp.json()
        assert res["candidate"]["stage"] == "UNIT_TESTED"
        assert res["gate_result"]["passed"] is True
        assert (
            store.update_candidate_stage(
                before, PromotionStage.UNIT_TESTED, datetime.now(UTC)
            )
            is None
        )

        audit_events = store.list_audit_events(limit=5)
        kinds = [e.kind for e in audit_events]
        assert "strategy.promoted.unit_tested" in kinds
        assert kinds.count("strategy.promoted.unit_tested") == 1


@pytest.mark.parametrize(
    ("stage", "target"),
    [
        (PromotionStage.PAPER_SHADOW, PromotionStage.PAPER_APPROVED),
        (PromotionStage.PAPER_APPROVED, PromotionStage.LIVE_CANARY),
        (PromotionStage.LIVE_CANARY, PromotionStage.LIVE_BOUNDED),
    ],
)
def test_candidate_promotion_cannot_use_client_assertions(
    tmp_path: Path, stage: PromotionStage, target: PromotionStage
) -> None:
    store = AuditStore(tmp_path / "app.sqlite")
    store.initialize()
    candidate = StrategyCandidate(
        candidate_id="candidate",
        name="SMA",
        version="1",
        stage=stage,
        dataset_hash="dataset",
    )
    store.save_strategy_candidate(candidate)
    with TestClient(create_app(store), base_url="http://127.0.0.1") as client:
        response = client.post(
            "/api/v1/strategies/candidates/candidate/promote",
            json={"target_stage": target, "shadow_fills": 5, "is_live_enabled": True},
        )
    assert response.status_code == 409
    assert store.get_strategy_candidate(candidate.candidate_id) == candidate
