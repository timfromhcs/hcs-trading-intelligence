"""Versioned registry of public news, macro and status sources.

Nothing in this module performs network I/O. A source is usable only when its
verification record says ``verified`` and an operator has explicitly enabled it;
the shipped registry therefore enables nothing. Keys are never stored here,
only the fact that a key would be required.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

_DEFAULT_PATH = Path(__file__).with_name("sources.json")
_MAX_RATE_PER_MINUTE = 60


class _Verification(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    endpoint: str = Field(min_length=1, max_length=500)
    checked_at: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    result: Literal["verified", "pending", "failed"]
    fixture_ref: str = Field(min_length=1, max_length=200)
    robots: Literal["allowed", "disallowed", "pending"]
    note: str = Field(default="", max_length=300)


class _Source(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,63}$")
    url: str = Field(min_length=1, max_length=500)
    format: Literal["rss", "atom", "json", "csv", "sdmx", "html"]
    language: str = Field(pattern=r"^[a-z]{2}(-[A-Z]{2})?$")
    api_key_required: bool
    enabled: bool
    terms_url: str = Field(min_length=1, max_length=500)
    attribution: str = Field(min_length=1, max_length=300)
    requests_per_minute: int = Field(ge=1, le=_MAX_RATE_PER_MINUTE)
    verification: _Verification


class _Document(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    version: int = Field(ge=1)
    updated_at: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    sources: list[_Source] = Field(min_length=1, max_length=200)


@dataclass(frozen=True, slots=True)
class SourceSpec:
    id: str
    url: str
    format: str
    language: str
    api_key_required: bool
    enabled: bool
    terms_url: str
    attribution: str
    requests_per_minute: int
    verification: _Verification

    @property
    def usable(self) -> bool:
        return (
            self.enabled
            and self.verification.result == "verified"
            and self.verification.robots == "allowed"
        )


class SourceRegistry:
    """Immutable, validated view of the shipped source registry."""

    def __init__(self, specs: tuple[SourceSpec, ...], version: int, updated_at: str):
        identifiers = [spec.id for spec in specs]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("Duplicate source identifier")
        self._specs = {spec.id: spec for spec in specs}
        self.version = version
        self.updated_at = updated_at

    def all(self) -> tuple[SourceSpec, ...]:
        return tuple(self._specs.values())

    def get(self, source_id: str) -> SourceSpec:
        try:
            return self._specs[source_id]
        except KeyError:
            raise ValueError("Source is not registered") from None

    def enabled(self) -> tuple[SourceSpec, ...]:
        return tuple(spec for spec in self._specs.values() if spec.usable)

    def requiring_key(self) -> tuple[SourceSpec, ...]:
        return tuple(spec for spec in self._specs.values() if spec.api_key_required)


def load_source_registry(path: Path | None = None) -> SourceRegistry:
    """Load and strictly validate a registry from disk, without network."""
    document = json.loads((path or _DEFAULT_PATH).read_text(encoding="utf-8"))
    parsed = _Document.model_validate(document)
    specs = tuple(
        SourceSpec(
            id=source.id,
            url=source.url,
            format=source.format,
            language=source.language,
            api_key_required=source.api_key_required,
            enabled=source.enabled,
            terms_url=source.terms_url,
            attribution=source.attribution,
            requests_per_minute=source.requests_per_minute,
            verification=source.verification,
        )
        for source in parsed.sources
    )
    return SourceRegistry(specs, parsed.version, parsed.updated_at)


@dataclass(frozen=True, slots=True)
class ConditionalCache:
    etag: str | None = None
    last_modified: str | None = None
    fetched_at: datetime | None = None


def conditional_headers(cache: ConditionalCache | None) -> dict[str, str]:
    """Build conditional GET headers; never send credentials or tokens."""
    if cache is None:
        return {"Accept-Encoding": "identity"}
    result = {"Accept-Encoding": "identity"}
    if cache.etag:
        result["If-None-Match"] = cache.etag[:256]
    if cache.last_modified:
        result["If-Modified-Since"] = cache.last_modified[:128]
    return result


def cache_from_headers(
    headers: Mapping[str, str], *, now: datetime | None = None
) -> ConditionalCache:
    """Retain only the cache validators a server actually returned."""
    fetched_at = now or datetime.now(UTC)
    if fetched_at.tzinfo is None:
        raise ValueError("Cache timestamp must be timezone-aware")
    return ConditionalCache(
        etag=headers.get("etag"),
        last_modified=headers.get("last-modified"),
        fetched_at=fetched_at,
    )


@dataclass
class RequestBudget:
    """Deterministic per-source request budget with an injected clock."""

    interval_seconds: float
    now: datetime

    def __post_init__(self) -> None:
        if self.interval_seconds <= 0:
            raise ValueError("Request interval must be positive")
        self._next_allowed = self.now

    def allow(self, now: datetime | None = None) -> bool:
        current = now or self.now
        if current < self._next_allowed:
            return False
        from datetime import timedelta

        self._next_allowed = current + timedelta(seconds=self.interval_seconds)
        return True

    def next_allowed(self) -> datetime:
        return self._next_allowed
