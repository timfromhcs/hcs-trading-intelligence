"""FTS search over hostile news returns inert data only."""

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from corpus.news_injection import HOSTILE_TITLES

from hcs_trader.domain.news import NewsItem
from hcs_trader.storage import AuditStore

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _store(path: Path) -> AuditStore:
    store = AuditStore(path)
    store.initialize()
    return store


def _rows(path: Path, sql: str) -> list[tuple[object, ...]]:
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute(sql).fetchall()  # noqa: S608


def test_injection_corpus_returns_data_only(tmp_path: Path) -> None:
    path = tmp_path / "a.sqlite"
    store = _store(path)
    now = T0
    store.record_news(
        [
            NewsItem("evil-feed", f"https://evil.test/{i}", title, now, now)
            for i, title in enumerate(HOSTILE_TITLES.values())
        ]
    )
    before = _rows(path, "SELECT COUNT(*) FROM audit_events")
    paused = store.execution_pause()
    hits = store.search_news("instructions", limit=100)
    assert hits, "corpus should contain a searchable hostile title"
    for hit in store.search_news("evil", limit=100):
        assert "<" not in hit.title and ">" not in hit.title
        assert hit.envelope is not None and hit.envelope.untrusted
    # FTS syntax in the query is inert: quoted tokens, never operators.
    for query in (
        '" OR 1=1 --',
        "title:*",
        "NEAR(a b)",
        "a AND NOT b",
        "'; DROP TABLE x",
    ):
        store.search_news(query, limit=5)
    assert _rows(path, "SELECT COUNT(*) FROM audit_events") == before
    assert store.execution_pause() == paused
