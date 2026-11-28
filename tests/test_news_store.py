"""News provenance persistence: dedup, revisions, FTS5 search and migration."""

import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hcs_trader import storage
from hcs_trader.api import create_app
from hcs_trader.domain.news import NewsItem
from hcs_trader.storage import AuditStore

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _item(url: str, title: str, source: str = "fixture-feed", n: int = 0) -> NewsItem:
    return NewsItem(source, url, title, T0 + timedelta(minutes=n), T0)


def _store(path: Path) -> AuditStore:
    store = AuditStore(path)
    store.initialize()
    return store


def _rows(path: Path, sql: str) -> list[tuple[object, ...]]:
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute(sql).fetchall()  # noqa: S608


def test_tracking_parameters_collapse_to_one_article(tmp_path: Path) -> None:
    store = _store(tmp_path / "a.sqlite")
    first = store.record_news(
        [_item("https://example.org/a/?utm_source=x", "Rate cut")]
    )
    second = store.record_news(
        [_item("https://EXAMPLE.org/a?fbclid=1&ref=y", "Rate cut")]
    )
    assert (first, second) == (1, 0)
    assert len(store.search_news("rate", limit=10)) == 1
    hit = store.search_news("rate", limit=10)[0]
    assert hit.canonical_url == "https://example.org/a"
    assert hit.revision_count == 1


def test_changed_title_is_stored_as_revision(tmp_path: Path) -> None:
    store = _store(tmp_path / "a.sqlite")
    store.record_news([_item("https://example.org/a", "Fed holds rates")])
    changed = store.record_news(
        [_item("https://example.org/a?utm_x=1", "Fed cuts rates")]
    )
    assert changed == 1
    (hit,) = store.search_news("cuts", limit=5)
    assert hit.title == "Fed cuts rates" and hit.revision_count == 2
    assert store.search_news("holds", limit=5) == []  # FTS row followed the update
    revisions = store.news_revisions(hit.article_id)
    assert [title for _, title, _ in revisions] == [
        "Fed holds rates",
        "Fed cuts rates",
    ]


def test_dedup_state_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "a.sqlite"
    _store(path).record_news([_item("https://example.org/a", "Fed holds rates")])
    restarted = _store(path)  # fresh process state, same database
    assert (
        restarted.record_news([_item("https://example.org/a", "Fed holds rates")]) == 0
    )
    # Same story from another source shares the duplicate group but is kept.
    assert (
        restarted.record_news(
            [_item("https://other.example/x", "Fed holds rates!", source="other")]
        )
        == 1
    )
    hits = restarted.search_news("holds", limit=10)
    assert len(hits) == 2
    assert len({h.duplicate_group_id for h in hits}) == 1
    assert len({h.article_id for h in hits}) == 2
    # An update after restart is still a revision, not a second article.
    assert (
        restarted.record_news([_item("https://example.org/a", "Fed hikes rates")]) == 1
    )
    assert len(restarted.search_news("hikes", limit=10)) == 1


def test_snippet_is_searchable_and_sanitised(tmp_path: Path) -> None:
    store = _store(tmp_path / "a.sqlite")
    url = "https://example.org/a"
    store.record_news(
        [_item(url, "Headline")],
        snippets={url: "Bond <script>x()</script> auction results"},
    )
    (hit,) = store.search_news("auction", limit=5)
    assert hit.snippet == "Bond auction results"
    assert hit.envelope is not None
    assert hit.envelope.untrusted is True and hit.envelope.source == "fixture-feed"
    assert hit.source_reliability == "unverified"


def test_fts_fallback_without_fts_table(tmp_path: Path) -> None:
    path = tmp_path / "a.sqlite"
    store = _store(path)
    store.record_news([_item("https://example.org/a", "Rate decision")])
    with closing(sqlite3.connect(path)) as connection, connection:
        for trigger in ("insert", "update", "delete"):
            connection.execute(f"DROP TRIGGER news_fts_{trigger}")
        connection.execute("DROP TABLE news_fts")
    assert [h.title for h in store.search_news("RATE", limit=5)] == ["Rate decision"]
    assert store.record_news([_item("https://example.org/b", "Other news")]) == 1
    assert store.search_news("zzzz", limit=5) == []


def test_migration_without_fts5_support_still_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(AuditStore, "_fts5_supported", staticmethod(lambda _c: False))
    path = tmp_path / "a.sqlite"
    store = _store(path)
    assert _rows(path, "SELECT name FROM sqlite_master WHERE name = 'news_fts'") == []
    store.record_news([_item("https://example.org/a", "Rate decision")])
    assert len(store.search_news("rate", limit=5)) == 1


def _downgrade_to_legacy_news(path: Path, version: int) -> None:
    with closing(sqlite3.connect(path)) as connection, connection:
        for trigger in ("insert", "update", "delete"):
            connection.execute(f"DROP TRIGGER news_fts_{trigger}")
        connection.execute("DROP TABLE news_fts")
        connection.execute("DROP TABLE news_revisions")
        connection.execute("DROP TABLE news_items")
        connection.execute(
            """
            CREATE TABLE news_items (
                source TEXT NOT NULL, url TEXT NOT NULL, title TEXT NOT NULL,
                published_at TEXT NOT NULL, received_at TEXT NOT NULL,
                PRIMARY KEY (source, url)
            ) STRICT, WITHOUT ROWID
            """
        )
        connection.execute(
            "CREATE INDEX news_items_timeline ON news_items (published_at DESC)"
        )
        legacy = [
            ("fed", "https://example.org/a?utm_source=z", "Old headline", T0),
            ("fed", "https://example.org/a", "Old headline", T0),
            ("fed", "ftp://odd.example/x", "Odd scheme row", T0),
        ]
        for source, url, title, when in legacy:
            connection.execute(
                "INSERT INTO news_items VALUES (?, ?, ?, ?, ?)",
                (source, url, title, when.isoformat(), when.isoformat()),
            )
        connection.execute(f"PRAGMA user_version = {version}")


