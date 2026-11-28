"""Tests for the versioned, disabled-by-default source registry."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from hcs_trader.data.source_registry import (
    ConditionalCache,
    RequestBudget,
    SourceRegistry,
    cache_from_headers,
    conditional_headers,
    load_source_registry,
)


def _registry(tmp_path: Path, document: dict[str, object]) -> SourceRegistry:
    path = tmp_path / "sources.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return load_source_registry(path)


def _source(**overrides: object) -> dict[str, object]:
    source: dict[str, object] = {
        "id": "sample-source",
        "url": "https://example.org/feed.xml",
        "format": "rss",
        "language": "en",
        "api_key_required": False,
        "enabled": True,
        "terms_url": "https://example.org/terms",
        "attribution": "Example Organisation",
        "requests_per_minute": 10,
        "verification": {
            "endpoint": "Example feed",
            "checked_at": "2026-11-27",
            "result": "verified",
            "fixture_ref": "fixtures/sources/sample.xml",
            "robots": "allowed",
            "note": "test fixture",
        },
    }
    source.update(overrides)
    return source


def _document(*sources: dict[str, object]) -> dict[str, object]:
    return {"version": 1, "updated_at": "2026-11-27", "sources": list(sources)}


def test_shipped_registry_is_valid_and_enables_nothing() -> None:
    registry = load_source_registry()
    assert registry.version >= 1
    assert len(registry.all()) >= 12
    assert registry.enabled() == ()
    assert {source.id for source in registry.all()} == {
        source.id for source in registry.all()
    }
    for source in registry.all():
        assert source.attribution
        assert source.terms_url.startswith("https://")
        assert source.verification.fixture_ref
        assert not source.usable


def test_key_required_sources_are_listed_without_secrets(tmp_path: Path) -> None:
    registry = _registry(
        tmp_path,
        _document(_source(id="keyed", api_key_required=True)),
    )
    assert [source.id for source in registry.requiring_key()] == ["keyed"]
    serialized = str(registry.all()).lower()
    assert "api_key_required=true" in serialized
    assert "api_key=" not in serialized  # no key value is ever stored


def test_unknown_field_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        _registry(tmp_path, _document(_source(secret="must-not-be-accepted")))


def test_verification_gates_usability(tmp_path: Path) -> None:
    pending = _registry(
        tmp_path,
        _document(
            _source(
                verification={
                    "endpoint": "Example feed",
                    "checked_at": "2026-11-27",
                    "result": "pending",
                    "fixture_ref": "fixtures/sources/sample.xml",
                    "robots": "pending",
                    "note": "",
                }
            )
        ),
    )
    assert pending.enabled() == ()
    verified = _registry(tmp_path, _document(_source()))
    assert [source.id for source in verified.enabled()] == ["sample-source"]


def test_duplicate_identifiers_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Duplicate"):
        _registry(tmp_path, _document(_source(), _source()))


def test_conditional_headers_are_identity_and_validator_only() -> None:
    assert conditional_headers(None) == {"Accept-Encoding": "identity"}
    cache = ConditionalCache(
        etag='"abc"', last_modified="Wed, 27 Nov 2026 12:00:00 GMT"
    )
    headers = conditional_headers(cache)
    assert headers["If-None-Match"] == '"abc"'
    assert headers["If-Modified-Since"].startswith("Wed, 27")
    assert "Authorization" not in headers


def test_cache_from_headers_retains_validators() -> None:
    now = datetime(2026, 11, 27, tzinfo=UTC)
    cache = cache_from_headers(
        {
            "etag": '"v1"',
            "last-modified": "Wed, 27 Nov 2026 12:00:00 GMT",
            "set-cookie": "x",
        },
        now=now,
    )
    assert cache.etag == '"v1"'
    assert cache.fetched_at == now


def test_request_budget_enforces_minimum_interval() -> None:
    start = datetime(2026, 11, 27, tzinfo=UTC)
    budget = RequestBudget(6.0, start)
    assert budget.allow(start)
    assert not budget.allow(start + timedelta(seconds=5))
    assert budget.allow(start + timedelta(seconds=6))
    assert budget.next_allowed() == start + timedelta(seconds=12)
