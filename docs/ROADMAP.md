# Implementation plan

Updated 2026-11-15 against GitHub `main` at `7093314`. [AGENTS.md](../AGENTS.md#17-phased-zero-to-v1-implementation-roadmap) defines the scope. The [release assessment](V1_RELEASE_REPORT.md) records verification evidence.

## Repository and PR status

Local HEAD and `origin/main` matched. The initial working-tree content diff touched promotion, MCP and three test files. Git also reported `frontend/src/styles.css` as modified with an empty content diff. The initial local suite had five failures. The audit changes are local.

[PR #1](https://github.com/timfromhcs/hcs-trading-intelligence/pull/1) was the sole PR across all states. It merged as `fd79d33` and touched 94 files covering the dashboard, wallet and execution foundations, research and packaging. Later commits added audit views, MCP resources and prompts, the skills registry and candidate promotion. The latest [public release](https://github.com/timfromhcs/hcs-trading-intelligence/releases/tag/v0.2.0) is eight commits behind the audited main.

## Work in dependency order

### 0–1. Foundation, domain and storage

Present: packaged CLI, loopback FastAPI, typed domain, SQLite migrations, backup and audit. First restore green tests and verify setup from the lockfile. For regressions, retain a reproducer, fix the cause, and rerun isolation and integration checks. Verify upgrades and recovery on interrupted operations.

### 3. Quant research and promotion

Present: deterministic indicators, SMA backtests, walk-forward and holdout primitives, saved reports and candidate cards. BACKTESTED uses a saved report with matching dataset and SMA windows. WALK_FORWARD_PASSED accepts a retrospective report bound to the same development data, single SMA pair and simulation settings. Older candidates without report IDs need to be registered again with saved evidence. Promotions reject stale candidate updates and leave later paper approval and live stages blocked. PAPER_SHADOW requires the separately committed and scored holdout associated with the same development dataset and strategy settings. Dataset confidentiality before freezing remains with the curator. Strategy Lab links the two retrospective reports offline and accepts the separate commitment hash at registration. Paper approval uses fills from a persisted post-promotion shadow replay. Forward shadow observation is implemented (`run_forward_shadow_observation`, `watch_shadow`), supported by CLI commands (`strategy-shadow-run`, `strategy-shadow-history`, `strategy-shadow-watch`) and MCP tool `strategies.get_shadow_runs`.

### 4. Paper and portfolio

Present: BTC/USD simulated fill ledger and portfolio panel. Durable, idempotent shadow replays record immutable inputs with run/fill IDs in SQLite. Replays and forward observations enforce boundary conditions, contiguous candle intervals, and fail-closed promotion gates. Shadow outcomes, net returns, drawdowns, and fills are surfaced across CLI, REST, and MCP.

### 5. MCP, skills, local model and runs

Present: read-only stdio MCP with enforced CapabilityTier grants (`READ`, `RESEARCH`, `PAPER_TRADE`, `LIVE_TRADE`, `ADMIN_LOCAL`), checked via `CapabilityGuard`, append-only JSONL invocation audit trail (`mcp_invocations.jsonl`), five registered skills and an optional loopback llama.cpp research brief with a verified GGUF manifest. Resources and prompts synchronize pre-trade risk policy bounds with enforced `RiskPolicy` defaults. RSI evidence is available in CLI, REST and MCP.

The opt-in research brief supports one validated call to the archived indicator tool under a shared deadline. Completed briefs persist outcome, elapsed time, candle boundaries and model identity in SQLite. CLI, REST and read-only MCP expose recent runs and lifetime status statistics. The opt-in foreground scheduler persists its due time, serializes workers per database, backs off on model unavailability and stops with Ctrl+C. The dashboard shows recent runs and lifetime status statistics and was exercised with a persisted model-free brief in Edge. A Windows CPU run verified both modes against LFM2.5-350M Q4_K_M (Liquid AI) and llama.cpp b12423. In-process test coverage via the official MCP v2 Client exercises tools, resources, prompts, error handling, and capability denial. [llama.cpp function calling](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md#function-calling) · [Qwen model card](https://huggingface.co/Qwen/Qwen3-4B-GGUF) · [MCP tools specification](https://modelcontextprotocol.io/specification/latest/server/tools)

### 2. Market, macro and news data

Present: Kraken BTC/USD books and candles, Fed release metadata, ECB exchange rates and opt-in FRED snapshots. In parallel with research development, add observable freshness, quota and provider health, then extend markets and sources with provenance, deduplication and restart tests.

### 6–7. Wallet network profiles, previews and signing

Present: BTC, ETH, SOL, LTC and DGB RPC adapters guarded by mainnet identity checks, isolated signer primitives and swap decoding. IsolatedSigner is bound to persisted execution pause state and approved risk decision lookup, rejecting signing if paused or if the decision does not match or is unapproved. Idempotency tracking prevents duplicate signatures. Non-chain-valid EVM and UTXO signatures fail closed with `SigningUnsupported`. Implement distinct network profiles, asset IDs and stores. Ethereum Sepolia and Solana Devnet now have explicit profiles, isolated snapshot databases and live read-only RPC evidence. Bitcoin Core regtest has an isolated profile and a verified native UTXO scan. The public Signet profile pins chain identity and the default challenge; a synchronized scan remains pending. Litecoin Core v0.21.5.8 and DigiByte Core v9.26.7 have verified native regtest UTXO scan, snapshot isolation and wrong-network rejection. Public testnet checks remain open. Verify router deployments and cluster support independently of wallet reads. Jupiter Swap API V2 `/build` response inspection normalizes instruction fields and allowlists Associated Token Account `CreateIdempotent` with payer/owner equal to the signer and classic SPL Token program; Token-2022 stays rejected. Devnet route availability remains unverified. [Solana clusters](https://solana.com/docs/references/clusters) · [Ethereum networks](https://ethereum.org/developers/docs/networks/) · [Bitcoin Core chain flags](https://github.com/bitcoin/bitcoin/blob/master/src/chainparamsbase.cpp) · [Jupiter Swap](https://developers.jup.ag/docs/swap) · [Litecoin Core](https://github.com/litecoin-project/litecoin) · [DigiByte Core](https://github.com/DigiByte-Core/digibyte)

### 8. Bounded live spot execution

Present: Kraken spot adapter, risk checks, pause persistence, lifecycle and Solana/EVM validation primitives. Kraken reconciliation by client ID was verified under simulated responses after reopening SQLite. The internal execution service persists pre-submit records and exposes startup reconciliation, verified in interrupted-submit and recovery tests. Read-only recovery is wired to an explicit server startup option and tested with simulated orders and unresolved results. Interactive operator setup persists expiring, revocable canary authorizations (`spot-canary-allow`, `spot-canary-revoke`) and binds internal reservations to authorized key fingerprints, strategy versions, and notional caps ($10–$1000). The canary serving path is verified end-to-end via CLI (`spot-canary-submit`), local REST API (`POST /api/v1/orders/spot`, `GET /api/v1/strategies/candidates/{candidate_id}/canary-approval`), and MCP (`strategies.get_canary_approval`). Live execution is strictly fail-closed upon expiration, exceeding notional cap, execution pause, key mismatch, or candidate stage demotion. Keep direct BTC/LTC/DGB execution outside enabled routes pending individual verification.

### 9. UI/UX and documentation

Present: dashboard with status, risk, portfolio, wallet/order history, audit, news, and Strategy Lab. The README shows a fresh PAPER screenshot. Promotion progress bars feature accessible progressbar semantics (`role='progressbar'`, ARIA value attributes), responsive mobile styling for narrow viewports (<640px), and active canary status indicators. Keyboard accessibility and focus management are verified across panels. Keep screenshots, capability displays, OpenAPI examples, MCP discovery and skill instructions aligned with tested behavior.

### 10–11. Distribution and release

Present: cross-platform CI plus a public Windows build and Python artifacts. Hardened CI workflows added: CodeQL (Python, JavaScript/TypeScript), OpenSSF Scorecard, Dependabot (actions, npm, uv), CODEOWNERS, Contributor Covenant 2.1, and expanded security policies. Reverify the exact release commit across platforms after functional gates pass. Collect published native artifacts, checksums, provenance, dependency inventory and applicable platform signing evidence before declaring V1.
