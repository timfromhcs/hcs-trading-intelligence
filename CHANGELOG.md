# Changelog

## Unreleased

- Added offline Strategy Lab promotion evidence, persisted paper-shadow replays and guarded Kraken order recovery.
- Added deterministic local research tools, durable run history, a foreground scheduler and dashboard statistics.
- Added isolated read-only profiles for Sepolia, Solana Devnet, Bitcoin regtest and Signet, Litecoin regtest, and DigiByte regtest.
- Added an interactive, expiring authorization for a single Kraken BTC/USD canary candidate and a separate validation-only CLI workflow.
- Bound IsolatedSigner to persisted pause state and approved risk decisions with idempotency tracking and SigningUnsupported for placeholder chains.
- Added Jupiter Swap API V2 `/build` response inspection, instruction normalization, and Associated Token Account CreateIdempotent allowlisting.
- Added MCP capability tiers (`READ`, `RESEARCH`, `PAPER_TRADE`, `LIVE_TRADE`, `ADMIN_LOCAL`), capability guard, JSONL invocation audit trail, and RiskPolicy defaults parity.
- Added CodeQL, OpenSSF Scorecard, Dependabot, CODEOWNERS, Contributor Covenant 2.1, and expanded security policies.

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
