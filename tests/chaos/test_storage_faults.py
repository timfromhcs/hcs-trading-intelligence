"""Chaos tests: restart between lifecycle steps, locked, read-only and corrupt DBs."""

import os
import sqlite3
import stat
import subprocess  # noqa: S404
import sys
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from hcs_trader.domain.execution import SpotOrder, SpotOrderType, SpotSide
from hcs_trader.domain.lifecycle import OrderState
from hcs_trader.risk import RiskEvaluation, RiskRejected
from hcs_trader.storage import (
    AuditStore,
    KillSwitchCause,
    PersistenceUnavailable,
)


def _order(state: OrderState = OrderState.SUBMITTED) -> SpotOrder:
    now = datetime.now(UTC)
    return SpotOrder(
        client_order_id="cl-chaos-1",
        order_id="ord-chaos-1",
        venue="kraken",
        market="BTC/USD",
        side=SpotSide.BUY,
        order_type=SpotOrderType.LIMIT,
        quantity=Decimal("0.001"),
        limit_price=Decimal("50000"),
        state=state,
        risk_decision_id="risk-chaos",
        created_at=now,
        updated_at=now,
    )


def _fresh(path: Path) -> AuditStore:
    store = AuditStore(path)
    store.initialize()
    return store


def test_order_lifecycle_survives_a_restart_between_every_transition(
    tmp_path: Path,
) -> None:
    path = tmp_path / "chaos.sqlite"
    order = _order()
    _fresh(path).save_spot_order(order)
    store = _fresh(path)  # "process killed" after SUBMITTED persisted
    assert store.list_open_spot_orders("kraken") == [order]
    current = order
    for step, target in enumerate(
        (
            OrderState.ACKNOWLEDGED,
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
        ),
        start=1,
    ):
        store = _fresh(path)  # restart before each transition
        reloaded = store.get_spot_order(current.client_order_id)
        assert reloaded == current
        nxt = replace(
            current,
            state=target,
            updated_at=current.updated_at + timedelta(seconds=step),
            venue_order_id="V-1",
            filled_quantity=Decimal("0.0005") if step > 1 else None,
            average_price=Decimal("50000") if step > 1 else None,
            fee=Decimal("0.05") if step > 1 else None,
        )
        assert store.apply_spot_observation(current, nxt) is True
        current = nxt
    final = _fresh(path).get_spot_order(order.client_order_id)
    assert final is not None and final.state is OrderState.FILLED
    # A stale replay after the restart cannot regress the order.
    assert (
        _fresh(path).apply_spot_observation(
            order,
            replace(
                order,
                state=OrderState.ACKNOWLEDGED,
                updated_at=order.updated_at + timedelta(seconds=1),
            ),
        )
        is False
    )


def test_crash_inside_a_transaction_rolls_back_every_write(tmp_path: Path) -> None:
    path = tmp_path / "chaos.sqlite"
    store = _fresh(path)
    with pytest.raises(RuntimeError, match="crash"):  # noqa: PT012
        with store._connect() as connection, connection:  # noqa: SLF001
            connection.execute("BEGIN IMMEDIATE")
            store._apply_pause(  # noqa: SLF001
                connection,
                True,
                cause=KillSwitchCause.MANUAL,
                detail="x",
                actor="a",
                kind="execution.paused",
            )
            raise RuntimeError("crash mid-transaction")
    reopened = _fresh(path)
    assert reopened.execution_pause().paused is False
    assert reopened.kill_switch_history() == []


def test_kill_switch_trip_survives_restart_and_blocks_reservation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "chaos.sqlite"
    child = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-c",
            "import os, sys; from pathlib import Path; "
            "from hcs_trader.storage import AuditStore, KillSwitchCause; "
            "store = AuditStore(Path(sys.argv[1])); store.initialize(); "
            "store.trip_kill_switch(KillSwitchCause.VENUE_UNHEALTHY, 'venue down'); "
            "os._exit(73)",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert child.returncode == 73, child.stderr
    restarted = _fresh(path)
    assert restarted.kill_switch_state().active
    with pytest.raises(RiskRejected, match="paused"):
        restarted.reserve_spot_submission(
            _order(), 2, candidate_id="c", strategy_version="v1", key_fingerprint="k"
        )
    assert restarted.get_spot_order("cl-chaos-1") is None


