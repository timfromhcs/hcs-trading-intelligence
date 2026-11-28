# Data sources

## Source registry

Every source has a registry row. A source marked Planned has no adapter, and its quota and terms are stated from the provider's public documentation and must be rechecked before integration. Provider limits change. Public access does not grant redistribution rights. A planned REST endpoint `/api/v1/system/sources` and a Settings panel will surface this registry in the application ([API reference](API.md)).

| Source ID | Owner | Format | Language | Key | Quota and cadence | Terms | Retry policy | Status |
|---|---|---|---|---|---|---|---|---|
| `kraken-spot` | Kraken | REST JSON | n/a | None for public data | About 1 request per second per IP. Refresh at most every 5 s | [Legal](https://www.kraken.com/legal) | Exponential backoff, at least 60 s after a rate limit | Implemented, off unless `--public-market-data` |
| `federal-reserve-monetary-policy` | Federal Reserve Board | RSS XML | English | None | One request per manual import, releases at irregular intervals | [Feeds](https://www.federalreserve.gov/feeds/feeds.htm) | None, import is manual | Implemented |
| `ecb-sdmx` | European Central Bank | SDMX JSON | English | None | One manual import, up to 250 daily values, published once per working day | [ECB data](https://data.ecb.europa.eu/help/api/data) | None, import is manual | Implemented |
| `fred` | Federal Reserve Bank of St. Louis | REST JSON | English | Yes, your own | One manual import, up to 250 values | [Terms](https://fred.stlouisfed.org/docs/api/terms_of_use.html) | None, import is manual | Implemented |
| `coingecko` | CoinGecko | REST JSON | English | Free demo key | Demo plan has a per-minute and monthly cap. Check current plan | [API](https://www.coingecko.com/en/api) | Planned: backoff with jitter | Planned, no adapter |
| `binance-public` | Binance | REST and WebSocket | English | None for market data | Weighted per-IP limits. Availability varies by jurisdiction | [Docs](https://github.com/binance/binance-spot-api-docs) | Planned: circuit breaker | Planned, no adapter |
| `gdelt-doc` | GDELT Project | REST JSON | Multilingual | None | Rate-limited, near real-time | [GDELT](https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/) | Planned | Planned, no adapter. Discovery only, not verified truth |
| `official-rss` (ECB, Bundesbank, US Treasury, SEC, ESMA, European Commission, Eurostat, BaFin) | Each publisher | RSS and Atom | Mostly English or German | None | Per-feed cadence | Per publisher | Planned | Planned, no adapters |

All sources are disabled by default. Feed text is untrusted input. Do not scrape paywalled pages or bypass robots rules.

## Ingestion runtime

- Background polling is opt-in: `hcs-trader serve --ingest-feeds`. The default stays off, so tests and the default PAPER setup make no feed requests. The scheduler runs inside the FastAPI lifespan and is cancelled on shutdown. It polls only the existing Federal Reserve and ECB providers. Mode is `PAPER` with `--paper-market-data`, otherwise `DATA_ONLY`. In `OFFLINE` mode the scheduler never polls. The HTTP client enforces the network allowlist.
- Per-source state lives in the `ingest_state` table of the audit database (schema version 20): last success, last error, newest item time, consecutive failures, a `Retry-After` deadline and a circuit breaker snapshot. A restart restores the deadline and the open breaker, so a rate-limited provider is not called again early. A 429 or 503 response is never retried in-process. Its pause is the `Retry-After` value, or 60 seconds when absent.
- A sink or storage failure (for example a locked database) is recorded as the source error, backs the source off exponentially up to one hour and never ends the loop.
- `QuoteService` uses the same `CircuitBreaker` and persists its state under source ID `kraken`, including the rate-limit cooldown.
- Freshness uses `assess_feed`. `GET /api/v1/data/sources`, the MCP tool `data.get_source_status` and the Overview panel read one `SourceStatus` model. Sources with an API key requirement stay disabled.

## Kraken Spot REST

- Source ID `kraken-spot`, operated by Kraken. The adapter requests top-of-book BTC/USD as JSON from the public Depth endpoint. [Endpoint documentation](https://docs.kraken.com/api-reference/market-data/get-order-book)
- Public access requires no API key. Kraken describes one request per second or less as within its public REST limit, with Depth limited by IP address. This application permits an on-demand refresh at most once every five seconds per running instance, applies bounded exponential backoff after failures and waits at least 60 seconds after a rate-limit response. [API limits](https://support.kraken.com/articles/206548367-what-are-the-api-rate-limits-)
- Quote quality uses the oldest side update and local UTC receipt. The last valid quote is marked degraded or stale during an outage, including after restart. Older updates cannot overwrite newer ones. Prices are returned as decimal strings.
- Distinct observed price levels and timestamps are persisted in local SQLite. The history endpoint reads up to 500 rows without network access and returns the most recently stored observations first. Repeated deliveries of the same quote retain the first local receipt.
- Disabled by default. Local usage is subject to the applicable Kraken terms. Public accessibility does not grant redistribution rights. [Terms by region](https://www.kraken.com/legal)
- Market registry (`hcs_trader.domain.markets`). Kraken pairs seeded and checked against the public `AssetPairs` endpoint on 2026-11-22: BTC, ETH, SOL and LTC against USD and EUR (request pairs `XBTUSD`, `ETHUSD`, `SOLUSD`, `LTCUSD` and the EUR equivalents). Kraken names the result key differently from the request (`XBTUSD` is answered as `XXBTZUSD`, `ETHUSD` as `XETHZUSD`, `LTCUSD` as `XLTCZUSD`, while `SOLUSD` is echoed), so the adapter reads the single pair key from the response and accepts it only if it is registered for the requested market. DGB has no Kraken pair and is registered read-only with no market; an aggregator source is planned for Wave 2 and is not available. Only BTC/USD is `live_allowed`; the other markets are `read_only` because paper execution is still BTC/USD only. Precision and minimum sizes come from the same endpoint and are not a promise that Kraken keeps them.
- The explicit `import-candles` command (`--market` selects a registry market, default BTC/USD) fetches up to 720 completed candles for the market given by `--market` (default BTC/USD) per call from Kraken OHLC. The interval is in minutes. The import keeps the first stored observation for each source, interval and opening time. The local candles API serves the latest requested rows in chronological order, with gaps preserved. [OHLC documentation](https://docs.kraken.com/api-reference/market-data/get-ohlc-data)

## Federal Reserve monetary-policy releases

- Source ID `federal-reserve-monetary-policy`, operated by the Board of Governors of the Federal Reserve System. The public RSS feed requires no API key. [Official RSS directory](https://www.federalreserve.gov/feeds/feeds.htm)
- `import-news` requests the feed once, without following redirects. Only monetary-policy release URLs on the official host are kept. The application stores plain title text, URL, publication time and first local receipt. Article bodies and feed descriptions are omitted.
- News storage (schema 21) keeps provenance per article: canonical URL (tracking parameters removed), content hash, duplicate group, language, source trust tier (`registered` when the source is in the registry, else `unverified`) and an optional raw payload reference. A changed title for the same article is stored as a new row in `news_revisions`. Titles and snippets pass through `news_safety.sanitize_text` before storage and again on output.
- Revision rules: only a changed title creates a revision. A repeat with the same title but a new snippet or publication time keeps the first-seen snippet and `published_at` (first-seen provenance is stable). `record_news` returns the number of rows inserted or revised and skips items whose title is empty after sanitising.
- The schema 21 migration sanitises legacy titles before hashing and indexing them (an empty result becomes `(untitled)`). When two legacy URLs collapse onto one article, the row with the earliest `received_at` is kept and the other row is deleted, so its own `received_at` and URL variant are lost.
- `news_fts` is an FTS5 index over title and snippet kept in step by triggers. If the SQLite build has no FTS5, the migration skips it and search falls back to a bounded substring scan over the latest 1000 items. Verified on Windows CPython 3.12 (SQLite 3.53.1) only; macOS and Linux builds were not checked here. A database created with FTS5 and then opened by a SQLite without it will fail writes until the `news_fts*` triggers are dropped.
- `GET /api/v1/news/events` reads locally stored source metadata in reverse publication order. Feed text is untrusted input.

## European Central Bank reference rates

- Source ID `ecb-sdmx`, series `EXR.D.USD.EUR.SP00.A`. Values are US dollars per euro. The public SDMX API requires no key. A single manual import fetches up to 250 daily observations. [ECB data API](https://data.ecb.europa.eu/help/api/data)
- Reference rates are economic observations. Trading venue quotes are supplied separately. The observation date describes the date of the reference rate, while `received_at` records when this installation first saw that value or revision. Initial downloads supply the currently published history. Historical publication times and complete historical vintages are outside this import's coverage. [ECB methodology](https://data.ecb.europa.eu/methodology/exchange-rates)

## FRED US consumer price index

- Source ID `fred`, series `CPIAUCSL`. The series is the seasonally adjusted index for all urban consumers. Each manual import requests up to 250 observations. FRED requires a registered API key. The CLI accepts it at a hidden terminal prompt for a single request. [Series](https://fred.stlouisfed.org/series/CPIAUCSL) · [FRED API terms](https://fred.stlouisfed.org/docs/api/terms_of_use.html)
- An optional `--as-of` date selects a single FRED real-time snapshot. The API preserves the observation date, source snapshot date and local receipt time separately. A snapshot date describes the selected real-time period. Treat observation dates as reference periods when aligning macro data to decisions. [FRED observations](https://fred.stlouisfed.org/docs/api/fred/series_observations.html)
