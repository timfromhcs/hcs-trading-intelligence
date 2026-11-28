# V1 release assessment

## Release candidate evidence · 2026-11-16 (local Windows, commit b66c2d2 plus this documentation commit)

- Gates (`scripts/run_gates.py`): ruff, ruff format, mypy, pytest (coverage gate 80%) and secret scan all exited 0.
- `hcs-trader selftest` passed on the source install and on the frozen single-file `HCS-Trading-Intelligence.exe` (27,455,196 bytes, SHA-256 `e61b5e35ee9e50692ef0b0a6c0103fa1e78fe20a650bea46d7ba3a6b078a9b89`): migrations, PAPER default, kill switch surviving a process kill, loopback bind, MCP stdio tool listing.
- Wheel `hcs_trader-0.2.0-py3-none-any.whl` SHA-256 `ffe6d0d82bb1854ff6f80f2f92246dc077956f82c999b7f403970893d43ef476`; sdist `007ed4f68736994f924f1f3c52d6975a6e198f1a7cf5e5f27152a453dc47a3dd`.
- New this candidate: venue-agnostic spot port with Binance Spot Testnet adapter and Kraken validate probe, EVM/Solana chain-valid signers, regtest PSBT node-signer, release workflows with `selftest`, MCPB bundle. All verified offline or against fixtures only.
- Not verified: macOS and Linux native runs, any live testnet or venue run, signing/notarization, provenance on a published release. These need the operator (keys, public repository, signing identities) and GitHub-hosted runners. `v1.0.0` is therefore not declared; this is a release candidate.


## Hosted platform rehearsal · 2026-11-16

