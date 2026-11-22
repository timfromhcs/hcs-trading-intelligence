"""News provenance fields, URL canonicalisation and cross-source de-duplication."""

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from hcs_trader.domain.news import NewsItem

_TRACKING = re.compile(r"^(utm_.*|fbclid|gclid|mc_cid|mc_eid|ref|ref_src)$")


def canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise ValueError("Only http(s) URLs can be canonicalised")
    host = parts.hostname.lower()
    port = parts.port
    default = {"http": 80, "https": 443}[parts.scheme.lower()]
    netloc = host if port in (None, default) else f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parts.path) or "/"
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    query = urlencode(
        sorted(
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not _TRACKING.match(k.lower())
        )
    )
    return urlunsplit((parts.scheme.lower(), netloc, path, query, ""))


def _normal_title(title: str) -> str:
    return re.sub(r"[^\w]+", " ", title.casefold()).strip()


def content_hash(title: str) -> str:
    return hashlib.sha256(_normal_title(title).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ProvenancedNews:
    article_id: str
    source_id: str
    canonical_url: str
    title: str
    published_at: datetime
    retrieved_at: datetime
    language: str
    region: str | None
    content_hash: str
    duplicate_group_id: str
    entities: tuple[str, ...] = ()
    topics: tuple[str, ...] = ()
    event_type: str | None = None
    raw_payload_reference: str | None = None


def source_tier(source_id: str) -> str:
    """Trust tier for a source: registered in the source registry or unverified."""
    from hcs_trader.data.sources import DEFAULT_REGISTRY

    try:
        DEFAULT_REGISTRY.get(source_id)
    except KeyError:
        return "unverified"
    return "registered"


def source_language(source_id: str, default: str = "en") -> str:
    from hcs_trader.data.sources import DEFAULT_REGISTRY

    try:
        return DEFAULT_REGISTRY.get(source_id).language
    except KeyError:
        return default


def enrich(
    item: NewsItem,
    *,
    strict: bool = True,
    language: str = "en",
    region: str | None = None,
    entities: tuple[str, ...] = (),
    topics: tuple[str, ...] = (),
    event_type: str | None = None,
    raw_payload_reference: str | None = None,
) -> ProvenancedNews:
    try:
        canonical = canonical_url(item.url)
    except ValueError:
        if strict:
            raise
        canonical = item.url.strip()  # stored as-is; never fetched
    digest = content_hash(item.title)
    return ProvenancedNews(
        hashlib.sha256(f"{item.source}\n{canonical}".encode()).hexdigest(),
        item.source,
        canonical,
        item.title,
        item.published_at,
        item.received_at,
        language,
        region,
        digest,
        digest[:32],
        entities,
        topics,
        event_type,
        raw_payload_reference,
    )


class IngestOutcome(StrEnum):
    NEW = "NEW"
    UNCHANGED = "UNCHANGED"
    UPDATED = "UPDATED"  # same article, changed title: revision recorded
    DUPLICATE = "DUPLICATE"  # same story from another source or URL


@dataclass(frozen=True, slots=True)
class Revision:
    title: str
    content_hash: str
    retrieved_at: datetime


@dataclass(slots=True)
class NewsDeduper:
    """In-memory dedup/history; persistence belongs to a storage adapter port."""

    _articles: dict[str, ProvenancedNews] = field(default_factory=dict)
    _history: dict[str, list[Revision]] = field(default_factory=dict)
    _groups: dict[str, str] = field(default_factory=dict)  # hash -> first article

    def ingest(self, item: ProvenancedNews) -> IngestOutcome:
        existing = self._articles.get(item.article_id)
        if existing is not None:
            if existing.content_hash == item.content_hash:
                return IngestOutcome.UNCHANGED
            self._history[item.article_id].append(
                Revision(item.title, item.content_hash, item.retrieved_at)
            )
            self._articles[item.article_id] = item
            return IngestOutcome.UPDATED
        self._articles[item.article_id] = item
        self._history[item.article_id] = [
            Revision(item.title, item.content_hash, item.retrieved_at)
        ]
        if item.content_hash in self._groups:
            return IngestOutcome.DUPLICATE
        self._groups[item.content_hash] = item.article_id
        return IngestOutcome.NEW

    def history(self, article_id: str) -> tuple[Revision, ...]:
        return tuple(self._history.get(article_id, ()))

    def group(self, duplicate_group_id: str) -> tuple[ProvenancedNews, ...]:
        return tuple(
            a
            for a in self._articles.values()
            if a.duplicate_group_id == duplicate_group_id
        )
