# V1 release assessment

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

## Open release criteria

- **Product and UI.** Exercise populated and empty states, errors, mobile widths and keyboard navigation. Cross-check displayed capabilities against REST, MCP and CLI.
- **Research and promotion.** Link gates to stored evidence. Verify walk-forward and holdout thresholds and demonstrate restart-safe paper shadow runs.
- **Wallets and execution.** Verify chain-specific node identities and read-only data on supported test networks. Exercise isolated signing and reconciliation in supported rehearsal environments. The current server exposes no live order submission path.
- **Reliability and security.** Verify restart and ambiguous venue recovery. Bind dependency and secret scans to the exact release commit. Review signer isolation, write authorization and promotion gates.
- **Distribution.** Publish and smoke-test the full native artifact set with checksums, provenance, dependency inventory and applicable signing or notarization evidence.

V1 readiness remains open.