[Foundation checks run 43850707718](https://github.com/timfromhcs/hcs-trading-intelligence/actions/runs/43850707718) passed on commit `dbfc633`. Windows, macOS and Ubuntu each passed the native desktop smoke test, clean-environment wheel and source installs, and the upgrade from the pinned v0.2.0 wheel. The upgrade preserves the execution pause and audit event across installation. The Windows single-file build also passed on this run. These results are historical candidate evidence. Repeat on the exact release commit.

## Candidate CodeQL findings · 2026-11-16

[CodeQL run 43851455472](https://github.com/timfromhcs/hcs-trading-intelligence/actions/runs/43851455472) analyzed commit `47ca6c3`. JavaScript/TypeScript reported zero findings. Python reported six `py/weak-sensitive-data-hashing` findings. The two production occurrences fingerprint the Kraken API key identifier to bind an approval to that identifier. Kraken authenticates requests with a separate private signing key. The remaining four findings fingerprint test fixtures. Reviewed as false positives for password storage. Repeat the scan and review on the final release commit. [Kraken API authorization](https://docs.kraken.com/api/docs/rest-api/get-api-key-info)

## Research and market-data regression evidence · 2026-11-16

- A persisted forward shadow replay survives reopening SQLite. The scheduler emits it once, and an explicit replay returns the same ID. REST, CLI and MCP agree on that ID after restart. The replay leaves the paper fill ledger untouched. The walk-forward promotion test also rejects a mismatched report and requires the separately scored holdout.
- A Kraken route test exercises a provider outage with a recent cached quote. REST still displays the source and degraded quote status, while both submission entry points reject an outage before reserving an order. A fresh fixture quote restores eligibility for the remaining risk and canary checks. These tests use a simulated provider and venue.
- On Windows, a separate Python process persisted a kill-switch trip and exited immediately via `os._exit`. Reopening its database preserved the active switch and blocked a new spot reservation. Clean-machine and cross-platform evidence for KL-003 remains open.

## Integration checkout · 2026-11-16

The integration checkout combines the 16 local commits beyond `7093314` with the implementation from the separate Claude worktree. Its release artifacts retain version `0.2.0`. Local Windows verification includes a native desktop build, a single-file EXE, wheel and source installs, the PAPER API, and a packaged MCP stdio request denied by the default capability policy.

The runtime now gates Kraken submission on the persisted LIVE operator policy and the limitations register. Authenticated Kraken rehearsal evidence, authoritative portfolio and reconciliation inputs for production submission, and chain-valid signing for on-chain execution remain open. The fixture tests exercise submission and rejection behavior with synthetic credentials.

The draft workflow now has a SHA-pinned provenance attestation step. The generated bundle, native macOS and Linux artifacts, upgrade and crash-recovery checks, SAST results and platform signing evidence must still be verified on the candidate. See the [ordered roadmap](ROADMAP.md#integration-status--2026-11-16) and [limitations register](KNOWN_LIMITATIONS.md).

## Earlier baseline · 2026-11-15

Audit date 2026-11-15. Baseline `origin/main` at `7093314`. The [implementation plan](ROADMAP.md) tracks the work required by [AGENTS.md](../AGENTS.md#18-v100-definition-of-done).

## Verified evidence

- GitHub's baseline workflow passed on Windows, macOS and Ubuntu. Its matrix includes native desktop and wheel smoke tests. The security job also passed. [GitHub Actions run](https://github.com/timfromhcs/hcs-trading-intelligence/actions/runs/43751525537)
- The v0.2.0 release publishes a Windows executable, wheel, source archive, selected checksums and license inventory. macOS and Linux binaries are CI artifacts. `main` is eight commits ahead of the public tag. [Published artifacts](https://github.com/timfromhcs/hcs-trading-intelligence/releases/tag/v0.2.0)
- The dashboard was rendered in Edge at 1440 × 1000 with an isolated fresh database. The capture shows the default PAPER state with external network and execution disabled. [Dashboard screenshot](dashboard.png)
- The offline Strategy Lab flow was exercised in Edge with synthetic candles in an isolated database. The browser generated both reports, registered a candidate and promoted it through WALK_FORWARD_PASSED. [Strategy Lab screenshot](strategy-lab.png)
- Edge showed the empty Research Runs panel and then displayed stored RSI evidence after a model-free CLI brief and refresh. The research table remained within its panel at 390 px. [Research Runs screenshot](research-runs.png)
- The direct and tool-enabled Research Briefs each completed with the actual LFM2.5-350M Q4_K_M GGUF on llama.cpp b12423 for Windows x64 CPU. Both used synthetic candle evidence and saved successful run records. [Model and runtime evidence](LOCAL_RESEARCH.md#verified-gguf-configuration)
- The Ethereum wallet CLI read a finalized Sepolia balance and transaction receipt through a temporary loopback relay to a public Sepolia RPC. Mainnet rejected the same node. [Sepolia evidence](WALLETS.md)
- The Solana wallet CLI read a finalized Devnet balance and confirmed a successful finalized transaction via a temporary loopback relay to the official Devnet RPC. The Mainnet profile rejected the same node on the genesis check. [Devnet evidence](WALLETS.md)
- A real Bitcoin Core 30.4 Windows x64 regtest node verified the authenticated UTXO scan and isolated snapshot. An offline Signet node confirmed identity fields. Public Signet scan evidence remains open. [Bitcoin test network evidence](WALLETS.md)
- Real Windows x64 Litecoin Core v0.21.5.8 and DigiByte Core v9.26.7 regtest nodes both verified authenticated UTXO scans, separate snapshots and wrong-network rejection. [Litecoin and DigiByte evidence](WALLETS.md)
- The server startup integration test verified that Kraken recovery is opt-in, issues only order queries and preserves unresolved state when the fixture has no match. Authenticated Kraken venue evidence remains open. [Execution evidence](EXECUTION.md)
- Local tests exercise deterministic indicators, paper accounting, backtests, migrations, MCP stdio, wallet parsing, execution adapters and signer logic. Controlled adapter responses establish behavior under those fixtures.
- The isolated signer is bound to persisted execution pause and approved risk decisions, enforcing idempotency and raising SigningUnsupported on non-chain-valid placeholders. [Signing evidence](SIGNING.md)
- Jupiter Swap API V2 `/build` response inspection normalizes instructions and restricts Associated Token Account creation to `CreateIdempotent` for the signer on classic SPL Token mints. [Execution evidence](EXECUTION.md)
- MCP enforces capability tiers (`READ`, `RESEARCH`, etc.) via `CapabilityGuard`, logs append-only SHA-256 argument hashed invocation records, and aligns risk policy defaults with `RiskPolicy`. [MCP evidence](MCP.md)
- Hardened CI workflows are configured: CodeQL (Python, JS/TS), OpenSSF Scorecard, Dependabot, CODEOWNERS, Contributor Covenant 2.1, and security policies.

## Open release criteria

- **Product and UI.** Exercise populated and empty states, errors, mobile widths and keyboard navigation. Cross-check displayed capabilities against REST, MCP and CLI.
- **Research and promotion.** Link gates to stored evidence. Verify walk-forward and holdout thresholds and demonstrate restart-safe paper shadow runs.
- **Wallets and execution.** Verify chain-specific node identities and read-only data on supported test networks. Exercise isolated signing and reconciliation in supported rehearsal environments. The current server exposes no live order submission path.
- **Reliability and security.** Verify restart and ambiguous venue recovery. Bind dependency and secret scans to the exact release commit. Review signer isolation, write authorization and promotion gates.
- **Distribution.** Publish and smoke-test the full native artifact set with checksums, provenance, dependency inventory and applicable signing or notarization evidence.

V1 readiness remains open.

## 1.1.0-rc.1 update (2026-11-27)

The integration branch adds Wave 1 (previously audited), Wave 2's shared data-quality library, keyless Coinbase public REST adapter, Kraken v2 book feed and versioned source registry; Wave 3's SSRF-safe fetch and untrusted-text pipeline wired at READ tier through REST and MCP; and Wave 4's typed MCP outputs with cursors and projection, model catalog, offline eval harness and local-model policy. The latest verification records **1289 passed / 6 skipped** Python tests with all seven gates green, plus frontend lint/typecheck/tests/build, wheel and sdist install smoke tests, and onedir/onefile desktop smoke tests. Wave 5 additions include conditional GET and cursor pagination, a bounded SSE status stream, an OpenAPI drift snapshot, deterministic sizing and shared cost previews across REST/MCP/CLI, opt-in correlation-cluster caps, and a release-gating offline model eval. Evidence is in `gates-report.json` and the CI artifacts of the release-candidate commit.

This is a release candidate, not a stable `v1.x` tag. The following remain open and are listed in [known limitations](KNOWN_LIMITATIONS.md): socket-level DNS pinning for web fetch, operator testnet keys and live venue evidence, chain signing reviews, all external source verification records, the seven-day PAPER soak and independent security review. No live route is enabled by this work and default mode stays PAPER.

