# Build and test waves

Planned 2026-11-22 against `main` at `73fb202` (v1 release candidate merged, CI green on Windows, macOS and Linux). This plan comes from five read-only code audits and eight web research tracks. Claims about external services are research findings from a single German IP on that date. Each source stays disabled until its verification record is added to [DATA_SOURCES.md](DATA_SOURCES.md).

## Goal of the next stable wave

Everything runs locally with internet access. All market, news and macro data comes from free keyless APIs, free-key APIs that the operator may add, or polite keyless web fetch. API, MCP, Skills, the local model and the UI become measurably better. Spot only. Paper by default. No real funds in any loop.

## Principles

1. **Wire before you add.** The audit found the ingestion scheduler, source registry, circuit breaker, feed status, cross-check, news provenance, System 1 loop and agent registry used only by tests. No new provider ships before the existing loop runs in the app lifespan with an end-to-end restart test.
2. **Keyless first, key optional.** The core data path works with zero keys. CoinGecko Demo, FRED JSON and similar are bring-your-own-key plugins that show `key required` in the UI instead of failing.
3. **Every source is disabled until verified.** A verification record holds endpoint, date, key need, rate limit, terms URL, robots result, attribution text and a recorded fixture. No hard-coded quotas.
4. **New assets are read-only or PAPER.** Live Kraken spot stays BTC/USD only and disabled by default. The refactor away from the `BTC_USD` constant touches risk, execution and promotion code, so the AGENTS.md section 13.2 invariant tests run before and after and no limit is loosened.
5. **Untrusted text never chooses an action.** MCP fetch tools take a registered `source_id` only, with server-side fixed paths. Tool output carries an untrusted-content envelope. An injection corpus runs against an instrumented sink.
6. **New dependencies start as optional extras** until the onedir and onefile builds, the license inventory and `selftest` pass on all three systems.
7. **Quorum rules are written down.** Cross-venue checks need independence: aggregators do not count as independent venues. USDT, USD and EUR quotes need an explicit conversion and depeg check. DGB is labelled low-liquidity and read-only. Disagreeing or stale data resolves to `NO_TRADE`.

## Wave template (copy per wave)

- **Entry gate:** `main` green on three systems. Previous retro done. `KNOWN_LIMITATIONS.md` and this file updated. At most five tracks, acceptance tests written first. Dependabot queue triaged. Baseline metrics recorded: test count, coverage per module, exe size, startup time.
- **Build:** one isolated worktree per track, branched from the current local `main` commit, not from an older remote base. Tracks touch disjoint files where possible. Shared files get minimal additive edits.
- **Review:** one adversarial reviewer per track before integration. Defects fixed on the integration branch.
- **Test gate:** PR-fast tier on three systems; nightly tier green on the release commit; per-module coverage floors; invariants 13.2 mapped to named tests; selftest on onedir and onefile; no new `skip`, `xfail` or weakened check.
- **Exit gate:** CI green, `release-check` evidence updated, docs claims match tests, rollback tag recorded.
- **Rollback:** revert the wave merge commit. Database migrations are forward-only, so each migration ships with a backup-and-restore test.

## Test tiers

| Tier | Trigger | Content |
|---|---|---|
| PR-fast (under 10 min) | every PR | ruff, mypy, unit, Hypothesis `ci` profile (derandomized), offline cassette contract tests, frontend lint/tsc/build, secret scan, onedir selftest |
| Nightly | cron on `main` | full pytest on three systems, Hypothesis `nightly` profile, replay and soak, live-provider drift probe (opens an issue, never blocks PRs), packaging smoke with Unicode paths and offline start |
| Weekly | cron | mutation testing (mutmut on Linux) scoped to `risk/` and signing policy checks, chaos set, feed canary, dependency audit |
| Release | tag | re-run of all tiers on the exact commit, attestation, SBOM, checksums, upgrade from the previous wheel |

## Wave 1: Wire and harden (foundation)

