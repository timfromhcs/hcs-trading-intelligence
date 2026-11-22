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
from hcs_trader.risk import RiskEvaluation, RiskRejected
from hcs_trader.storage import (
    AuditStore,
    KillSwitchCause,
    RiskOutcome,
    SpotFill,
)


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
        assert connection.execute("PRAGMA user_version").fetchone() == (20,)


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
        # /orders/live is an alias of the gated spot path: disabled without credentials.
        live = client.post(
            "/api/v1/orders/live",
            json={
                "side": "buy",
                "quantity": "0.001",
                "limit_price": "50000",
                "candidate_id": "x",
                "strategy_version": "v1.0.0",
            },
        )
        assert live.status_code == 403
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

        shadow_runs_resp = client.get(
            f"/api/v1/strategies/candidates/{cand_id}/shadow-runs"
        )
        assert shadow_runs_resp.status_code == 200
        assert shadow_runs_resp.json() == []


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


# --- Persist-audit track: risk decisions, kill switch, audit, fills ---


def _evaluation(
    decision_id: str = "risk-aaa111", *, allowed: bool = True
) -> RiskEvaluation:
    return RiskEvaluation(
        decision_id=decision_id,
        allowed=allowed,
        reason=None if allowed else "Spread too wide",
        evaluated_at=datetime.now(UTC),
        plan_hash="ab" * 32,
    )


def _submitted_order(client_order_id: str = "cl-sub-1") -> SpotOrder:
    now = datetime.now(UTC)
    return SpotOrder(
        client_order_id=client_order_id,
        order_id="ord-" + client_order_id,
        venue="kraken",
        market="BTC/USD",
        side=SpotSide.BUY,
        order_type=SpotOrderType.LIMIT,
        quantity=Decimal("0.001"),
        limit_price=Decimal("50000"),
        state=OrderState.SUBMITTED,
        risk_decision_id="risk-aaa111",
        created_at=now,
        updated_at=now,
    )


def test_every_risk_verdict_is_persisted_with_typed_outcome(tmp_path: Path) -> None:
    path = tmp_path / "r.sqlite"
    store = AuditStore(path)
    store.initialize()
    allowed = store.record_risk_decision(
        _evaluation("risk-allow"), policy_version="v1", inputs={"spread_bps": "3"}
    )
    rejected = store.record_risk_decision(
        _evaluation("risk-reject", allowed=False), policy_version="v1"
    )
    no_trade = store.record_risk_decision(
        _evaluation("risk-notrade", allowed=False),
        policy_version="v1",
        no_trade=True,
    )
    assert (allowed.outcome, rejected.outcome, no_trade.outcome) == (
        RiskOutcome.ALLOWED,
        RiskOutcome.REJECTED,
        RiskOutcome.NO_TRADE,
    )
    reopened = AuditStore(path)
    reopened.initialize()
    stored = reopened.get_risk_decision("risk-reject")
    assert stored is not None
    assert stored.reason == "Spread too wide" and stored.policy_version == "v1"
    assert len(reopened.list_risk_decisions()) == 3
    kinds = [e.kind for e in reopened.list_structured_audit(10)]
    assert kinds.count("risk.decision.recorded") == 3


def test_risk_decision_recording_is_idempotent_but_not_overwritable(
    tmp_path: Path,
) -> None:
    store = AuditStore(tmp_path / "r.sqlite")
    store.initialize()
    evaluation = _evaluation()
    first = store.record_risk_decision(evaluation, policy_version="v1")
    assert store.record_risk_decision(evaluation, policy_version="v1") == first
    with pytest.raises(ValueError, match="already used"):
        store.record_risk_decision(_evaluation(allowed=False), policy_version="v1")
    with pytest.raises(ValueError, match="policy version"):
        store.record_risk_decision(evaluation, policy_version=" ")
    with pytest.raises(ValueError, match="NO_TRADE"):
        store.record_risk_decision(
            _evaluation("risk-x"), policy_version="v1", no_trade=True
        )
    assert store.approved_risk_decision("missing") is None


def test_submission_rejection_leaves_a_redacted_audit_record(tmp_path: Path) -> None:
    store = AuditStore(tmp_path / "r.sqlite")
    store.initialize()
    with pytest.raises(RiskRejected, match="canary"):
        store.reserve_spot_submission(
            _submitted_order(),
            2,
            candidate_id="cand-none",
            strategy_version="v1.0.0",
            key_fingerprint="fp",
        )
    event = store.list_structured_audit(1)[0]
    assert event.kind == "spot.submission.rejected"
    assert event.outcome == "NO_TRADE"
    assert event.order_or_tx_id == "cl-sub-1"
    assert event.strategy_version == "v1.0.0"
    assert event.args_hash is not None and event.redacted_args is not None
    assert store.get_spot_order("cl-sub-1") is None


