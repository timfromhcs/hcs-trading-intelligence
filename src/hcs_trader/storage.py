"""Versioned local SQLite storage for audit events and quote history."""

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import TypeAdapter

from hcs_trader.domain.assets import MarketId
from hcs_trader.domain.execution import (
    SpotCanaryApproval,
    SpotCashTrade,
    SpotOrder,
    SpotOrderType,
    SpotSide,
)
from hcs_trader.domain.execution import SpotFill as SpotFill
from hcs_trader.domain.lifecycle import OrderState
from hcs_trader.domain.macro import MacroObservation
from hcs_trader.domain.market_data import Candle, Quote
from hcs_trader.domain.news import NewsItem
from hcs_trader.domain.paper import (
    PaperBalance,
    PaperConflict,
    PaperFill,
    PaperOrder,
    PaperRejected,
    book_fill,
)
from hcs_trader.domain.signing import ApprovedRiskDecision, SignedPlanConflict
from hcs_trader.domain.wallet import WalletSnapshot
from hcs_trader.local_research import ResearchHistory, ResearchRun, ResearchRunStats
from hcs_trader.promotion import PromotionStage, StrategyCandidate
from hcs_trader.risk import RiskEvaluation, RiskRejected

_SCHEMA_VERSION = 19
_SPOT_CASH_TRADE = TypeAdapter(SpotCashTrade)
_PAPER_FILL = TypeAdapter(PaperFill)
_RESEARCH_RUN = TypeAdapter(ResearchRun)
_RESEARCH_STATS = TypeAdapter(ResearchRunStats)
WALLET_SNAPSHOT = TypeAdapter(WalletSnapshot)


class PersistenceUnavailable(sqlite3.OperationalError):
    """The local database cannot be trusted or used (locked, corrupt, read-only).

    Execution must treat this as a hard NO_TRADE condition.
    """


class KillSwitchCause(StrEnum):
    """Typed reasons for tripping the kill switch (AGENTS.md 7.2)."""

    MANUAL = "MANUAL"
    STALE_FEED = "STALE_FEED"
    RECONCILIATION_MISMATCH = "RECONCILIATION_MISMATCH"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    VENUE_UNHEALTHY = "VENUE_UNHEALTHY"
    UNEXPECTED_CHAIN = "UNEXPECTED_CHAIN"
    REPEATED_REJECTS = "REPEATED_REJECTS"
    UNEXPLAINED_BALANCE_DELTA = "UNEXPLAINED_BALANCE_DELTA"
    LOSS_LIMIT = "LOSS_LIMIT"
    SLIPPAGE_OR_FEE_BREACH = "SLIPPAGE_OR_FEE_BREACH"
    SUSPICIOUS_INTERACTION = "SUSPICIOUS_INTERACTION"
    SIGNING_POLICY_FAILURE = "SIGNING_POLICY_FAILURE"
    INJECTION_ATTEMPT = "INJECTION_ATTEMPT"
    PERSISTENCE_FAILURE = "PERSISTENCE_FAILURE"


class RiskOutcome(StrEnum):
    ALLOWED = "ALLOWED"
    REJECTED = "REJECTED"
    NO_TRADE = "NO_TRADE"


@dataclass(frozen=True, slots=True)
class AuditEvent:
    event_id: str
    recorded_at: str
    kind: str


@dataclass(frozen=True, slots=True)
class StructuredAuditEvent:
    """Spec 12.3 audit record; secrets are redacted before storage."""

    event_id: str
    recorded_at: str
    kind: str
    client_id: str | None = None
    agent_id: str | None = None
    tool_name: str | None = None
    capability_scope: str | None = None
    request_id: str | None = None
    idempotency_key: str | None = None
    args_hash: str | None = None
    redacted_args: str | None = None
    strategy_version: str | None = None
    model_version: str | None = None
    policy_version: str | None = None
    risk_decision_id: str | None = None
    simulation_hash: str | None = None
    order_or_tx_id: str | None = None
    outcome: str | None = None
    app_version: str | None = None
    commit_sha: str | None = None


_AUDIT_FIELDS: tuple[str, ...] = (
    "client_id",
    "agent_id",
    "tool_name",
    "capability_scope",
    "request_id",
    "idempotency_key",
    "args_hash",
    "redacted_args",
    "strategy_version",
    "model_version",
    "policy_version",
    "risk_decision_id",
    "simulation_hash",
    "order_or_tx_id",
    "outcome",
    "app_version",
    "commit_sha",
)
_SECRET_KEY = re.compile(
    r"secret|passw|passphrase|private|seed|mnemonic|token|api[_-]?key|"
    r"authorization|credential|signature|keystore|cookie",
    re.IGNORECASE,
)
REDACTED = "[REDACTED]"


def redact_arguments(value: Any) -> Any:
    """Return a JSON-safe copy of ``value`` with secret-looking fields masked."""
    if isinstance(value, Mapping):
        return {
            str(key): REDACTED
            if _SECRET_KEY.search(str(key))
            else redact_arguments(item)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple | set | frozenset):
        return [redact_arguments(item) for item in value]
    if value is None or isinstance(value, bool | int | float | str):
        return value
    return str(value)