| Track | Work | Acceptance |
|---|---|---|
| W1-A Ingestion runtime | Run `IngestionScheduler` in the app lifespan. Move ingest state into the audit database by migration. Catch storage errors without killing the loop. Persist `Retry-After`. Unify `QuoteService` and the scheduler on `CircuitBreaker`. Expose source status in REST, MCP and UI | restart test: a 429 deadline survives restart; a sink failure keeps the loop alive; freshness visible in the API |
| W1-B Market registry | `MarketRegistry` maps asset to venue symbol, precision, minimum size and quote allowlist. The Kraken adapter takes a market parameter and resolves the response key from the response. BTC, ETH, SOL, LTC paper and read-only. DGB read-only through an aggregator | no `BTC_USD` import outside a default constant; live submit still BTC/USD only; 13.2 invariants unchanged |
| W1-C News provenance | Migration for canonical URL, content hash, language, duplicate group, revisions. Persist deduplication state. FTS5 index over titles and licensed snippets | corrections kept as revisions; tracking-parameter URLs collapse; restart keeps dedup state |
| W1-D Loopback hardening | Host and Origin allowlist, `Sec-Fetch-Site` check, CSP, JSON content type on writes, Host validation on MCP HTTP. Confirm the locked `mcp` version matches the `pyproject.toml` pin | DNS-rebinding test fails closed; CI resolves the pinned SDK |
| W1-E CI tiers | Nightly and weekly workflows, Hypothesis profiles, per-module coverage floors (90% for risk, signing, execution, domain), cassette tooling with network blocked by default, 13.2 invariant map. Triage Dependabot PR #3 (TypeScript 7) behind a green frontend build | floors measured first, then enforced; no gate weakened |

**W1-B status note.** Implemented: registry, per-market Kraken quotes, candles and order book (BTC, ETH, SOL, LTC against USD and EUR, read-only for all but BTC/USD), DGB registered read-only with no pair. Paper execution and live submission stay BTC/USD only; paper support for ETH, SOL and LTC is deferred. The live Kraken shape check now also enforces registry precision and minimum size (`validate_order_shape`), additive to the existing bounds. Quote archiving is BTC/USD only, so other markets return archived data only if candles or quotes were imported.

## Wave 2: Free data breadth

- Market providers behind the registry, each verified and rate-limited by a token bucket: Kraken (REST and WebSocket v2), Coinbase Exchange public, Binance `data-api.binance.vision` and `data-stream.binance.vision` (market data only, jurisdiction check first), plus one or two more verified venues. CoinGecko keyless as metadata fallback only.
- WebSocket client with bounded backoff, resubscribe from a cursor, gap and out-of-order detection, REST as bootstrap and fallback.
- Shared data-quality library: duplicates, gaps, OHLC consistency, crossed or locked books, non-finite values, clock drift. Candles get the same freshness states as quotes.
- Deep history: `data.binance.vision` bulk files with published checksums verified, converted to partitioned Parquet with a DuckDB catalog and SHA-256 manifests (optional extra). Kraken OHLC, limited to 720 candles, is used for bootstrap and gap fill only.
- News and macro from a versioned `sources.yaml`: ECB, Fed, BoE, BIS, SEC, CFTC, ESMA, European Commission, BaFin, BEA and Treasury feeds with exact verified URLs, conditional GET and attribution text. ECB SDMX and FRED `fredgraph.csv` keyless; World Bank; BLS v2 keyless. GDELT DOC at most one request per 6 s with caching, bulk files for volume. Fear and Greed index with attribution next to the value.
- Exchange status pages and GitHub release Atom feeds raise venue state to DEGRADED through structured fields only.
- Chain read data: mempool.space and Blockstream for BTC, Blockchair for LTC, public Solana and EVM RPC, an alternate explorer for DGB. Light polling, no heavy use of public services.
- Weekly feed canary flips stale or failing sources to DEGRADED in the UI. Known problem endpoints (for example an Eurostat HTTP 500 seen in research) stay disabled until re-verified.

## Wave 3: SafeFetch and the untrusted-text pipeline

