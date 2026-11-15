"""Versioned local SQLite storage for application audit events."""

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

_SCHEMA_VERSION = 1


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
            if version == _SCHEMA_VERSION:
                return
            if version != 0:
                raise RuntimeError("Unsupported audit database schema")
            connection.execute(
                """
                CREATE TABLE audit_events (
                    event_id TEXT PRIMARY KEY NOT NULL,
                    recorded_at TEXT NOT NULL,
                    kind TEXT NOT NULL
                ) STRICT
                """
            )
            connection.execute("PRAGMA user_version = 1")

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