def canonical_hash(value: Any) -> str:
    """SHA-256 over canonical JSON of the (already redacted) value."""
    return hashlib.sha256(
        json.dumps(
            redact_arguments(value), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class StoredRiskDecision:
    decision_id: str
    outcome: RiskOutcome
    allowed: bool
    reason: str | None
    policy_version: str
    inputs_hash: str
    plan_hash: str | None
    evaluated_at: datetime
    consumed_at: datetime | None


@dataclass(frozen=True, slots=True)
class KillSwitchState:
    active: bool
    cause: KillSwitchCause | None
    detail: str | None
    changed_at: datetime | None


@dataclass(frozen=True, slots=True)
class StoreHealth:
    ok: bool
    detail: str
    schema_version: int | None = None
    execution_paused: bool | None = None


@dataclass(frozen=True, slots=True)
class ResearchRecord:
    recorded_at: datetime
    payload: str


@dataclass(frozen=True, slots=True)
class ExecutionPause:
    paused: bool
    changed_at: datetime | None


class AuditStore:
    def __init__(
        self, path: Path, *, read_only: bool = False, busy_timeout: float = 5.0
    ) -> None:
        self.path = path
        self._read_only = read_only
        self._busy_timeout = busy_timeout

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        location = (
            self.path.resolve().as_uri() + "?mode=ro" if self._read_only else self.path
        )
        try:
            with closing(
                sqlite3.connect(
                    location, timeout=self._busy_timeout, uri=self._read_only
                )
            ) as connection:
                connection.execute("PRAGMA foreign_keys = ON")
                yield connection
        except PersistenceUnavailable:
            raise
        except sqlite3.DatabaseError as error:
            # Locked, read-only, full, corrupt or truncated storage fails closed.
            # Constraint violations stay ordinary errors for callers to handle.
            if isinstance(error, sqlite3.OperationalError) or (
                type(error) is sqlite3.DatabaseError
            ):
                raise PersistenceUnavailable(
                    f"Audit storage unavailable: {error}"
                ) from error
            raise

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection, connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("BEGIN IMMEDIATE")
            (version,) = connection.execute("PRAGMA user_version").fetchone()
            if not 0 <= version <= _SCHEMA_VERSION:
                raise RuntimeError("Unsupported audit database schema")
            if version < 1:
                connection.execute(
                    """
                    CREATE TABLE audit_events (
                        event_id TEXT PRIMARY KEY NOT NULL,
                        recorded_at TEXT NOT NULL,
                        kind TEXT NOT NULL
                    ) STRICT
                    """
                )
            if version < 2:
                connection.execute(
                    """
                    CREATE TABLE market_quotes (
                        id INTEGER PRIMARY KEY,
                        venue TEXT NOT NULL,
                        base_asset TEXT NOT NULL,
                        quote_asset TEXT NOT NULL,
                        source TEXT NOT NULL,
                        bid TEXT NOT NULL,
                        ask TEXT NOT NULL,
                        bid_updated_at TEXT NOT NULL,
                        ask_updated_at TEXT NOT NULL,
                        received_at TEXT NOT NULL,
                        UNIQUE (venue, base_asset, quote_asset, source,
                                bid_updated_at, ask_updated_at, bid, ask)
                    ) STRICT
                    """
                )
                connection.execute(
                    "CREATE INDEX market_quotes_history "
                    "ON market_quotes (venue, base_asset, quote_asset, id DESC)"
                )
            if version < 3:
                connection.execute(
                    """
                    CREATE TABLE market_candles (
                        venue TEXT NOT NULL,
                        base_asset TEXT NOT NULL,
                        quote_asset TEXT NOT NULL,
                        source TEXT NOT NULL,
                        interval_minutes INTEGER NOT NULL CHECK (interval_minutes > 0),
                        opened_at TEXT NOT NULL,
                        received_at TEXT NOT NULL,
                        opening TEXT NOT NULL,
                        high TEXT NOT NULL,
                        low TEXT NOT NULL,
                        closing TEXT NOT NULL,
                        volume TEXT NOT NULL,
                        trade_count INTEGER NOT NULL CHECK (trade_count >= 0),
                        PRIMARY KEY (venue, base_asset, quote_asset, source,
                                     interval_minutes, opened_at)
                    ) STRICT, WITHOUT ROWID
                    """
                )
            if version < 4:
                connection.execute(
                    """
                    CREATE TABLE news_items (
                        source TEXT NOT NULL,
                        url TEXT NOT NULL,
                        title TEXT NOT NULL,
                        published_at TEXT NOT NULL,
                        received_at TEXT NOT NULL,
                        PRIMARY KEY (source, url)
                    ) STRICT, WITHOUT ROWID
                    """
                )
                connection.execute(
                    "CREATE INDEX news_items_timeline ON news_items (published_at DESC)"
                )
            if version < 5:
                connection.execute(
                    """
                    CREATE TABLE macro_observations (
                        id INTEGER PRIMARY KEY,
                        source TEXT NOT NULL,
                        series TEXT NOT NULL,
                        observation_date TEXT NOT NULL,
                        received_at TEXT NOT NULL,
                        value TEXT NOT NULL
                    ) STRICT
                    """
                )
                connection.execute(
                    "CREATE INDEX macro_series_history ON macro_observations "
                    "(source, series, observation_date DESC, id DESC)"
                )
            if version < 6:
                connection.execute(
                    "ALTER TABLE macro_observations ADD COLUMN vintage_date TEXT"
                )
            if version < 7:
                connection.execute(
                    """
                    CREATE TABLE backtest_reports (
                        id TEXT PRIMARY KEY NOT NULL,
                        recorded_at TEXT NOT NULL,
                        payload TEXT NOT NULL
                    ) STRICT, WITHOUT ROWID
                    """
                )
            if version < 8:
                connection.execute(
                    """
                    CREATE TABLE research_holdouts (
                        commitment TEXT PRIMARY KEY NOT NULL,
                        candidate TEXT NOT NULL,
                        result TEXT
                    ) STRICT, WITHOUT ROWID
                    """
                )
            if version < 9:
                connection.execute(
                    """
                    CREATE TABLE paper_fills (
                        id INTEGER PRIMARY KEY,
                        order_key TEXT NOT NULL UNIQUE,
                        payload TEXT NOT NULL
                    ) STRICT
                    """
                )
            if version < 10:
                connection.execute(
                    """
                    CREATE TABLE execution_pause (
                        id INTEGER PRIMARY KEY CHECK (id = 1),
                        paused INTEGER NOT NULL CHECK (paused IN (0, 1)),
                        changed_at TEXT
                    ) STRICT
                    """
                )
                connection.execute("INSERT INTO execution_pause VALUES (1, 0, NULL)")
            if version < 11:
                connection.execute(
                    """
                    CREATE TABLE wallet_snapshots (
                        asset TEXT NOT NULL,
                        address TEXT NOT NULL,
                        observed_at TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        PRIMARY KEY (asset, address)
                    ) STRICT, WITHOUT ROWID
                    """
                )
                connection.execute("PRAGMA user_version = 11")
            if version < 12:
                connection.execute(
                    """
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
                )
                connection.execute(
                    "CREATE INDEX spot_orders_state "
                    "ON spot_orders (venue, market, state)"
                )
                connection.execute("PRAGMA user_version = 12")
            if version < 13:
                connection.execute(
                    """
                    CREATE TABLE strategy_candidates (
                        candidate_id TEXT PRIMARY KEY NOT NULL,
                        name TEXT NOT NULL,
                        version TEXT NOT NULL,
                        stage TEXT NOT NULL,
                        parameters TEXT NOT NULL,
                        dataset_hash TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    ) STRICT, WITHOUT ROWID
                    """
                )
                connection.execute(
                    "CREATE INDEX strategy_candidates_stage "
                    "ON strategy_candidates (stage, updated_at DESC)"
                )
                connection.execute("PRAGMA user_version = 13")
            if version < 14:
                connection.execute(
                    """
                    CREATE TABLE strategy_shadow_runs (
                        candidate_id TEXT NOT NULL REFERENCES strategy_candidates,
                        run_id TEXT NOT NULL,
                        evaluated_until TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        PRIMARY KEY (candidate_id, run_id)
                    ) STRICT, WITHOUT ROWID
                    """
                )
                connection.execute("PRAGMA user_version = 14")
            if version < 15:
                connection.execute(
                    """
                    CREATE TABLE research_runs (
                        run_id TEXT PRIMARY KEY NOT NULL,
                        started_at TEXT NOT NULL,
                        duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
                        status TEXT NOT NULL,
                        payload TEXT NOT NULL
                    ) STRICT, WITHOUT ROWID
                    """
                )
                connection.execute(
                    "CREATE INDEX research_runs_recent "
                    "ON research_runs(started_at DESC, run_id DESC)"
                )
                connection.execute("PRAGMA user_version = 15")
            if version < 16:
                connection.execute(
                    """
                    CREATE TABLE research_schedule (
                        id INTEGER PRIMARY KEY CHECK (id = 1),
                        next_run_at TEXT NOT NULL,
                        consecutive_failures INTEGER NOT NULL
                    ) STRICT
                    """
                )
                connection.execute("PRAGMA user_version = 16")
            if version < 17:
                connection.execute(
                    """
                    CREATE TABLE spot_canary_approval (
                        id INTEGER PRIMARY KEY CHECK (id = 1),
                        candidate_id TEXT NOT NULL REFERENCES strategy_candidates,
                        candidate_version TEXT NOT NULL,
                        dataset_hash TEXT NOT NULL,
                        key_fingerprint TEXT NOT NULL,
                        max_notional TEXT NOT NULL,
                        expires_at TEXT NOT NULL
                    ) STRICT
                    """
                )
                connection.execute("PRAGMA user_version = 17")
                connection.execute(
                    "ALTER TABLE spot_orders ADD COLUMN canary_candidate_id TEXT "
                    "REFERENCES strategy_candidates(candidate_id)"
                )
            if version < 18:
                present = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(audit_events)")
                }
                for column in (c for c in _AUDIT_FIELDS if c not in present):
                    connection.execute(
                        f"ALTER TABLE audit_events ADD COLUMN {column} TEXT"
                    )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS risk_decisions (
                        decision_id TEXT PRIMARY KEY NOT NULL,
                        outcome TEXT NOT NULL
                            CHECK (outcome IN ('ALLOWED', 'REJECTED', 'NO_TRADE')),
                        allowed INTEGER NOT NULL CHECK (allowed IN (0, 1)),
                        reason TEXT,
                        policy_version TEXT NOT NULL,
                        inputs_hash TEXT NOT NULL,
                        plan_hash TEXT,
                        evaluated_at TEXT NOT NULL,
                        consumed_at TEXT,
                        CHECK ((allowed = 1) = (outcome = 'ALLOWED'))
                    ) STRICT, WITHOUT ROWID
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS kill_switch_events (
                        id INTEGER PRIMARY KEY,
                        action TEXT NOT NULL CHECK (action IN ('TRIPPED', 'CLEARED')),
                        cause TEXT NOT NULL,
                        detail TEXT NOT NULL,
                        actor TEXT NOT NULL,
                        occurred_at TEXT NOT NULL
                    ) STRICT
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS signed_plans (
                        idempotency_key TEXT PRIMARY KEY NOT NULL,
                        plan_hash TEXT NOT NULL,
                        risk_decision_id TEXT NOT NULL UNIQUE
                            REFERENCES risk_decisions(decision_id),
                        tx_hash TEXT NOT NULL,
                        signed_at TEXT NOT NULL
                    ) STRICT, WITHOUT ROWID
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS spot_fills (
                        client_order_id TEXT NOT NULL
                            REFERENCES spot_orders(client_order_id),
                        fill_id TEXT NOT NULL,
                        quantity TEXT NOT NULL,
                        price TEXT NOT NULL,
                        fee TEXT NOT NULL,
                        filled_at TEXT NOT NULL,
                        recorded_at TEXT NOT NULL,
                        PRIMARY KEY (client_order_id, fill_id)
                    ) STRICT, WITHOUT ROWID
                    """
                )
                connection.execute("PRAGMA user_version = 18")
            if version < 19:
                columns = connection.execute("PRAGMA table_info(spot_fills)").fetchall()
                if not any(column[1] == "cash_booking" for column in columns):
                    connection.execute(
                        "ALTER TABLE spot_fills ADD COLUMN cash_booking TEXT"
                    )
                connection.execute("PRAGMA user_version = 19")
        self._verify_integrity()

    def _verify_integrity(self) -> None:
        try:
            with self._connect() as connection:
                rows = connection.execute("PRAGMA quick_check").fetchall()
        except sqlite3.DatabaseError as error:
            raise PersistenceUnavailable(
                f"Audit storage failed its integrity check: {error}"
            ) from error
        if rows != [("ok",)]:
            raise PersistenceUnavailable("Audit storage failed its integrity check")

    def health(self) -> StoreHealth:
        """Non-raising store health check for status endpoints and startup."""
        try:
            self._verify_integrity()
            with self._connect() as connection:
                (version,) = connection.execute("PRAGMA user_version").fetchone()
                paused = self._execution_pause(connection).paused
        except sqlite3.Error as error:
            return StoreHealth(False, f"unavailable: {error}")
        if version != _SCHEMA_VERSION:
            return StoreHealth(False, "schema version mismatch", version, paused)
        return StoreHealth(True, "ok", version, paused)

    @contextmanager
    def research_worker_lock(self) -> Iterator[None]:
        # A separate SQLite file keeps the worker lock independent of data writes.
        path = self.path.resolve()
        lock_path = path.with_name(path.name + ".research-worker")
        with closing(sqlite3.connect(lock_path, timeout=0)) as lock:
            try:
                lock.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as error:
                if error.sqlite_errorcode == sqlite3.SQLITE_BUSY:
                    raise ValueError(
                        "A research schedule worker is already running"
                    ) from error
                raise
            yield

    def research_schedule(self) -> tuple[datetime | None, int]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT next_run_at, consecutive_failures "
                "FROM research_schedule WHERE id = 1"
            ).fetchone()
        return (datetime.fromisoformat(row[0]), int(row[1])) if row else (None, 0)

    def record_research_run(
        self,
        run: ResearchRun,
        *,
        schedule: tuple[datetime, int] | None = None,
    ) -> None:
        with self._connect() as connection, connection:
            connection.execute(
                "INSERT INTO research_runs VALUES (?, ?, ?, ?, ?)",
                (
                    run.run_id,
                    run.started_at.isoformat(timespec="microseconds"),
                    run.duration_ms,
                    run.status,
                    _RESEARCH_RUN.dump_json(run).decode("utf-8"),
                ),
            )
            if schedule is not None:
                next_run_at, failures = schedule
                connection.execute(
                    "INSERT INTO research_schedule VALUES (1, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET next_run_at = excluded.next_run_at, "
                    "consecutive_failures = excluded.consecutive_failures",
                    (next_run_at.isoformat(), failures),
                )

    def research_history(self, limit: int = 25) -> ResearchHistory:
        if not 1 <= limit <= 100:
            raise ValueError("Research history limit must be between 1 and 100")
        with self._connect() as connection, connection:
            connection.execute("BEGIN")
            rows = connection.execute(
                "SELECT payload FROM research_runs "
                "ORDER BY started_at DESC, run_id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            totals = connection.execute(
                "SELECT status, COUNT(*), AVG(duration_ms) FROM research_runs "
                "GROUP BY status ORDER BY status"
            ).fetchall()
        return ResearchHistory(
            runs=[_RESEARCH_RUN.validate_json(payload) for (payload,) in rows],
            statistics=[
                _RESEARCH_STATS.validate_python(
                    {"status": status, "count": count, "mean_duration_ms": duration}
                )
                for status, count, duration in totals
            ],
        )

    def save_wallet_snapshot(self, snapshot: WalletSnapshot) -> None:
        with self._connect() as connection, connection:
            connection.execute(
                """
                INSERT INTO wallet_snapshots (asset, address, observed_at, payload)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (asset, address) DO UPDATE
                SET observed_at = excluded.observed_at, payload = excluded.payload
                WHERE excluded.observed_at > wallet_snapshots.observed_at
                """,
                (
                    snapshot.asset,
                    snapshot.address,
                    snapshot.observed_at.isoformat(timespec="microseconds"),
                    WALLET_SNAPSHOT.dump_json(snapshot).decode("utf-8"),
                ),
            )

    def wallet_snapshots(self, *, limit: int) -> list[WalletSnapshot]:
        if not 1 <= limit <= 100:
            raise ValueError("Wallet snapshot limit must be between 1 and 100")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT payload FROM wallet_snapshots
                ORDER BY observed_at DESC, asset, address LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [WALLET_SNAPSHOT.validate_json(payload) for (payload,) in rows]

    @staticmethod
    def _execution_pause(connection: sqlite3.Connection) -> ExecutionPause:
        row = connection.execute(
            "SELECT paused, changed_at FROM execution_pause WHERE id = 1"
        ).fetchone()
        if row is None:
            raise sqlite3.DatabaseError("Missing execution pause state")
        paused, changed_at = row
        return ExecutionPause(
            bool(paused), datetime.fromisoformat(changed_at) if changed_at else None
        )

    def execution_pause(self) -> ExecutionPause:
        with self._connect() as connection:
            return self._execution_pause(connection)

    @staticmethod
    def _insert_audit(
        connection: sqlite3.Connection,
        kind: str,
        recorded_at: str | None = None,
        **fields: str | None,
    ) -> str:
        unknown = set(fields) - set(_AUDIT_FIELDS)
        if unknown:
            raise ValueError(f"Unknown audit fields: {sorted(unknown)}")
        event_id = uuid4().hex
        names = ["event_id", "recorded_at", "kind", *fields]
        connection.execute(
            f"INSERT INTO audit_events ({', '.join(names)}) "  # noqa: S608
            f"VALUES ({', '.join('?' * len(names))})",
            (
                event_id,
                recorded_at or datetime.now(UTC).isoformat(),
                kind,
                *fields.values(),
            ),
        )
        return event_id

    def _apply_pause(
        self,
        connection: sqlite3.Connection,
        paused: bool,
        *,
        cause: KillSwitchCause,
        detail: str,
        actor: str,
        kind: str,
        always_record: bool = False,
    ) -> bool:
        now = datetime.now(UTC).isoformat()
        updated = connection.execute(
            "UPDATE execution_pause SET paused = ?, changed_at = ? "
            "WHERE id = 1 AND paused <> ?",
            (int(paused), now, int(paused)),
        )
        changed = bool(updated.rowcount)
        if changed or (always_record and paused):
            connection.execute(
                "INSERT INTO kill_switch_events "
                "(action, cause, detail, actor, occurred_at) VALUES (?, ?, ?, ?, ?)",
                ("TRIPPED" if paused else "CLEARED", cause.value, detail, actor, now),
            )
            self._insert_audit(
                connection,
                kind,
                now,
                outcome=cause.value,
                client_id=actor,
                tool_name="kill_switch",
            )
        return changed

    def set_execution_pause(self, paused: bool) -> ExecutionPause:
        with self._connect() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._apply_pause(
                connection,
                paused,
                cause=KillSwitchCause.MANUAL,
                detail="operator pause" if paused else "operator resume",
                actor="operator",
                kind="execution.paused" if paused else "execution.resumed",
            )
            return self._execution_pause(connection)

    def trip_kill_switch(
        self, cause: KillSwitchCause, detail: str, *, actor: str = "system"
    ) -> KillSwitchState:
        """Persist a typed kill-switch trip; only an operator clear releases it."""
        if not detail.strip():
            raise ValueError("A kill-switch trip requires a detail message")
        with self._connect() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._apply_pause(
                connection,
                True,
                cause=cause,
                detail=detail[:500],
                actor=actor,
                kind="kill_switch.tripped",
                always_record=True,
            )
            return self._kill_switch_state(connection)

    def clear_kill_switch(self, operator: str, note: str) -> KillSwitchState:
        """Explicit local operator clear; requires an identified operator and note."""
        if not operator.strip() or not note.strip():
            raise ValueError("Clearing the kill switch requires an operator and a note")
        with self._connect() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._apply_pause(
                connection,
                False,
                cause=KillSwitchCause.MANUAL,
                detail=note[:500],
                actor=operator.strip(),
                kind="kill_switch.cleared",
            )
            return self._kill_switch_state(connection)

    def _kill_switch_state(self, connection: sqlite3.Connection) -> KillSwitchState:
        pause = self._execution_pause(connection)
        row = connection.execute(
            "SELECT cause, detail FROM kill_switch_events "
            "WHERE action = 'TRIPPED' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not pause.paused:
            return KillSwitchState(False, None, None, pause.changed_at)
        cause = KillSwitchCause(row[0]) if row else KillSwitchCause.MANUAL
        return KillSwitchState(True, cause, row[1] if row else None, pause.changed_at)

    def kill_switch_state(self) -> KillSwitchState:
        with self._connect() as connection:
            return self._kill_switch_state(connection)

    def kill_switch_history(self, limit: int = 50) -> list[tuple[str, str, str, str]]:
        """Return (action, cause, actor, occurred_at), newest first."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT action, cause, actor, occurred_at FROM kill_switch_events "
                "ORDER BY id DESC LIMIT ?",
                (max(1, min(limit, 500)),),
            ).fetchall()
        return [(str(a), str(c), str(o), str(t)) for a, c, o, t in rows]

    def fail_safe_paused(self) -> bool:
        """Persisted pause read that fails closed (for RiskEngine and the signer).

        Any storage fault reports "paused" and, where the store is still
        writable, persists a PERSISTENCE_FAILURE kill-switch trip.
        """
        try:
            return self.execution_pause().paused
        except sqlite3.Error as error:
            self._best_effort_trip(f"pause state unreadable: {error}")
            return True

    def _best_effort_trip(self, detail: str) -> None:
        if self._read_only:
            return
        try:
            self.trip_kill_switch(KillSwitchCause.PERSISTENCE_FAILURE, detail)
        except sqlite3.Error:
            return  # Unwritable: the caller still fails closed.

    # --- risk decisions ---

    def record_risk_decision(
        self,
        evaluation: RiskEvaluation,
        *,
        policy_version: str,
        inputs: Mapping[str, Any] | None = None,
        no_trade: bool = False,
        strategy_version: str | None = None,
    ) -> StoredRiskDecision:
        """Persist every risk verdict (allow, reject and NO_TRADE) exactly once."""
        if not policy_version.strip():
            raise ValueError("A risk decision requires a policy version")
        if evaluation.allowed and no_trade:
            raise ValueError("An allowed decision cannot be NO_TRADE")
        if evaluation.allowed:
            outcome = RiskOutcome.ALLOWED
        elif no_trade:
            outcome = RiskOutcome.NO_TRADE
        else:
            outcome = RiskOutcome.REJECTED
        inputs_hash = canonical_hash(
            {
                "inputs": dict(inputs or {}),
                "plan_hash": evaluation.plan_hash,
                "reason": evaluation.reason,
            }
        )
        evaluated_at = evaluation.evaluated_at.isoformat()
        with self._connect() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            inserted = connection.execute(
                "INSERT INTO risk_decisions (decision_id, outcome, allowed, reason, "
                "policy_version, inputs_hash, plan_hash, evaluated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (decision_id) DO NOTHING",
                (
                    evaluation.decision_id,
                    outcome.value,
                    int(evaluation.allowed),
                    evaluation.reason,
                    policy_version,
                    inputs_hash,
                    evaluation.plan_hash,
                    evaluated_at,
                ),
            )
            if inserted.rowcount:
                self._insert_audit(
                    connection,
                    "risk.decision.recorded",
                    evaluated_at,
                    risk_decision_id=evaluation.decision_id,
                    policy_version=policy_version,
                    strategy_version=strategy_version,
                    outcome=outcome.value,
                )
            stored = self._risk_decision(connection, evaluation.decision_id)
        if stored is None or (
            stored.inputs_hash,
            stored.allowed,
            stored.plan_hash,
        ) != (inputs_hash, evaluation.allowed, evaluation.plan_hash):
            raise ValueError("Risk decision ID was already used for another verdict")
        return stored

    @staticmethod
    def _risk_decision(
        connection: sqlite3.Connection, decision_id: str
    ) -> StoredRiskDecision | None:
        row = connection.execute(
            "SELECT decision_id, outcome, allowed, reason, policy_version, "
            "inputs_hash, plan_hash, evaluated_at, consumed_at "
            "FROM risk_decisions WHERE decision_id = ?",
            (decision_id,),
        ).fetchone()
        if row is None:
            return None
        return StoredRiskDecision(
            decision_id=row[0],
            outcome=RiskOutcome(row[1]),
            allowed=bool(row[2]),
            reason=row[3],
            policy_version=row[4],
            inputs_hash=row[5],
            plan_hash=row[6],
            evaluated_at=datetime.fromisoformat(row[7]),
            consumed_at=datetime.fromisoformat(row[8]) if row[8] else None,
        )

    def get_risk_decision(self, decision_id: str) -> StoredRiskDecision | None:
        with self._connect() as connection:
            return self._risk_decision(connection, decision_id)

    def list_risk_decisions(self, limit: int = 50) -> list[StoredRiskDecision]:
        with self._connect() as connection:
            ids = connection.execute(
                "SELECT decision_id FROM risk_decisions "
                "ORDER BY evaluated_at DESC, decision_id LIMIT ?",
                (max(1, min(limit, 500)),),
            ).fetchall()
            decisions = [self._risk_decision(connection, i) for (i,) in ids]
        return [d for d in decisions if d is not None]

    def approved_risk_decision(self, decision_id: str) -> ApprovedRiskDecision | None:
        """Store-backed signer lookup; a consumed or unbound decision never allows."""
        stored = self.get_risk_decision(decision_id)
        if stored is None:
            return None
        return ApprovedRiskDecision(
            decision_id=stored.decision_id,
            plan_hash=stored.plan_hash or "",
            allowed=bool(
                stored.allowed and stored.plan_hash and stored.consumed_at is None
            ),
        )

    # --- signed plans ---

    def is_plan_signed(self, idempotency_key: str) -> bool:
        with self._connect() as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM signed_plans WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                is not None
            )

    def commit_signed_plan(
        self,
        *,
        idempotency_key: str,
        plan_hash: str,
        risk_decision_id: str,
        tx_hash: str,
        signed_at: datetime,
    ) -> None:
        """Atomically consume the risk decision and record the signature."""
        now = signed_at.isoformat()
        try:
            with self._connect() as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                consumed = connection.execute(
                    "UPDATE risk_decisions SET consumed_at = ? "
                    "WHERE decision_id = ? AND allowed = 1 AND plan_hash = ? "
                    "AND consumed_at IS NULL",
                    (now, risk_decision_id, plan_hash),
                )
                if not consumed.rowcount:
                    raise SignedPlanConflict(
                        "Risk decision is unknown, not approved for this plan, "
                        "or already consumed"
                    )
                connection.execute(
                    "INSERT INTO signed_plans VALUES (?, ?, ?, ?, ?)",
                    (idempotency_key, plan_hash, risk_decision_id, tx_hash, now),
                )
                self._insert_audit(
                    connection,
                    "signer.plan.signed",
                    now,
                    idempotency_key=idempotency_key,
                    risk_decision_id=risk_decision_id,
                    order_or_tx_id=tx_hash,
                    outcome="signed",
                    tool_name="sign_transaction",
                )
        except sqlite3.IntegrityError as error:
            raise SignedPlanConflict(
                "Idempotency key or risk decision was already signed"
            ) from error

    # --- structured audit ---

    def record_structured_audit(
        self,
        kind: str,
        *,
        arguments: Mapping[str, Any] | None = None,
        **fields: str | None,
    ) -> StructuredAuditEvent:
        """Record a spec 12.3 audit event; arguments are redacted before storage."""
        if arguments is not None:
            fields["args_hash"] = canonical_hash(arguments)
            fields["redacted_args"] = json.dumps(
                redact_arguments(arguments), sort_keys=True, separators=(",", ":")
            )
        with self._connect() as connection, connection:
            event_id = self._insert_audit(connection, kind, None, **fields)
            row = connection.execute(
                f"SELECT event_id, recorded_at, kind, {', '.join(_AUDIT_FIELDS)} "  # noqa: S608
                "FROM audit_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
        return StructuredAuditEvent(*row)

    def list_structured_audit(self, limit: int = 50) -> list[StructuredAuditEvent]:
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT event_id, recorded_at, kind, {', '.join(_AUDIT_FIELDS)} "  # noqa: S608
                "FROM audit_events ORDER BY rowid DESC LIMIT ?",
                (max(1, min(limit, 500)),),
            ).fetchall()
        return [StructuredAuditEvent(*row) for row in rows]

    # --- spot fills ---

    def record_spot_fill(self, fill: SpotFill) -> bool:
        """Idempotently record one fill; a conflicting replay is an error."""
        with self._connect() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._record_spot_fill(connection, fill)

    def import_spot_fills(self, order: SpotOrder, fills: list[SpotFill]) -> int:
        """Commit a verified batch only if the local order is still current."""
        with self._connect() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            if self._get_spot_order(connection, order.client_order_id) != order:
                raise ValueError("Local spot order changed before import")
            if any(fill.client_order_id != order.client_order_id for fill in fills):
                raise ValueError("Spot fill does not match the local order")
            ids = {fill.fill_id for fill in fills}
            existing = connection.execute(
                "SELECT fill_id FROM spot_fills WHERE client_order_id = ?",
                (order.client_order_id,),
            ).fetchall()
            if any(row[0] not in ids for row in existing):
                raise ValueError("Previously recorded fills are missing from the venue")
            return sum(self._record_spot_fill(connection, fill) for fill in fills)

    def _record_spot_fill(self, connection: sqlite3.Connection, fill: SpotFill) -> bool:
        if (
            not (fill.quantity.is_finite() and fill.price.is_finite())
            or not fill.fee.is_finite()
            or fill.quantity <= 0
            or fill.price <= 0
            or fill.fee < 0
        ):
            raise ValueError("Spot fill amounts must be finite and positive")
        values = (
            fill.client_order_id,
            fill.fill_id,
            str(fill.quantity),
            str(fill.price),
            str(fill.fee),
            fill.filled_at.isoformat(),
        )
        booking = (
            _SPOT_CASH_TRADE.dump_json(fill.cash_booking).decode()
            if fill.cash_booking is not None
            else None
        )
        inserted = connection.execute(
            "INSERT INTO spot_fills VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (client_order_id, fill_id) DO NOTHING",
            (*values, datetime.now(UTC).isoformat(), booking),
        )
        if inserted.rowcount:
            self._insert_audit(
                connection,
                "spot.fill.recorded",
                order_or_tx_id=fill.client_order_id,
                idempotency_key=fill.fill_id,
                outcome="recorded",
            )
            return True
        existing = connection.execute(
            "SELECT client_order_id, fill_id, quantity, price, fee, filled_at, "
            "cash_booking "
            "FROM spot_fills WHERE client_order_id = ? AND fill_id = ?",
            values[:2],
        ).fetchone()
        if tuple(existing[:6]) != values:
            raise ValueError("Fill ID was already recorded with different values")
        if fill.cash_booking is not None and existing[6] is None:
            connection.execute(
                "UPDATE spot_fills SET cash_booking = ? "
                "WHERE client_order_id = ? AND fill_id = ?",
                (booking, *values[:2]),
            )
            self._insert_audit(
                connection,
                "spot.fill.cash-booking.verified",
                order_or_tx_id=fill.client_order_id,
                idempotency_key=fill.fill_id,
                outcome="recorded",
            )
        elif fill.cash_booking is not None and (
            _SPOT_CASH_TRADE.validate_json(existing[6]) != fill.cash_booking
        ):
            raise ValueError(
                "Fill ID was already recorded with a different cash booking"
            )
        return False

    def spot_fills(self, client_order_id: str) -> list[SpotFill]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT client_order_id, fill_id, quantity, price, fee, filled_at, "
                "cash_booking "
                "FROM spot_fills WHERE client_order_id = ? ORDER BY filled_at, fill_id",
                (client_order_id,),
            ).fetchall()
        return [
            SpotFill(
                r[0],
                r[1],
                Decimal(r[2]),
                Decimal(r[3]),
                Decimal(r[4]),
                datetime.fromisoformat(r[5]),
                _SPOT_CASH_TRADE.validate_json(r[6]) if r[6] is not None else None,
            )
            for r in rows
        ]

    def spot_cash_trades(self, venue: str) -> dict[str, SpotCashTrade]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT f.fill_id, o.market, o.side, f.quantity, f.price, f.fee, "
                "f.cash_booking "
                "FROM spot_fills f JOIN spot_orders o USING (client_order_id) "
                "WHERE o.venue = ?",
                (venue,),
            ).fetchall()
        trades: dict[str, SpotCashTrade] = {}
        for fill_id, market, side, quantity, price, fee, booking in rows:
            if market != "BTC/USD" or not fill_id or fill_id in trades:
                raise ValueError("Unsupported or duplicate local spot fill")
            btc = Decimal(quantity)
            if SpotSide(side) is SpotSide.SELL:
                btc = -btc
            trades[fill_id] = (
                _SPOT_CASH_TRADE.validate_json(booking)
                if booking is not None
                else SpotCashTrade(btc, -btc * Decimal(price), Decimal(fee))
            )
        return trades

    def record_paper_fill(
        self,
        order: PaperOrder,
        price: Decimal,
        fee: Decimal,
        *,
        quote: Quote | None = None,
    ) -> PaperFill:
        with self._connect() as connection, connection:
            # Serialize both replay detection and accounting with competing fills.
            connection.execute("BEGIN IMMEDIATE")
            existing = self._existing_paper_fill(connection, order)
            if existing is not None:
                return existing

            if self._execution_pause(connection).paused:
                raise PaperRejected("Execution is paused")
            balance = self._paper_balance(connection)
            fill = book_fill(balance, order, price, fee, datetime.now(UTC), quote)
            connection.execute(
                "INSERT INTO paper_fills (order_key, payload) VALUES (?, ?)",
                (order.idempotency_key, _PAPER_FILL.dump_json(fill).decode("utf-8")),
            )
            return fill

    @staticmethod
    def _existing_paper_fill(
        connection: sqlite3.Connection, order: PaperOrder
    ) -> PaperFill | None:
        row = connection.execute(
            "SELECT payload FROM paper_fills WHERE order_key = ?",
            (order.idempotency_key,),
        ).fetchone()
        if row is None:
            return None
        fill = _PAPER_FILL.validate_json(row[0])
        if fill.order != order:
            raise PaperConflict("Paper order ID was already used")
        return fill

    def existing_paper_fill(self, order: PaperOrder) -> PaperFill | None:
        with self._connect() as connection:
            return self._existing_paper_fill(connection, order)

    @staticmethod
    def _paper_balance(connection: sqlite3.Connection) -> PaperBalance:
        row = connection.execute(
            "SELECT payload FROM paper_fills ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return _PAPER_FILL.validate_json(row[0]).balance if row else PaperBalance()

    def paper_balance(self) -> PaperBalance:
        with self._connect() as connection:
            return self._paper_balance(connection)

    def paper_fills(self, *, limit: int) -> list[PaperFill]:
        if not 1 <= limit <= 100:
            raise ValueError("Paper fill history limit must be between 1 and 100")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM paper_fills ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_PAPER_FILL.validate_json(payload) for (payload,) in rows]

    def freeze_holdout(self, commitment: str, candidate: str) -> None:
        with self._connect() as connection, connection:
            connection.execute(
                """
                INSERT INTO research_holdouts (commitment, candidate) VALUES (?, ?)
                ON CONFLICT (commitment) DO NOTHING
                """,
                (commitment, candidate),
            )
            (original,) = connection.execute(
                "SELECT candidate FROM research_holdouts WHERE commitment = ?",
                (commitment,),
            ).fetchone()
            if original != candidate:
                raise ValueError(
                    "A different candidate is already frozen for this holdout"
                )

    def frozen_candidate(self, commitment: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT candidate FROM research_holdouts WHERE commitment = ?",
                (commitment,),
            ).fetchone()
        return str(row[0]) if row is not None else None

    def finalize_holdout(self, commitment: str, result: str) -> str:
        with self._connect() as connection, connection:
            connection.execute(
                "UPDATE research_holdouts SET result = ? "
                "WHERE commitment = ? AND result IS NULL",
                (result, commitment),
            )
            row = connection.execute(
                "SELECT result FROM research_holdouts WHERE commitment = ?",
                (commitment,),
            ).fetchone()
            if row is None:
                raise ValueError("No candidate is frozen for this holdout")
            return str(row[0])

    def scored_holdout(self, commitment: str) -> tuple[str, str] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT candidate, result FROM research_holdouts "
                "WHERE commitment = ? AND result IS NOT NULL",
                (commitment,),
            ).fetchone()
        return (str(row[0]), str(row[1])) if row is not None else None

    def record_backtest(self, report_id: str, payload: str) -> None:
        with self._connect() as connection, connection:
            connection.execute(
                """
                INSERT INTO backtest_reports (id, recorded_at, payload)
                VALUES (?, ?, ?) ON CONFLICT (id) DO NOTHING
                """,
                (report_id, datetime.now(UTC).isoformat(), payload),
            )

    def backtest_report(self, report_id: str) -> ResearchRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT recorded_at, payload FROM backtest_reports WHERE id = ?",
                (report_id,),
            ).fetchone()
        if row is None:
            return None
        return ResearchRecord(datetime.fromisoformat(row[0]), row[1])

    def record_macro(self, observations: list[MacroObservation]) -> int:
        with self._connect() as connection, connection:
            for observation in observations:
                key = (
                    observation.source,
                    observation.series,
                    observation.observation_date.isoformat(),
                )
                value = str(observation.value)
                received_at = observation.received_at.isoformat(timespec="microseconds")
                vintage = (
                    observation.vintage_date.isoformat()
                    if observation.vintage_date
                    else None
                )
                connection.execute(
                    """
                    INSERT INTO macro_observations
                        (source, series, observation_date, received_at,
                         value, vintage_date)
                    WITH latest AS (
                        SELECT value, received_at FROM macro_observations
                        WHERE source = ? AND series = ? AND observation_date = ?
                          AND vintage_date IS ?
                        ORDER BY id DESC LIMIT 1
                    )
                    SELECT ?, ?, ?, ?, ?, ?
                    WHERE NOT EXISTS (
                        SELECT 1 FROM latest WHERE value = ? OR received_at >= ?
                    )
                    """,
                    (
                        *key,
                        vintage,
                        *key,
                        received_at,
                        value,
                        vintage,
                        value,
                        received_at,
                    ),
                )
            return connection.total_changes

    def macro_history(
        self, source: str, series: str, *, limit: int
    ) -> list[MacroObservation]:
        if not 1 <= limit <= 1000:
            raise ValueError("Macro history limit must be between 1 and 1000")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT observation_date, received_at, value, vintage_date
                FROM macro_observations
                WHERE source = ? AND series = ?
                ORDER BY observation_date DESC, id DESC LIMIT ?
                """,
                (source, series, limit),
            ).fetchall()
        return [
            MacroObservation(
                source,
                series,
                date.fromisoformat(day),
                datetime.fromisoformat(received),
                Decimal(value),
                date.fromisoformat(vintage) if vintage is not None else None,
            )
            for day, received, value, vintage in reversed(rows)
        ]

    def record_news(self, items: list[NewsItem]) -> int:
        with self._connect() as connection, connection:
            connection.executemany(
                """
                INSERT INTO news_items VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (source, url) DO NOTHING
                """,
                (
                    (
                        item.source,
                        item.url,
                        item.title,
                        item.published_at.isoformat(timespec="microseconds"),
                        item.received_at.isoformat(timespec="microseconds"),
                    )
                    for item in items
                ),
            )
            return connection.total_changes

    def news(self, *, limit: int) -> list[NewsItem]:
        if not 1 <= limit <= 100:
            raise ValueError("News limit must be between 1 and 100")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT source, url, title, published_at, received_at
                FROM news_items ORDER BY published_at DESC, source, url LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            NewsItem(
                source,
                url,
                title,
                datetime.fromisoformat(published),
                datetime.fromisoformat(received),
            )
            for source, url, title, published, received in rows
        ]

    def record_candles(self, candles: list[Candle]) -> int:
        with self._connect() as connection, connection:
            connection.executemany(
                """
                INSERT INTO market_candles (
                    venue, base_asset, quote_asset, source, interval_minutes,
                    opened_at, received_at, opening, high, low, closing,
                    volume, trade_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (venue, base_asset, quote_asset, source,
                             interval_minutes, opened_at) DO NOTHING
                """,
                (
                    (
                        candle.market.venue,
                        str(candle.market.base),
                        str(candle.market.quote),
                        candle.source,
                        candle.interval_minutes,
                        candle.opened_at.isoformat(timespec="microseconds"),
                        candle.received_at.isoformat(),
                        str(candle.open),
                        str(candle.high),
                        str(candle.low),
                        str(candle.close),
                        str(candle.volume),
                        candle.trade_count,
                    )
                    for candle in candles
                ),
            )
            return connection.total_changes

    def candle_history(
        self, market: MarketId, source: str, interval_minutes: int, *, limit: int
    ) -> list[Candle]:
        if not 1 <= limit <= 1000:
            raise ValueError("Candle history limit must be between 1 and 1000")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT opened_at, received_at, opening, high, low, closing,
                       volume, trade_count
                FROM market_candles
                WHERE venue = ? AND base_asset = ? AND quote_asset = ?
                    AND source = ? AND interval_minutes = ?
                ORDER BY opened_at DESC LIMIT ?
                """,
                (
                    market.venue,
                    str(market.base),
                    str(market.quote),
                    source,
                    interval_minutes,
                    limit,
                ),
            ).fetchall()
        return [
            Candle(
                market,
                source,
                interval_minutes,
                datetime.fromisoformat(opened_at),
                datetime.fromisoformat(received_at),
                Decimal(opening),
                Decimal(high),
                Decimal(low),
                Decimal(closing),
                Decimal(volume),
                trade_count,
            )
            for (
                opened_at,
                received_at,
                opening,
                high,
                low,
                closing,
                volume,
                trade_count,
            ) in reversed(rows)
        ]

    def record_quote(self, quote: Quote) -> None:
        with self._connect() as connection, connection:
            connection.execute(
                """
                INSERT INTO market_quotes (
                    venue, base_asset, quote_asset, source,
                    bid, ask, bid_updated_at, ask_updated_at, received_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (venue, base_asset, quote_asset, source,
                             bid_updated_at, ask_updated_at, bid, ask)
                DO NOTHING
                """,
                (
                    quote.market.venue,
                    str(quote.market.base),
                    str(quote.market.quote),
                    quote.source,
                    str(quote.bid),
                    str(quote.ask),
                    quote.bid_updated_at.isoformat(),
                    quote.ask_updated_at.isoformat(),
                    quote.received_at.isoformat(),
                ),
            )

    def quote_history(self, market: MarketId, *, limit: int) -> list[Quote]:
        if not 1 <= limit <= 500:
            raise ValueError("Quote history limit must be between 1 and 500")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT source, bid, ask, bid_updated_at, ask_updated_at, received_at
                FROM market_quotes
                WHERE venue = ? AND base_asset = ? AND quote_asset = ?
                ORDER BY id DESC LIMIT ?
                """,
                (market.venue, str(market.base), str(market.quote), limit),
            ).fetchall()
        return [
            Quote(
                market,
                source,
                Decimal(bid),
                Decimal(ask),
                datetime.fromisoformat(bid_at),
                datetime.fromisoformat(ask_at),
                datetime.fromisoformat(received_at),
            )
            for source, bid, ask, bid_at, ask_at, received_at in rows
        ]

    def authorize_spot_canary(
        self,
        candidate: StrategyCandidate,
        key_fingerprint: str,
        max_notional: Decimal,
        at: datetime,
        expires_at: datetime,
    ) -> None:
        if (
            not max_notional.is_finite()
            or not 10 <= max_notional <= 1000
            or not at < expires_at <= at + timedelta(hours=24)
        ):
            raise ValueError("Canary authorization bounds are invalid")
        with self._connect() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT 1 FROM strategy_candidates "
                "WHERE candidate_id = ? AND stage IN ('PAPER_APPROVED', 'LIVE_CANARY') "
                "AND updated_at = ? AND version = ? AND dataset_hash = ?",
                (
                    candidate.candidate_id,
                    candidate.updated_at.isoformat(),
                    candidate.version,
                    candidate.dataset_hash,
                ),
            ).fetchone()
            if current is None:
                raise RiskRejected("A paper-approved strategy candidate is required")
            connection.execute(
                "INSERT INTO spot_canary_approval VALUES (1, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET candidate_id = excluded.candidate_id, "
                "candidate_version = excluded.candidate_version, "
                "dataset_hash = excluded.dataset_hash, "
                "key_fingerprint = excluded.key_fingerprint, "
                "max_notional = excluded.max_notional, "
                "expires_at = excluded.expires_at",
                (
                    candidate.candidate_id,
                    candidate.version,
                    candidate.dataset_hash,
                    key_fingerprint,
                    str(max_notional),
                    expires_at.isoformat(),
                ),
            )
            connection.execute(
                "INSERT INTO audit_events (event_id, recorded_at, kind) "
                "VALUES (?, ?, ?)",
                (uuid4().hex, at.isoformat(), "spot.canary.authorized"),
            )

    def revoke_spot_canary(self) -> None:
        with self._connect() as connection, connection:
            deleted = connection.execute(
                "DELETE FROM spot_canary_approval WHERE id = 1"
            )
            if deleted.rowcount:
                connection.execute(
                    "INSERT INTO audit_events (event_id, recorded_at, kind) "
                    "VALUES (?, ?, ?)",
                    (uuid4().hex, datetime.now(UTC).isoformat(), "spot.canary.revoked"),
                )

    @staticmethod
    def _canary_approval(
        connection: sqlite3.Connection,
        candidate_id: str,
        at: datetime,
    ) -> tuple[str, Decimal, str, str] | None:
        row = connection.execute(
            """
            SELECT a.key_fingerprint, a.max_notional, c.version, c.stage
            FROM spot_canary_approval AS a
            JOIN strategy_candidates AS c ON c.candidate_id = a.candidate_id
            WHERE a.id = 1 AND c.candidate_id = ? AND a.expires_at > ?
            AND c.version = a.candidate_version AND c.dataset_hash = a.dataset_hash
            """,
            (candidate_id, at.isoformat()),
        ).fetchone()
        return (str(row[0]), Decimal(row[1]), str(row[2]), str(row[3])) if row else None

    def has_spot_canary_approval(self, candidate_id: str, at: datetime) -> bool:
        with self._connect() as connection:
            return self._canary_approval(connection, candidate_id, at) is not None

    def get_spot_canary_approval(
        self, candidate_id: str, at: datetime | None = None
    ) -> SpotCanaryApproval | None:
        at_dt = at or datetime.now(UTC)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT a.candidate_id, a.candidate_version, a.dataset_hash,
                       a.key_fingerprint, a.max_notional, a.expires_at, c.stage
                FROM spot_canary_approval AS a
                JOIN strategy_candidates AS c ON c.candidate_id = a.candidate_id
                WHERE a.id = 1 AND c.candidate_id = ?
                """,
                (candidate_id,),
            ).fetchone()
            if row is None:
                return None
            expires_at = datetime.fromisoformat(row[5])
            active = expires_at > at_dt and row[6] == PromotionStage.LIVE_CANARY
            return SpotCanaryApproval(
                candidate_id=str(row[0]),
                candidate_version=str(row[1]),
                dataset_hash=str(row[2]),
                key_fingerprint=str(row[3]),
                max_notional=Decimal(str(row[4])),
                expires_at=expires_at,
                stage=str(row[6]),
                active=active,
            )

    def reserve_spot_submission(
        self,
        order: SpotOrder,
        max_open_orders: int,
        *,
        candidate_id: str,
        strategy_version: str,
        key_fingerprint: str,
    ) -> None:
        """Reserve a submission; every rejection or storage fault is a NO_TRADE."""
        if order.state != OrderState.SUBMITTED:
            raise ValueError("Submission reservation requires a submitted order")
        try:
            self._reserve_spot_submission(
                order,
                max_open_orders,
                candidate_id=candidate_id,
                strategy_version=strategy_version,
                key_fingerprint=key_fingerprint,
            )
        except PersistenceUnavailable as error:
            self._best_effort_trip(f"submission reservation failed: {error}")
            raise RiskRejected(
                "Persistence unavailable: execution blocked (NO_TRADE)"
            ) from error
        except RiskRejected as rejection:
            self._audit_rejection(order, strategy_version, str(rejection))
            raise

    def _audit_rejection(
        self, order: SpotOrder, strategy_version: str, reason: str
    ) -> None:
        try:
            self.record_structured_audit(
                "spot.submission.rejected",
                arguments={"reason": reason, "market": order.market},
                strategy_version=strategy_version,
                risk_decision_id=order.risk_decision_id,
                order_or_tx_id=order.client_order_id,
                idempotency_key=order.client_order_id,
                outcome="NO_TRADE",
            )
        except sqlite3.Error:
            return  # The rejection itself still stands.

    def _reserve_spot_submission(
        self,
        order: SpotOrder,
        max_open_orders: int,
        *,
        candidate_id: str,
        strategy_version: str,
        key_fingerprint: str,
    ) -> None:
        with self._connect() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            if self._execution_pause(connection).paused:
                raise RiskRejected("Execution is paused")
            approval = self._canary_approval(
                connection, candidate_id, datetime.now(UTC)
            )
            if approval is None or (
                approval[0] != key_fingerprint
                or approval[2] != strategy_version
                or approval[3] != PromotionStage.LIVE_CANARY
                or order.limit_price is None
                or order.limit_price * order.quantity > approval[1]
            ):
                raise RiskRejected(
                    "No matching active canary authorization for this order"
                )
            if connection.execute(
                "SELECT 1 FROM spot_orders WHERE client_order_id = ?",
                (order.client_order_id,),
            ).fetchone():
                raise RiskRejected("Client order ID is already recorded")
            (unresolved, active) = connection.execute(
                """
                SELECT COUNT(CASE WHEN state IN (
                    'SUBMITTED', 'RECONCILIATION_REQUIRED'
                ) THEN 1 END), COUNT(*)
                FROM spot_orders
                WHERE venue = ? AND state IN (
                    'SUBMITTED', 'RECONCILIATION_REQUIRED', 'ACKNOWLEDGED',
                    'PARTIALLY_FILLED'
                )
                """,
                (order.venue,),
            ).fetchone()
            if unresolved:
                raise RiskRejected("Venue has an unresolved submission")
            if active >= max_open_orders:
                raise RiskRejected("Venue has reached its open-order limit")
            connection.execute(
                """
                INSERT INTO spot_orders (
                    client_order_id, order_id, venue, market, side, order_type,
                    quantity, limit_price, state, risk_decision_id, created_at,
                    updated_at, canary_candidate_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order.client_order_id,
                    order.order_id,
                    order.venue,
                    order.market,
                    order.side.value,
                    order.order_type.value,
                    str(order.quantity),
                    str(order.limit_price) if order.limit_price is not None else None,
                    order.state.value,
                    order.risk_decision_id,
                    order.created_at.isoformat(),
                    order.updated_at.isoformat(),
                    candidate_id,
                ),
            )
            self._insert_audit(
                connection,
                "spot.submission.reserved",
                order.updated_at.isoformat(),
                strategy_version=strategy_version,
                risk_decision_id=order.risk_decision_id,
                order_or_tx_id=order.client_order_id,
                idempotency_key=order.client_order_id,
                outcome="reserved",
            )

    def apply_spot_observation(self, before: SpotOrder, after: SpotOrder) -> bool:
        if before == after:
            return True
        before.state.advance(after.state)
        with self._connect() as connection, connection:
            result = connection.execute(
                """
                UPDATE spot_orders
                SET state = ?, updated_at = ?, venue_order_id = ?,
                    filled_quantity = ?, average_price = ?, fee = ?
                WHERE client_order_id = ? AND order_id = ?
                    AND state = ? AND updated_at = ?
                """,
                (
                    after.state.value,
                    after.updated_at.isoformat(),
                    after.venue_order_id,
                    str(after.filled_quantity)
                    if after.filled_quantity is not None
                    else None,
                    str(after.average_price)
                    if after.average_price is not None
                    else None,
                    str(after.fee) if after.fee is not None else None,
                    before.client_order_id,
                    before.order_id,
                    before.state.value,
                    before.updated_at.isoformat(),
                ),
            )
            if result.rowcount:
                self._insert_audit(
                    connection,
                    f"spot.order.{after.state.value.lower()}",
                    after.updated_at.isoformat(),
                    risk_decision_id=after.risk_decision_id,
                    order_or_tx_id=after.client_order_id,
                    idempotency_key=after.client_order_id,
                    outcome=after.state.value,
                )
            return result.rowcount == 1

    def save_spot_order(self, order: SpotOrder) -> None:
        with self._connect() as connection, connection:
            connection.execute(
                """
                INSERT INTO spot_orders (
                    client_order_id, order_id, venue, market, side, order_type,
                    quantity, limit_price, state, risk_decision_id, created_at,
                    updated_at, venue_order_id, filled_quantity, average_price, fee
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (client_order_id) DO UPDATE SET
                    state = excluded.state,
                    updated_at = excluded.updated_at,
                    venue_order_id = excluded.venue_order_id,
                    filled_quantity = excluded.filled_quantity,
                    average_price = excluded.average_price,
                    fee = excluded.fee
                """,
                (
                    order.client_order_id,
                    order.order_id,
                    order.venue,
                    order.market,
                    order.side.value,
                    order.order_type.value,
                    str(order.quantity),
                    str(order.limit_price) if order.limit_price is not None else None,
                    order.state.value,
                    order.risk_decision_id,
                    order.created_at.isoformat(),
                    order.updated_at.isoformat(),
                    order.venue_order_id,
                    (
                        str(order.filled_quantity)
                        if order.filled_quantity is not None
                        else None
                    ),
                    (
                        str(order.average_price)
                        if order.average_price is not None
                        else None
                    ),
                    str(order.fee) if order.fee is not None else None,
                ),
            )

    def get_spot_order(self, client_order_id: str) -> SpotOrder | None:
        with self._connect() as connection:
            return self._get_spot_order(connection, client_order_id)

    @staticmethod
    def _get_spot_order(
        connection: sqlite3.Connection, client_order_id: str
    ) -> SpotOrder | None:
        row = connection.execute(
            """
            SELECT client_order_id, order_id, venue, market, side, order_type,
                   quantity, limit_price, state, risk_decision_id, created_at,
                   updated_at, venue_order_id, filled_quantity, average_price, fee
            FROM spot_orders WHERE client_order_id = ?
            """,
            (client_order_id,),
        ).fetchone()
        if row is None:
            return None
        return SpotOrder(
            client_order_id=row[0],
            order_id=row[1],
            venue=row[2],
            market=row[3],
            side=SpotSide(row[4]),
            order_type=SpotOrderType(row[5]),
            quantity=Decimal(row[6]),
            limit_price=Decimal(row[7]) if row[7] is not None else None,
            state=OrderState(row[8]),
            risk_decision_id=row[9],
            created_at=datetime.fromisoformat(row[10]),
            updated_at=datetime.fromisoformat(row[11]),
            venue_order_id=row[12],
            filled_quantity=Decimal(row[13]) if row[13] is not None else None,
            average_price=Decimal(row[14]) if row[14] is not None else None,
            fee=Decimal(row[15]) if row[15] is not None else None,
        )

    def list_open_spot_orders(self, venue: str) -> list[SpotOrder]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT client_order_id, order_id, venue, market, side, order_type,
                       quantity, limit_price, state, risk_decision_id, created_at,
                       updated_at, venue_order_id, filled_quantity, average_price, fee
                FROM spot_orders
                WHERE venue = ? AND state IN (
                    'SUBMITTED', 'ACKNOWLEDGED', 'PARTIALLY_FILLED',
                    'RECONCILIATION_REQUIRED'
                )
                ORDER BY created_at ASC
                """,
                (venue,),
            ).fetchall()
            return [
                SpotOrder(
                    client_order_id=r[0],
                    order_id=r[1],
                    venue=r[2],
                    market=r[3],
                    side=SpotSide(r[4]),
                    order_type=SpotOrderType(r[5]),
                    quantity=Decimal(r[6]),
                    limit_price=Decimal(r[7]) if r[7] is not None else None,
                    state=OrderState(r[8]),
                    risk_decision_id=r[9],
                    created_at=datetime.fromisoformat(r[10]),
                    updated_at=datetime.fromisoformat(r[11]),
                    venue_order_id=r[12],
                    filled_quantity=Decimal(r[13]) if r[13] is not None else None,
                    average_price=Decimal(r[14]) if r[14] is not None else None,
                    fee=Decimal(r[15]) if r[15] is not None else None,
                )
                for r in rows
            ]

    def record_audit_event(self, kind: str) -> AuditEvent:
        event = AuditEvent(uuid4().hex, datetime.now(UTC).isoformat(), kind)
        with self._connect() as connection, connection:
            connection.execute(
                "INSERT INTO audit_events (event_id, recorded_at, kind)"
                " VALUES (?, ?, ?)",
                (event.event_id, event.recorded_at, event.kind),
            )
        return event

    def record_startup(self) -> AuditEvent:
        return self.record_audit_event("app.started")

    def latest_event(self) -> AuditEvent | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT event_id, recorded_at, kind FROM audit_events"
                " ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
        return AuditEvent(*row) if row is not None else None

    def list_audit_events(self, limit: int = 50) -> list[AuditEvent]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT event_id, recorded_at, kind FROM audit_events"
                " ORDER BY rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [AuditEvent(*row) for row in rows]

    def save_strategy_candidate(self, candidate: StrategyCandidate) -> None:
        with self._connect() as connection, connection:
            connection.execute(
                """
                INSERT INTO strategy_candidates (
                    candidate_id, name, version, stage, parameters,
                    dataset_hash, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(candidate_id) DO UPDATE SET
                    name = excluded.name,
                    version = excluded.version,
                    stage = excluded.stage,
                    parameters = excluded.parameters,
                    dataset_hash = excluded.dataset_hash,
                    updated_at = excluded.updated_at
                """,
                (
                    candidate.candidate_id,
                    candidate.name,
                    candidate.version,
                    candidate.stage.value,
                    json.dumps(candidate.parameters),
                    candidate.dataset_hash,
                    candidate.created_at.isoformat(),
                    candidate.updated_at.isoformat(),
                ),
            )

    def get_strategy_candidate(self, candidate_id: str) -> StrategyCandidate | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT candidate_id, name, version, stage, parameters,
                       dataset_hash, created_at, updated_at
                FROM strategy_candidates WHERE candidate_id = ?
                """,
                (candidate_id,),
            ).fetchone()
            if row is None:
                return None
            return StrategyCandidate(
                candidate_id=row[0],
                name=row[1],
                version=row[2],
                stage=PromotionStage(row[3]),
                parameters=json.loads(row[4]),
                dataset_hash=row[5],
                created_at=datetime.fromisoformat(row[6]),
                updated_at=datetime.fromisoformat(row[7]),
            )

    def list_strategy_candidates(
        self, stage: PromotionStage | None = None
    ) -> list[StrategyCandidate]:
        with self._connect() as connection:
            if stage is not None:
                rows = connection.execute(
                    """
                    SELECT candidate_id, name, version, stage, parameters,
                           dataset_hash, created_at, updated_at
                    FROM strategy_candidates WHERE stage = ?
                    ORDER BY updated_at DESC
                    """,
                    (stage.value,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT candidate_id, name, version, stage, parameters,
                           dataset_hash, created_at, updated_at
                    FROM strategy_candidates
                    ORDER BY updated_at DESC
                    """
                ).fetchall()
            return [
                StrategyCandidate(
                    candidate_id=r[0],
                    name=r[1],
                    version=r[2],
                    stage=PromotionStage(r[3]),
                    parameters=json.loads(r[4]),
                    dataset_hash=r[5],
                    created_at=datetime.fromisoformat(r[6]),
                    updated_at=datetime.fromisoformat(r[7]),
                )
                for r in rows
            ]

    def record_shadow_run(
        self,
        candidate: StrategyCandidate,
        run_id: str,
        evaluated_until: datetime,
        payload: str,
    ) -> bool | None:
        """Return insertion status, or None when the candidate changed."""
        with self._connect() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            if self._execution_pause(connection).paused:
                raise PaperRejected("Execution is paused")
            row = connection.execute(
                "SELECT stage, updated_at FROM strategy_candidates "
                "WHERE candidate_id = ?",
                (candidate.candidate_id,),
            ).fetchone()
            if row != (
                PromotionStage.PAPER_SHADOW.value,
                candidate.updated_at.isoformat(),
            ):
                return None
            inserted = connection.execute(
                "INSERT INTO strategy_shadow_runs VALUES (?, ?, ?, ?) "
                "ON CONFLICT (candidate_id, run_id) DO NOTHING",
                (candidate.candidate_id, run_id, evaluated_until.isoformat(), payload),
            )
            if inserted.rowcount:
                connection.execute(
                    "INSERT INTO audit_events (event_id, recorded_at, kind) "
                    "VALUES (?, ?, ?)",
                    (
                        uuid4().hex,
                        datetime.now(UTC).isoformat(),
                        "strategy.shadow.recorded",
                    ),
                )
            return bool(inserted.rowcount)

    def latest_shadow_run(self, candidate_id: str) -> tuple[str, str] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT run_id, payload FROM strategy_shadow_runs "
                "WHERE candidate_id = ? "
                "ORDER BY evaluated_until DESC, run_id DESC LIMIT 1",
                (candidate_id,),
            ).fetchone()
        return (str(row[0]), str(row[1])) if row is not None else None

    def list_shadow_runs(
        self, candidate_id: str, limit: int = 25
    ) -> list[tuple[str, datetime, str]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT run_id, evaluated_until, payload FROM strategy_shadow_runs "
                "WHERE candidate_id = ? "
                "ORDER BY evaluated_until DESC, run_id DESC LIMIT ?",
                (candidate_id, max(1, min(limit, 200))),
            ).fetchall()
        return [(str(r[0]), datetime.fromisoformat(r[1]), str(r[2])) for r in rows]

    def update_candidate_stage(
        self,
        candidate: StrategyCandidate,
        new_stage: PromotionStage,
        evaluated_at: datetime,
        *,
        shadow_run_id: str | None = None,
    ) -> StrategyCandidate | None:
        with self._connect() as connection, connection:
            if new_stage is PromotionStage.LIVE_CANARY:
                connection.execute("BEGIN IMMEDIATE")
                if (
                    self._canary_approval(
                        connection, candidate.candidate_id, datetime.now(UTC)
                    )
                    is None
                ):
                    return None
            now = evaluated_at.isoformat()
            updated = connection.execute(
                "UPDATE strategy_candidates SET stage = ?, updated_at = ? "
                "WHERE candidate_id = ? AND stage = ? AND updated_at = ? "
                "AND (? IS NULL OR ? = ("
                "SELECT run_id FROM strategy_shadow_runs "
                "WHERE candidate_id = strategy_candidates.candidate_id "
                "ORDER BY evaluated_until DESC, run_id DESC LIMIT 1))",
                (
                    new_stage.value,
                    now,
                    candidate.candidate_id,
                    candidate.stage.value,
                    candidate.updated_at.isoformat(),
                    shadow_run_id,
                    shadow_run_id,
                ),
            )
            if updated.rowcount == 0:
                return None
            connection.execute(
                "INSERT INTO audit_events (event_id, recorded_at, kind) "
                "VALUES (?, ?, ?)",
                (uuid4().hex, now, f"strategy.promoted.{new_stage.value.lower()}"),
            )
        return candidate.model_copy(
            update={"stage": new_stage, "updated_at": evaluated_at}
        )

    def backup(self, target: Path) -> None:
        """Write a snapshot to a new destination using SQLite's online backup API."""
        # Exclusive creation keeps existing files and the source DB safe.
        with target.open("xb"):
            pass
        try:
            with (
                self._connect() as source,
                closing(sqlite3.connect(target)) as destination,
            ):
                source.backup(destination)
        except BaseException:
            target.unlink()
            raise
