# Changelog

## Unreleased

- CI tiers: nightly and weekly workflows (full suite with the Hypothesis nightly profile, packaging smoke, mutation testing of risk and signing code, chaos, dependency audits), Hypothesis profiles dev/ci/nightly, per-module coverage floors (`scripts/check_coverage_floors.py`, in `run_gates.py` and CI; the global 80% gate is unchanged), an invariant map (`docs/INVARIANTS.md`) checked by tests, a stricter layering test for signing and execution modules, and a default test block on non-loopback sockets (`allow_network`, `rehearsal` and `regtest` markers opt out).
- Loopback hardening (W1-D): Host allowlist pinned to the bound port, Origin and `Sec-Fetch-Site` checks on writes, JSON content type on writes, and CSP, `nosniff` and `no-referrer` headers on every API and UI response. The MCP Streamable HTTP path validates Host and Origin before the bearer token. New `hcs-trader mcp-http` command (127.0.0.1 only, token printed once).
- Skills follow the Agent Skills frontmatter (`name`, `description`, `metadata`); skill directories are hyphenated to match their names; a test checks every `required_tools` entry against the registered MCP tools and the declared tier.
- Run the ingestion scheduler in the app lifespan behind the opt-in `serve --ingest-feeds` flag (default off, never polls in OFFLINE mode). Persist ingest state, `Retry-After` deadlines and breaker snapshots in the audit database (schema version 20) so a restart does not hammer a provider. Sink and storage errors no longer stop the loop. `QuoteService` now shares the `CircuitBreaker` and persists its state.
- Add `GET /api/v1/data/sources`, the MCP read tool `data.get_source_status` and a Data sources panel on the Overview screen.
- News provenance (schema 21): stored news now carries article id, canonical URL, language, content hash, duplicate group and source trust tier. Tracking-parameter URLs collapse onto one article and a changed title is kept as a revision in `news_revisions`. Dedup state lives in SQLite and survives restart. `news.search` and `GET /api/v1/news/search` use an FTS5 index over title and snippet (with a substring fallback) and add provenance fields plus an `envelope` marking the text as untrusted. Existing fields are unchanged.
- Add a market registry (`domain/markets.py`) with venue pair, precision, minimum size, quote assets and status per market. The Kraken quote and candle adapter takes the market and resolves the response key from the response. BTC, ETH, SOL and LTC against USD and EUR are readable; DGB is registered read-only with no pair. REST `GET /api/v1/market/markets`, `market` query parameter on quotes, history, candles, indicators and orderbook, MCP `market` argument on quote, candle, orderbook and indicator tools, and `import-candles --market`.
- Live Kraken submission and the canary stay BTC/USD only. The risk engine and the spot execution shape check now also reject any market that is not `live_allowed` in the registry. Paper execution stays BTC/USD only.

- Enforce the operator's hot-wallet USD cap and portfolio share in the LIVE risk engine. A missing valuation blocks authorization when a share cap is configured.
- Require venue health and a recent reconciliation timestamp for LIVE authorization. Reject reconciliation timestamps in the future.

- Report a forward shadow replay only when its result is newly recorded. An explicit replay still returns the existing result for identical inputs.
- Require a fresh Kraken quote for CLI and HTTP canary submission. Cached quotes remain visible with their data-quality status during outages.

- Gate the Kraken CLI and HTTP serving paths on the persisted operator policy and security-critical limitations. Recheck approval before submission.
- Verify denied paper writes in the installed MCP smoke test and run frontend tests in CI.

Changes since 0.2.0 on `main`. No release candidate has been cut and the version is still 0.2.0.

### Added

- Strategy Lab promotion evidence, persisted paper-shadow replays, forward shadow observation, and `strategy-list`, `strategy-show`, `strategy-promote` and `strategy-shadow-*` commands. Promotion logic is unified in `promotion.py`.
- REST `GET /api/v1/strategies/candidates/{candidate_id}/shadow-runs`, MCP `strategies.get_candidates` and `strategies.get_shadow_runs`, and the `hcs://strategies/candidates` resource.
- Interactive, expiring authorization for a single Kraken BTC/USD canary order, a validation-only CLI workflow, and guarded Kraken order recovery.
- Deterministic local research tools, durable run history, a foreground scheduler and dashboard statistics.
- Isolated read-only wallet profiles for Sepolia, Solana Devnet, Bitcoin regtest and Signet, Litecoin regtest and DigiByte regtest.
- MCP capability tiers (`READ`, `RESEARCH`, `PAPER_TRADE`, `LIVE_TRADE`, `ADMIN_LOCAL`), a capability guard, and a JSONL invocation audit trail.
- Jupiter Swap API V2 `/build` response inspection, instruction normalization and Associated Token Account `CreateIdempotent` allowlisting.
- Accessible progress bars and a responsive layout for the strategy ladder.
- Documentation: README rewritten to the 20 required sections, wallet security guide, API reference, strategy research guide, data source registry, expanded threat model, architecture diagrams, ADRs 0004 to 0009, and a documentation consistency test.
- CodeQL, OpenSSF Scorecard, Dependabot, CODEOWNERS and Contributor Covenant 2.1.

