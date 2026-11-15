"""Versioned local SQLite storage for audit events and quote history."""

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from hcs_trader.domain.assets import MarketId
from hcs_trader.domain.market_data import Quote

_SCHEMA_VERSION = 2


@dataclass(frozen=True, slots=True)
class AuditEvent:
    event_id: str
    recorded_at: str
    kind: str


class AuditStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.path, timeout=5)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            yield connection

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
                connection.execute("PRAGMA user_version = 2")

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

    def record_startup(self) -> AuditEvent:
        event = AuditEvent(uuid4().hex, datetime.now(UTC).isoformat(), "app.started")
        with self._connect() as connection, connection:
            connection.execute(
                "INSERT INTO audit_events (event_id, recorded_at, kind)"
                " VALUES (?, ?, ?)",
                (event.event_id, event.recorded_at, event.kind),
            )
        return event

    def latest_event(self) -> AuditEvent | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT event_id, recorded_at, kind FROM audit_events"
                " ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
        return AuditEvent(*row) if row is not None else None

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
