"""Upgrade and downgrade-refusal tests for the local audit database."""

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from hcs_trader import storage
from hcs_trader.domain.execution import SpotOrder, SpotSide
from hcs_trader.domain.execution import SpotOrderType as OrderType
from hcs_trader.domain.lifecycle import OrderState
from hcs_trader.promotion import PromotionStage, StrategyCandidate
from hcs_trader.storage import AuditStore

_LEGACY_SPOT_ORDERS = """
CREATE TABLE spot_orders (
    client_order_id TEXT PRIMARY KEY NOT NULL,
    order_id TEXT NOT NULL,
    venue TEXT NOT NULL,
    market TEXT NOT NULL,
    side TEXT NOT NULL,
    order_type TEXT NOT NULL,
    quantity TEXT NOT NULL,
    limit_price TEXT,
    state TEXT NOT NULL,
    risk_decision_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    venue_order_id TEXT,
    filled_quantity TEXT,
    average_price TEXT,
    fee TEXT
) STRICT, WITHOUT ROWID
"""
_LEGACY_COLUMNS = (
    "client_order_id, order_id, venue, market, side, order_type, quantity, "
    "limit_price, state, risk_decision_id, created_at, updated_at, "
    "venue_order_id, filled_quantity, average_price, fee"
)


def _seed(store: AuditStore) -> None:
    now = datetime.now(UTC)
    store.initialize()
    store.set_execution_pause(True)
    store.record_audit_event("fixture.event")
    store.save_strategy_candidate(
        StrategyCandidate(
            candidate_id="cand-1",
            name="SMA-Trend",
            version="1.0.0",
            stage=PromotionStage.RESEARCH,
            parameters={"fast": 10},
            dataset_hash="hash",
            created_at=now,
            updated_at=now,
        )
    )
    store.save_spot_order(
        SpotOrder(
            client_order_id="cl-1",
            order_id="ord-1",
            venue="kraken",
            market="BTC/USD",
            side=SpotSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=Decimal("0.05"),
            limit_price=Decimal("50000"),
            state=OrderState.ACKNOWLEDGED,
            risk_decision_id="risk-1",
            created_at=now,
            updated_at=now,
            venue_order_id="V-1",
        )
    )


def _downgrade(path: Path, version: int) -> None:
    """Rewrite a current database into the shape it had at an older version."""
    assert version in {14, 16}
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DROP TABLE spot_canary_approval")
        connection.execute("ALTER TABLE spot_orders RENAME TO spot_orders_new")
        connection.execute(_LEGACY_SPOT_ORDERS)
        connection.execute(
            f"INSERT INTO spot_orders ({_LEGACY_COLUMNS}) "  # noqa: S608
            f"SELECT {_LEGACY_COLUMNS} FROM spot_orders_new"
        )
        connection.execute("DROP TABLE spot_orders_new")
        connection.execute(
            "CREATE INDEX spot_orders_state ON spot_orders (venue, market, state)"
        )
        if version == 14:
            connection.execute("DROP TABLE research_runs")
            connection.execute("DROP TABLE research_schedule")
        connection.execute(f"PRAGMA user_version = {version}")


def _user_version(path: Path) -> int:
    with closing(sqlite3.connect(path)) as connection:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])


@pytest.mark.parametrize("legacy_version", [14, 16])
def test_upgrade_preserves_state_and_stays_safe(
    tmp_path: Path, legacy_version: int
) -> None:
    path = tmp_path / "Données dir" / "audit.sqlite"
    store = AuditStore(path)
    _seed(store)
    _downgrade(path, legacy_version)
    assert _user_version(path) == legacy_version

    upgraded = AuditStore(path)
    upgraded.initialize()

    assert _user_version(path) == storage._SCHEMA_VERSION  # noqa: SLF001
    # The kill switch survives the upgrade and is not silently cleared.
    assert upgraded.execution_pause().paused is True
    kinds = {event.kind for event in upgraded.list_audit_events(limit=100)}
    assert {"fixture.event", "execution.paused"} <= kinds
    order = upgraded.get_spot_order("cl-1")
    assert order is not None
    assert order.quantity == Decimal("0.05")
    assert order.state is OrderState.ACKNOWLEDGED
    candidate = upgraded.get_strategy_candidate("cand-1")
    assert candidate is not None
    assert candidate.stage is PromotionStage.RESEARCH
    # New tables exist and are empty: no canary authorisation appears by upgrade.
    assert upgraded.get_spot_canary_approval("cand-1", datetime.now(UTC)) is None
    assert upgraded.research_schedule() is not None
    # A second initialisation is a no-op.
    upgraded.initialize()
    assert upgraded.execution_pause().paused is True


def test_upgrade_leaves_paper_mode_and_live_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    from hcs_trader.api import create_app

    path = tmp_path / "audit.sqlite"
    store = AuditStore(path)
    _seed(store)
    _downgrade(path, 16)
    AuditStore(path).initialize()
    with TestClient(
        create_app(AuditStore(path)), base_url="http://127.0.0.1"
    ) as client:
        body = client.get("/api/v1/system/status").json()
    assert body["mode"] == "PAPER"
    assert body["execution_available"] is False
    assert body["network_enabled"] is False


def test_newer_schema_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "audit.sqlite"
    AuditStore(path).initialize()
    newer = storage._SCHEMA_VERSION + 1  # noqa: SLF001
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(f"PRAGMA user_version = {newer}")
    with pytest.raises(RuntimeError, match="Unsupported"):
        AuditStore(path).initialize()
    assert _user_version(path) == newer


def test_fresh_database_starts_unpaused_at_current_version(tmp_path: Path) -> None:
    path = tmp_path / "audit.sqlite"
    store = AuditStore(path)
    store.initialize()
    assert _user_version(path) == storage._SCHEMA_VERSION  # noqa: SLF001
    assert store.execution_pause().paused is False