- `hcs_trader.webfetch`: DNS-pinned connect, every resolved address must be global, only http and https on ports 80 and 443, no redirects to private ranges, userinfo rejected, `trust_env=False`.
- robots.txt with RFC 9309 semantics (protego), cached up to 24 hours, 5xx means disallow. Per-host rate limit, ETag and Last-Modified cache in SQLite, circuit breaker, hard size and time caps, decompression-bomb guard.
- Honest user agent with an operator-set contact URL. No rotation, no stealth, no paywall or login bypass, no CAPTCHA handling.
- Extraction: feeds and sitemaps first, then trafilatura as an optional extra. A headless browser stays an opt-in extra outside the single-file exe.
- Untrusted pipeline: sanitise markup and hidden or bidirectional characters, NFKC, quarantine, structured extraction, deterministic gate. Tiered source trust (official primary, licensed press headline-only, community).
- Exposure at READ tier: `GET /api/v1/webfetch/*` and MCP tools that take only `source_id`. A test table covers loopback, link-local, metadata IPs, IPv4-mapped IPv6, decimal and hex IPs, redirect-to-localhost and DNS rebinding.

## Wave 4: MCP, Skills and local AI

- **MCP:** typed return models with `outputSchema` and `structuredContent`; cursor pagination and field projection on read-heavy tools; remove duplicate alias tools from the default list; accurate annotations; the four prompts promised in AGENTS.md (`market_brief`, `risk_review`, `strategy_postmortem`, `release_checklist`); paginated resources. A launchable loopback Streamable HTTP server with token, Origin and Host checks. Human confirmation for gated PAPER tools through the current confirmation mechanism, fail-closed. Do not build on sampling, roots or MCP logging (deprecated in the 2026-07-28 spec). Verify spec and SDK claims against primary docs before coding. Finish the registry `server.json` hashes.
- **Skills:** add the required `description` frontmatter and drop unknown keys; qualified tool names; reference files one level deep; a test that every skill's `required_tools` exist and match the declared tier; skill evals.
- **Local AI:** pin the llama.cpp build tag in `docs/MODELS.md`; a single OpenAI-compatible `LocalModelProvider` (llama-server, Ollama, LM Studio); constrained JSON as the only model-to-app channel; System 2 output becomes a validated `SignalProposal` with `NO_TRADE` on any failure; a model catalog of Apache-2.0 or MIT models up to 9B with license and SHA-256 gating and operator-initiated download only; FTS5 retrieval with mandatory citations (vectors via sqlite-vec later as an extra); an eval harness for schema validity, citation faithfulness and injection resistance with release-blocking thresholds. Product text keeps OpenJEV and System One JEV claims marked unverified.

## Wave 5: API, UI and quant

- **API:** SSE for quotes, freshness, kill-switch state and agent events; cursor pagination, ETag and conditional GET; curated OpenAPI with examples and error models; a committed OpenAPI snapshot with a drift test; one endpoint contract without aliases.
- **UI:** generated TypeScript client (openapi-typescript), TanStack Query as the only server-state layer, stale and offline banners, a provider status panel with key-required states, safe rendering with CSP, German and English, an Overview with equity, drawdown, hot/cold and freshness, deeper World Monitor, Markets and Wallets screens, Playwright end-to-end tests with an axe gate on three systems. Lightweight Charts with the TradingView attribution notice.
- **Quant:** the backtest engine runs any `Strategy` instead of a hard-coded SMA rule; fee tiers, half-spread, impact and partial fills from one shared cost model used by backtest, paper and risk; a sizing module that turns a signal into a bounded order; correlation-cluster and concentration caps in the deterministic risk engine; purged and anchored walk-forward; rule-based regime features with look-ahead tests; indicator parity checks against an optional TA-Lib. Licence policy: no backtesting.py (AGPL), no vectorbt in the distributable.

## Cut line

- **v1.1 stable:** Waves 1 to 3 and the core of Wave 4 (typed MCP outputs, skills fix, local-model adapter, eval harness).
- **v1.2:** Wave 5 and the remaining Wave 4 items.
- A wave does not start before the previous exit gate passes. After the Wave 4 core, a release candidate soaks at least seven days in PAPER before the `v1.1.0` tag.

