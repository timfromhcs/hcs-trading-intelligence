"""Versioned local SQLite storage for audit events and quote history."""

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from pydantic import TypeAdapter

from hcs_trader.domain.assets import MarketId
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
from hcs_trader.domain.wallet import WalletSnapshot

_SCHEMA_VERSION = 11
_PAPER_FILL = TypeAdapter(PaperFill)
WALLET_SNAPSHOT = TypeAdapter(WalletSnapshot)


@dataclass(frozen=True, slots=True)
class AuditEvent:
    event_id: str
    recorded_at: str
    kind: str


@dataclass(frozen=True, slots=True)
class ResearchRecord:
    recorded_at: datetime
    payload: str


@dataclass(frozen=True, slots=True)
class ExecutionPause:
    paused: bool
    changed_at: datetime | None


class AuditStore:
    def __init__(self, path: Path, *, read_only: bool = False) -> None:
        self.path = path
        self._read_only = read_only

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        location = (
            self.path.resolve().as_uri() + "?mode=ro" if self._read_only else self.path
        )
        with closing(
            sqlite3.connect(location, timeout=5, uri=self._read_only)
        ) as connection:
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

    def set_execution_pause(self, paused: bool) -> ExecutionPause:
        with self._connect() as connection, connection:
            now = datetime.now(UTC).isoformat()
            updated = connection.execute(
                "UPDATE execution_pause SET paused = ?, changed_at = ? "
                "WHERE id = 1 AND paused <> ?",
                (int(paused), now, int(paused)),
            )
            if updated.rowcount:
                kind = "execution.paused" if paused else "execution.resumed"
                connection.execute(
                    "INSERT INTO audit_events (event_id, recorded_at, kind)"
                    " VALUES (?, ?, ?)",
                    (uuid4().hex, now, kind),
                )
            return self._execution_pause(connection)

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