def test_structured_audit_redacts_secrets_and_hashes_arguments(
    tmp_path: Path,
) -> None:
    store = AuditStore(tmp_path / "a.sqlite")
    store.initialize()
    event = store.record_structured_audit(
        "mcp.tool.call",
        arguments={
            "market": "BTC/USD",
            "api_key": "sk-live-123",
            "nested": {"Passphrase": "hunter2", "ok": [1, {"seed_phrase": "x y z"}]},
        },
        client_id="client-1",
        agent_id="agent-1",
        tool_name="orders.preview",
        capability_scope="READ",
        request_id="req-1",
        simulation_hash="sim-1",
        model_version="none",
        policy_version="v1",
        app_version="0.0.0",
        commit_sha="deadbeef",
    )
    assert event.redacted_args is not None
    for leaked in ("sk-live-123", "hunter2", "x y z"):
        assert leaked not in event.redacted_args
    assert event.redacted_args.count("[REDACTED]") == 3
    assert "BTC/USD" in event.redacted_args
    # The hash covers the redacted view, so secret values never influence it.
    other = store.record_structured_audit(
        "mcp.tool.call",
        arguments={
            "market": "BTC/USD",
            "api_key": "different",
            "nested": {"Passphrase": "other", "ok": [1, {"seed_phrase": "q"}]},
        },
    )
    assert other.args_hash == event.args_hash
    assert (event.client_id, event.agent_id, event.tool_name) == (
        "client-1",
        "agent-1",
        "orders.preview",
    )
    with pytest.raises(ValueError, match="Unknown audit fields"):
        store.record_structured_audit("x", bogus="1")
    # The legacy three-field view still works on the extended table.
    assert store.latest_event() is not None


def test_kill_switch_typed_trip_persists_and_requires_operator_clear(
    tmp_path: Path,
) -> None:
    path = tmp_path / "k.sqlite"
    store = AuditStore(path)
    store.initialize()
    state = store.trip_kill_switch(
        KillSwitchCause.UNEXPLAINED_BALANCE_DELTA, "BTC balance short by 0.1"
    )
    assert state.active and state.cause is KillSwitchCause.UNEXPLAINED_BALANCE_DELTA

    reopened = AuditStore(path)
    reopened.initialize()
    persisted = reopened.kill_switch_state()
    assert persisted.active
    assert persisted.cause is KillSwitchCause.UNEXPLAINED_BALANCE_DELTA
    assert persisted.detail == "BTC balance short by 0.1"
    assert reopened.fail_safe_paused() is True

    with pytest.raises(ValueError, match="operator"):
        reopened.clear_kill_switch("  ", "all good")
    with pytest.raises(ValueError, match="note"):
        reopened.clear_kill_switch("tim", "")
    assert reopened.kill_switch_state().active

    cleared = reopened.clear_kill_switch("tim", "reconciled manually")
    assert not cleared.active and cleared.cause is None
    history = reopened.kill_switch_history()
    assert [(a, c, o) for a, c, o, _ in history] == [
        ("CLEARED", "MANUAL", "tim"),
        ("TRIPPED", "UNEXPLAINED_BALANCE_DELTA", "system"),
    ]
    kinds = [e.kind for e in reopened.list_structured_audit(10)]
    assert "kill_switch.tripped" in kinds and "kill_switch.cleared" in kinds


def test_repeated_trips_record_each_cause_and_manual_pause_still_works(
    tmp_path: Path,
) -> None:
    store = AuditStore(tmp_path / "k.sqlite")
    store.initialize()
    store.trip_kill_switch(KillSwitchCause.STALE_FEED, "no quotes for 90s")
    state = store.trip_kill_switch(KillSwitchCause.REPEATED_REJECTS, "3 rejects")
    assert state.cause is KillSwitchCause.REPEATED_REJECTS
    assert len(store.kill_switch_history()) == 2
    with pytest.raises(ValueError, match="detail"):
        store.trip_kill_switch(KillSwitchCause.MANUAL, " ")
    store.set_execution_pause(False)
    assert store.execution_pause().paused is False
    store.set_execution_pause(True)
    assert store.kill_switch_state().cause is KillSwitchCause.MANUAL


def test_spot_fills_are_recorded_once_and_conflicts_are_rejected(
    tmp_path: Path,
) -> None:
    store = AuditStore(tmp_path / "f.sqlite")
    store.initialize()
    order = _submitted_order()
    store.save_spot_order(order)
    fill = SpotFill(
        order.client_order_id,
        "T1",
        Decimal("0.0004"),
        Decimal("50000"),
        Decimal("0.08"),
        datetime.now(UTC),
    )
    assert store.record_spot_fill(fill) is True
    assert store.record_spot_fill(fill) is False
    with pytest.raises(ValueError, match="different values"):
        store.record_spot_fill(replace(fill, price=Decimal("50001")))
    store.record_spot_fill(replace(fill, fill_id="T2", quantity=Decimal("0.0006")))
    fills = store.spot_fills(order.client_order_id)
    assert [f.fill_id for f in fills] == ["T1", "T2"]
    assert sum(f.quantity for f in fills) == Decimal("0.0010")
    with pytest.raises(ValueError, match="finite and positive"):
        store.record_spot_fill(replace(fill, fill_id="T3", quantity=Decimal("0")))
    with pytest.raises(ValueError, match="finite and positive"):
        store.record_spot_fill(replace(fill, fill_id="T4", fee=Decimal("NaN")))
    with pytest.raises(sqlite3.IntegrityError):
        store.record_spot_fill(replace(fill, client_order_id="missing", fill_id="T5"))


def test_health_reports_ok_and_failures_without_raising(tmp_path: Path) -> None:
    store = AuditStore(tmp_path / "h.sqlite")
    store.initialize()
    health = store.health()
    assert health.ok and health.schema_version == 20
    assert health.execution_paused is False
    bad = AuditStore(tmp_path / "nowhere" / "missing.sqlite", read_only=True)
    report = bad.health()
    assert report.ok is False and "unavailable" in report.detail