@pytest.mark.parametrize("legacy_version", [19, 20])
def test_migration_keeps_old_rows_and_backfills(
    tmp_path: Path, legacy_version: int
) -> None:
    path = tmp_path / "a.sqlite"
    _store(path)
    _downgrade_to_legacy_news(path, legacy_version)
    store = _store(path)
    with closing(sqlite3.connect(path)) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert version == storage._SCHEMA_VERSION  # noqa: SLF001
    titles = sorted(h.title for h in store.search_news("headline", limit=10))
    assert titles == ["Old headline"]  # tracking-URL twin collapsed, nothing lost
    assert [h.title for h in store.search_news("odd", limit=10)] == ["Odd scheme row"]
    (hit,) = store.search_news("headline", limit=10)
    assert hit.canonical_url == "https://example.org/a" and hit.revision_count == 1
    assert len(store.news(limit=10)) == 2
    # A later correction of a migrated row becomes a revision.
    assert (
        store.record_news([_item("https://example.org/a", "New headline", "fed")]) == 1
    )
    assert store.search_news("new", limit=5)[0].revision_count == 2


def test_rest_search_is_additive_with_envelope(tmp_path: Path) -> None:
    path = tmp_path / "a.sqlite"
    store = _store(path)
    store.record_news([_item("https://example.org/a", "Rate decision")])
    client = TestClient(create_app(store), base_url="http://127.0.0.1")
    (row,) = client.get("/api/v1/news/search", params={"q": "RATE"}).json()
    assert {"source", "url", "title", "published_at", "received_at"} <= row.keys()
    assert row["canonical_url"] == "https://example.org/a"
    assert row["envelope"]["untrusted"] is True
    assert row["envelope"]["trust_tier"] == "unverified"


def test_empty_title_after_sanitising_is_skipped_and_return_counts_changes(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path / "a.sqlite")
    items = [
        _item("https://example.org/empty", "<script>alert(1)</script>"),
        _item("https://example.org/ok", "Plain headline"),
        _item("https://example.org/ok?utm_source=x", "Plain headline"),
    ]
    # One insert; the markup-only title is skipped and the duplicate adds nothing.
    assert store.record_news(items) == 1
    assert [h.title for h in store.search_news("headline", limit=10)] == [
        "Plain headline"
    ]
    assert len(store.news(limit=10)) == 1
    assert store.record_news(items) == 0  # repeat is a no-op, not a cumulative count


def test_same_title_new_snippet_or_date_keeps_first_seen_values(
    tmp_path: Path,
) -> None:
    """Deliberate: only a title change is a revision; first-seen snippet stays."""
    store = _store(tmp_path / "a.sqlite")
    url = "https://example.org/a"
    store.record_news([_item(url, "Same title", n=0)], snippets={url: "first body"})
    later = NewsItem("fixture-feed", url, "Same title", T0 + timedelta(hours=5), T0)
    assert store.record_news([later], snippets={url: "second body"}) == 0
    (hit,) = store.search_news("same", limit=5)
    assert hit.revision_count == 1
    assert hit.snippet == "first body"
    assert hit.published_at == T0
    assert store.search_news("second", limit=5) == []


def test_migration_sanitises_legacy_titles_before_indexing(tmp_path: Path) -> None:
    path = tmp_path / "a.sqlite"
    _store(path)
    _downgrade_to_legacy_news(path, 20)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "INSERT INTO news_items VALUES (?, ?, ?, ?, ?)",
            (
                "fed",
                "https://example.org/m",
                "<b>Markup</b> headline<script>x()</script>",
                T0.isoformat(),
                T0.isoformat(),
            ),
        )
    store = _store(path)
    assert [h.title for h in store.search_news("markup", limit=5)] == [
        "Markup headline"
    ]
    assert store.search_news("script", limit=5) == []
    assert _rows(path, "SELECT title FROM news_fts WHERE title LIKE '%<%'") == []


def test_migration_dedup_keeps_earliest_received_at(tmp_path: Path) -> None:
    path = tmp_path / "a.sqlite"
    _store(path)
    _downgrade_to_legacy_news(path, 20)
    early, late = T0 - timedelta(days=2), T0 + timedelta(days=2)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("DELETE FROM news_items")
        for url, when in (
            ("https://example.org/d", late),  # sorts first by key, received last
            ("https://example.org/d?utm_source=q", early),
        ):
            connection.execute(
                "INSERT INTO news_items VALUES (?, ?, ?, ?, ?)",
                ("fed", url, "Dup headline", T0.isoformat(), when.isoformat()),
            )
    _store(path)
    rows = _rows(path, "SELECT received_at FROM news_items")
    assert len(rows) == 1
    assert datetime.fromisoformat(str(rows[0][0])) == early