## Operator-only items (not closable by an agent)

Testnet keys and the Binance, Sepolia and devnet rehearsals; any real Kraken evidence; free keys (FRED, CoinGecko Demo); Windows and macOS signing identities; PyPI Trusted Publishing; an independent security review of signer, wallet, risk and MCP paths; legal review before any third-party service.

## Highest risks (from the planning critic)

1. The multi-asset refactor touches the security-critical path (mitigated by principle 4).
2. External source claims come from one-off probes (principle 3).
3. Growth of the untrusted-content and SSRF surface (Wave 3 design, `source_id` only).
4. More unwired code (Wave 1 comes first).
5. Inconsistent cross-venue quorum rules (principle 7).
6. Too much scope and packaging breakage from new native dependencies (cut line, optional extras).
7. SDK claims unverified; the installed `mcp` differed from the pin during planning (W1-D).
8. New CI gates can break existing ones (measure floors first; `filterwarnings=error` may trip on deprecations).

## Wave status (2026-11-27)

- **Wave 1 — done.** All five tracks are merged and covered by tests.
- **Wave 2 — partial.** The shared data-quality library (`data/quality.py`), the keyless Coinbase Exchange public REST adapter behind the reviewed `VenueConfig` host allowlist, the transport-injected Kraken v2 book feed with cursor/gap handling and bounded backoff, and the versioned source registry (`data/sources.json`) are implemented with 34 offline tests. Binance `data-api.binance.vision` is deliberately **not** enabled: it needs the jurisdiction check and a verification record first. WebSocket transport itself stays injected; no websocket dependency was added.
- **Wave 3 — core done.** `webfetch.py` enforces host/port/address validation, refuses redirects and caller-supplied URLs, re-resolves DNS to detect rebinding, applies robots.txt with a 24 h cache (5xx means disallow), per-source budgets, conditional GET and hard size limits. `data/untrusted.py` strips scripts, hidden/bidirectional characters and instruction-like lines and marks output with an `UNTRUSTED` envelope. Wired at READ tier through `GET /api/v1/webfetch/sources`, `GET /api/v1/webfetch/{source_id}` and MCP `webfetch.list_sources` / `webfetch.get`. All shipped sources remain disabled until verification.
- **Wave 4 — core done.** Typed MCP outputs with `outputSchema` and `structuredContent` (`news.search_page`, `quant.size_position`, `webfetch.*`), opaque cursor pagination and field projection, four prompts already present, loopback HTTP bearer/Host/Origin guards already present, skills frontmatter and required-tool tests already present. Added `model_catalog.py` (license/SHA/9B admission), `agents/eval.py` (schema, citation and injection gates) and [model policy](MODELS.md). The eval harness is wired to `hcs-trader eval-model` and is a release gate. Remaining: alias-tool cleanup and the wider Wave 5 UI work.
- **Wave 5 — started.** Content ETags with conditional GET, cursor pagination on news search and audit events, a committed OpenAPI snapshot with drift detection, a bounded loopback SSE status stream, a deterministic read-only sizing preview (REST + MCP), a shared cost model wired into the backtester and exposed at `POST /api/v1/quant/costs`, opt-in correlation-cluster exposure caps in the risk engine, and the CLI commands `sources`, `webfetch` and `eval-model` are implemented; the CLI surface has a guard test against dangerous commands. Generated TypeScript client, locale switching, deeper UI screens and Playwright/axe remain open.
- **Release state:** `1.1.0-rc.1`. PRs #6–#10 are merged to `main`; the tag produced a fully verified draft GitHub release with native single-file binaries, MCPB bundles, wheel, sdist, SBOM, Sigstore provenance and checksums. A static UI-only preview is deployed on Vercel (no backend, wallet or MCP surface), and a tested WebGPU/WebAssembly capability probe is available in the frontend. The seven-day PAPER soak, operator testnet keys, signing identities and independent review are not closable by an agent; see [known limitations](KNOWN_LIMITATIONS.md).