### Changed

- `IsolatedSigner` is bound to the persisted pause state and to approved risk decisions with idempotency tracking. Placeholder chains return `SigningUnsupported`.
- MCP risk resources and prompts are rendered from the `RiskPolicy` defaults that the engine enforces.
- The threat model no longer copies numeric limits or the schema version.

### Security

- Expanded security policy. The threat model now lists the brief's threats with verifying tests and names the gaps (no injection corpus, no corruption injection, no SBOM).

## 0.2.0 — spot execution, isolated signing, research and dashboard UI (2026-11-15)

- Added canonical asset and market identities, exact monetary values, and explicit order and transaction transitions for later adapters.
- Added a loopback status API, responsive dashboard, and versioned SQLite startup audit history.
- Added opt-in Kraken BTC/USD quotes with bounded refresh, provider provenance and explicit data quality.
- Added deduplicated SQLite quote history and restart-safe fallback to the last stored quote.
- Added the opt-in BTC/USD dashboard panel with exact prices, source, timestamps and quote quality.
- Added explicit import of closed BTC/USD candles and offline candle history through the local API.
- Added offline CSV downloads for stored BTC/USD candles.
- Added an explicit monetary-policy release import and local, attributed news timeline.
- Displayed stored monetary-policy releases in the dashboard, with official links and publication and receipt times.
- Added ECB daily reference-rate import with local revision history and a read-only API.
- Added FRED CPI imports with explicit snapshot dates and private terminal key entry.
- Added offline deterministic indicators over closed BTC/USD candles.
- Added a deterministic SMA trend research backtest with simulated fees, spread, slippage and partial fills.
- Added expanding-window evaluation, a final holdout and a cost-matched buy-and-hold benchmark.
- Added persistent, content-addressed backtest reports with an integrity check on retrieval.
- Added a CLI workflow that freezes a research candidate before opening a committed holdout file.
- Added persistent paper balances and fills with weighted-average cost basis, fees and realized PnL.
- Added explicit opt-in paper order simulation using verified Kraken BTC/USD quotes.
- Added a durable execution pause with local controls and transactional enforcement.
- Displayed the simulated portfolio, recent fills and execution pause in the dashboard.
- Added a read-only MCP server over local stdio.
- Added a local indicator research brief with optional schema-checked llama.cpp commentary.
- Added GGUF manifest validation and research report artifact metadata.
- Added read-only native Ethereum balance checks against a local RPC node.
- Added read-only native Solana balance checks with a mainnet-beta genesis pin.
- Added Bitcoin Core confirmed UTXO balance reads with exact satoshi conversion.
- Added Litecoin and DigiByte confirmed UTXO balance reads and pinned the genesis block for all UTXO adapters.
- Added opt-in persistence and offline REST access for public-address wallet snapshots.
- Added read-only Ethereum and Solana transaction status queries.
- Added native desktop development builds with the embedded dashboard and CI smoke checks.
- Added the Windows single-file executable build and checksum.
- Added isolated transaction plan validation and canonical SHA-256 plan hashing.
- Added encrypted software keystore using Scrypt KDF and AES-256-GCM authenticated encryption.
- Added independent transaction payload decoding and policy-bounded signer boundary.
- Added signing invariant test suite covering policy enforcement, tampering detection, and secret isolation.
- Added deterministic pre-trade risk engine with notional, spread, slippage, quote freshness and open-order bounds.
- Added spot-only Kraken execution adapter with HMAC-SHA512 signing, withdrawals disabled, and fail-closed reconciliation.
- Added Solana Jupiter swap adapter with strict mint, slippage, price-impact, and instruction allowlists.
- Added EVM spot swap adapter with pinned chain ID, Universal Router allowlist, exact-allowance enforcement, and revoke-approval generation.
- Added CEX spot mapping for LTC/USD and ETH/USD, confirming on-chain BTC/LTC/DGB spend remains disabled.
- Added SQLite schema version 12 with persistent spot-order storage and lifecycle tracking.
- Added read-only spot orders API endpoints with venue filtering and single-order retrieval.
- Added emergency execution pause and resume API endpoints with local-origin verification.
- Added frontend Spot Orders panel with lifecycle status badges and reconciliation alerts.
- Added frontend On-Chain Wallets panel with cold reserve isolation boundary notice.
- Added frontend Emergency Kill Switch and execution resume controls.
- Verified Windows single-file executable packaging and automated desktop smoke testing.
- Verified Python wheel distribution in clean virtual environments and generated full SPDX license inventory.

## 0.1.0 — development foundation

- Added the installable CLI with default mode and capability reporting.
- Added locked development tooling and a three-platform verification workflow.
