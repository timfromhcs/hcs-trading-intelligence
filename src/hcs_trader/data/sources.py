"""Typed source registry (AGENTS.md 4.4). Static defaults; no secrets stored."""

from dataclasses import dataclass
from enum import StrEnum

from hcs_trader.data.resilience import RetryPolicy


class SourceKind(StrEnum):
    MARKET = "market"
    NEWS = "news"
    MACRO = "macro"


@dataclass(frozen=True, slots=True)
class SourceEntry:
    source_id: str
    kind: SourceKind
    owner: str
    url: str
    format: str
    language: str
    requires_api_key: bool
    quota_note: str
    terms_url: str
    cadence_seconds: int
    stale_after_seconds: int
    retry: RetryPolicy
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.source_id or not self.url.startswith("https://"):
            raise ValueError("Sources need an id and an https URL")
        if not self.terms_url.startswith("https://"):
            raise ValueError("Sources need an https terms URL")
        if self.cadence_seconds <= 0 or self.stale_after_seconds < self.cadence_seconds:
            raise ValueError("Stale threshold must be at least one cadence")


class SourceRegistry:
    def __init__(self, entries: tuple[SourceEntry, ...]) -> None:
        ids = [e.source_id for e in entries]
        if len(set(ids)) != len(ids):
            raise ValueError("Duplicate source id")
        self._entries = {e.source_id: e for e in entries}

    def get(self, source_id: str) -> SourceEntry:
        return self._entries[source_id]

    def all(self) -> tuple[SourceEntry, ...]:
        return tuple(self._entries.values())

    def enabled(self) -> tuple[SourceEntry, ...]:
        return tuple(e for e in self._entries.values() if e.enabled)

    def with_enabled(self, source_id: str, enabled: bool) -> "SourceRegistry":
        from dataclasses import replace

        return SourceRegistry(
            tuple(
                replace(e, enabled=enabled) if e.source_id == source_id else e
                for e in self._entries.values()
            )
        )


DEFAULT_REGISTRY = SourceRegistry(
    (
        SourceEntry(
            "kraken",
            SourceKind.MARKET,
            "Payward, Inc. (Kraken)",
            "https://api.kraken.com/0/public/",
            "json",
            "en",
            False,
            "Public REST limits apply; check current Kraken documentation",
            "https://www.kraken.com/legal",
            5,
            30,
            RetryPolicy(max_attempts=3, base_delay=1.0, max_delay=30.0),
        ),
        SourceEntry(
            "federal-reserve-monetary-policy",
            SourceKind.NEWS,
            "Board of Governors of the Federal Reserve System",
            "https://www.federalreserve.gov/feeds/press_monetary.xml",
            "rss",
            "en",
            False,
            "No published quota; poll sparingly",
            "https://www.federalreserve.gov/disclaimer.htm",
            900,
            86_400,
            RetryPolicy(max_attempts=3, base_delay=2.0, max_delay=60.0),
        ),
        SourceEntry(
            "ecb-sdmx",
            SourceKind.MACRO,
            "European Central Bank",
            "https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A",
            "sdmx-csv",
            "en",
            False,
            "Daily reference rates; no published hard quota",
            "https://www.ecb.europa.eu/services/disclaimer/html/index.en.html",
            3600,
            172_800,
            RetryPolicy(max_attempts=3, base_delay=2.0, max_delay=60.0),
        ),
        SourceEntry(
            "fred",
            SourceKind.MACRO,
            "Federal Reserve Bank of St. Louis",
            "https://api.stlouisfed.org/fred/series/observations",
            "json",
            "en",
            True,
            "Free personal API key required; rate limited",
            "https://fred.stlouisfed.org/docs/api/terms_of_use.html",
            3600,
            172_800,
            RetryPolicy(max_attempts=3, base_delay=2.0, max_delay=60.0),
            enabled=False,
        ),
    )
)