def test_locked_database_fails_closed_as_persistence_unavailable(
    tmp_path: Path,
) -> None:
    path = tmp_path / "chaos.sqlite"
    store = AuditStore(path, busy_timeout=0.05)
    store.initialize()
    blocker = sqlite3.connect(path, timeout=0)
    try:
        blocker.execute("BEGIN IMMEDIATE")  # holds the write lock
        with pytest.raises(PersistenceUnavailable):
            store.set_execution_pause(True)
        with pytest.raises(PersistenceUnavailable):
            store.record_risk_decision(
                RiskEvaluation("risk-1", True, None, datetime.now(UTC), "ab" * 32),
                policy_version="v1",
            )
        # Reads still work under WAL, but a failed reservation is a NO_TRADE.
        with pytest.raises(RiskRejected, match="Persistence unavailable"):
            store.reserve_spot_submission(
                _order(),
                2,
                candidate_id="c",
                strategy_version="v1",
                key_fingerprint="k",
            )
    finally:
        blocker.rollback()
        blocker.close()
    assert store.execution_pause().paused is False  # nothing half-applied
    assert store.get_spot_order("cl-chaos-1") is None


def test_unreadable_pause_state_is_reported_as_paused(tmp_path: Path) -> None:
    store = _fresh(tmp_path / "chaos.sqlite")
    with closing(sqlite3.connect(store.path)) as connection, connection:
        connection.execute("DROP TABLE execution_pause")
    # Fail-safe read: unreadable means paused; the trip itself cannot be written.
    assert store.fail_safe_paused() is True
    assert store.health().ok is False


@pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX permission bits are not enforced"
)
def test_read_only_file_blocks_writes_fail_safe(tmp_path: Path) -> None:
    path = tmp_path / "chaos.sqlite"
    _fresh(path)
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(path) + suffix)
        if candidate.exists():
            candidate.chmod(stat.S_IRUSR)
    try:
        if os.access(path, os.W_OK):  # running as root
            pytest.skip("file remains writable for this user")
        store = AuditStore(path)
        with pytest.raises(PersistenceUnavailable):
            store.record_audit_event("x")
    finally:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def test_read_only_connection_rejects_writes_and_never_trips(tmp_path: Path) -> None:
    path = tmp_path / "chaos.sqlite"
    _fresh(path)
    readonly = AuditStore(path, read_only=True)
    with pytest.raises(PersistenceUnavailable):
        readonly.set_execution_pause(True)
    assert readonly.fail_safe_paused() is False  # readable and not paused
    readonly._best_effort_trip("ignored")  # noqa: SLF001
    assert _fresh(path).execution_pause().paused is False


def test_garbage_file_is_refused_at_startup(tmp_path: Path) -> None:
    path = tmp_path / "chaos.sqlite"
    path.write_bytes(b"this is not a sqlite database " * 200)
    store = AuditStore(path)
    with pytest.raises(PersistenceUnavailable):
        store.initialize()
    assert store.health().ok is False
    assert store.fail_safe_paused() is True


def test_truncated_database_is_refused_and_pause_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "chaos.sqlite"
    store = _fresh(path)
    for index in range(200):
        store.record_audit_event(f"filler.{index}" + "x" * 200)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    size = path.stat().st_size
    assert size > 8192
    with path.open("r+b") as handle:
        handle.truncate(size // 2)
    reopened = AuditStore(path)
    with pytest.raises(PersistenceUnavailable):
        reopened.initialize()
    assert reopened.health().ok is False
    assert reopened.fail_safe_paused() is True


def test_page_corruption_is_detected_by_integrity_check(tmp_path: Path) -> None:
    path = tmp_path / "chaos.sqlite"
    store = _fresh(path)
    for index in range(100):
        store.record_audit_event(f"filler.{index}" + "y" * 200)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    data = bytearray(path.read_bytes())
    for offset in range(4096 * 2, len(data) - 4096, 4096):
        data[offset : offset + 64] = b"\xff" * 64  # smash page headers
    path.write_bytes(bytes(data))
    with pytest.raises(PersistenceUnavailable):
        AuditStore(path).initialize()


def test_reserve_on_corrupt_store_is_a_no_trade(tmp_path: Path) -> None:
    path = tmp_path / "chaos.sqlite"
    _fresh(path)
    path.write_bytes(b"\x00" * 4096)
    store = AuditStore(path)
    with pytest.raises(RiskRejected, match="Persistence unavailable"):
        store.reserve_spot_submission(
            _order(), 2, candidate_id="c", strategy_version="v1", key_fingerprint="k"
        )


def test_constraint_violations_are_not_mistaken_for_storage_faults(
    tmp_path: Path,
) -> None:
    store = _fresh(tmp_path / "chaos.sqlite")
    with pytest.raises(sqlite3.IntegrityError):
        with store._connect() as connection, connection:  # noqa: SLF001, PT012
            connection.execute(
                "INSERT INTO kill_switch_events "
                "(action, cause, detail, actor, occurred_at) "
                "VALUES ('BOGUS', 'x', 'x', 'x', 'x')"
            )
