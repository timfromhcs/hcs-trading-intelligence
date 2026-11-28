# Path to the next stable release

Status: 2026-11-28. Current release candidate: **1.1.0-rc.3** (draft GitHub
release, 16 verified assets, Vercel static UI preview). Milestones C1–C4 are
merged; A and B remain blocked as described below. This document is the
execution plan from the RC to a stable `v1.1.0` tag. It does **not** relax any
gate from [AGENTS.md](../AGENTS.md#18-v100-definition-of-done) or
[the release assessment](V1_RELEASE_REPORT.md).

## Rule

No stable tag until every mandatory item below has evidence. If a step is not
closable by an agent, it stays listed with its owner and blocks the tag; we do
not invent testnet evidence, signatures or soak time.

## Milestone A — operator evidence (blocking, owner: operator)

| Item | Evidence required | State |
|---|---|---|
| Rehearsals | Binance testnet, Sepolia and Solana devnet runs with operator-created keys; recorded results | Open |
| Venue evidence | Authenticated Kraken read-only/validate probe on the operator's account | Open |
| Signing review | Independent security review of signer, wallet derivation, decoders and MCP authorization | Open |
| Signing identities | Windows/macOS signing identity, notarization where applicable | Open |
| Soak | Seven consecutive days in PAPER with no unexplained divergence, logged | Open |
| Publication | PyPI Trusted Publishing and MCP registry publication credentials | Open |

Agent action while blocked: keep all live paths fail-closed, maintain the
read-only/verification surface, and report the blocker each cycle.

## Milestone B — UI completion (owner: agent, needs design tooling)

1. Restore the `better-design_*` tool session; apply the project's design system
   and accessibility rules to all changed screens.
2. Generate the TypeScript client from `tests/fixtures/openapi-snapshot.json`
   (`openapi-typescript`) and make it the single HTTP layer; delete hand-written
   duplicate request types.
3. German/English navigation and mode labels are implemented with a persisted locale switch and no hardcoded financial advice; reports remain English and deeper screen translation is still open.
4. Deepen Overview (equity, drawdown, hot/cold, freshness), World Monitor,
   Markets and Wallets screens using only verified API fields.
5. Add Playwright end-to-end tests with an axe accessibility gate on Windows,
   macOS and Linux.
6. Show stale/offline banners and provider key-required states; keep the
   WebGPU/WebAssembly panel informational only.

Acceptance: frontend lint, typecheck, 29+ unit tests, build, Playwright and axe
green on all three runners; no fabricated balances or capabilities.

## Milestone C — remaining research/risk work (owner: agent)

1. Wire the shared cost model into paper execution and the risk pre-trade gate;
   prove identical numbers to the backtester with a cross-module test.
2. Purged and anchored walk-forward evaluation; regime features with
   look-ahead tests; optional TA-Lib indicator parity check.
3. Deprecate duplicate MCP alias tools behind a versioned compatibility flag,
   with an explicit removal note in the changelog.
4. Close `KL-006` with a transport-level DNS/IP pinning integration test before
   any external source is enabled.
5. Add verification records and fixtures for the disabled sources in
   `src/hcs_trader/data/sources.json`; enable nothing without a recorded check.

Acceptance: all seven gates green; 13.2 invariants still mapped; no weakened
limit or skipped test; coverage floors unchanged or raised.

## Milestone D — publish (owner: operator + agent)

1. Rerun `ci.yml` and `release.yml` on the exact candidate commit.
2. Verify SHA-256/SBOM/provenance and the installed smoke tests.
3. Publish the draft release, sign/notarize platform assets, publish wheel to
   PyPI and the MCP registry entry.
4. Update README feature statuses to `Implemented` only where tests exist.
5. Tag `v1.1.0` only when A–D are evidenced; otherwise cut another RC.

## Loop protocol

Each 2-minute ping cycle: inspect status → pick the smallest item from the
highest-priority open milestone → write acceptance test first → implement →
run narrow tests → run full gates → PR → wait for all CI checks → merge →
update docs/evidence. On a red gate: capture the exact failure, fix once,
re-run; after three failed attempts on one failure class, stop and report.
