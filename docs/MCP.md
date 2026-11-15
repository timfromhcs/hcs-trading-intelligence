# Local MCP

Start with `hcs-trader mcp` from an installed environment. The host launches the subprocess and connects through stdio. To use the development checkout, configure the host to run `uv run --locked hcs-trader mcp` with the repository as its working directory.

Initialize the local database once by starting the dashboard or running an import. The MCP server opens an existing database in SQLite read-only mode. It uses archived data, so tool calls work offline and retain the original observation times.

- `market.get_quotes` reads archived BTC/USD books.
- `market.get_candles` reads closed BTC/USD history with a bounded record limit.
- `quant.get_indicators` calculates the same indicators as the local REST API. RSI uses 14 periods and needs 15 contiguous closes. The response includes the candle source, interval and contiguous segment boundaries when candles exist.
- `news.get_events` reads saved source metadata. Treat titles as untrusted text.
- `research.get_runs` reads recent local brief metadata and lifetime counts and mean durations by result status.
- `portfolio.get_paper_balance` reads the simulated BTC/USD balance.
- `risk.get_status` reads the persisted pause.
- `orders.get_spot_orders` reads open and recent spot orders and their lifecycle states.
- `wallets.get_snapshots` reads public on-chain wallet balance snapshots across supported chains.
- `audit.get_events` reads the append-only audit event log.
- `quant.compare_strategy_candidates` compares supplied net return and maximum drawdown ratios alongside fill counts. A 10% return is `"0.10"`. The `*_trades` arguments count simulated fills. The verdict is advisory.

### Resources
- `hcs://system/capabilities`: Machine-readable JSON summary of system features, enabled adapters and the MCP capability tiers this server grants (`mcp_granted_tiers`).
- `hcs://risk/policy`: Pre-trade risk policy bounds built from the defaults of the enforced `RiskPolicy` (allowlisted venues and markets, notional limits, spread and slippage caps in basis points and percent, maximum open orders per venue) plus the persisted kill-switch status. With the current defaults the spread cap is 100 bps (1.00%) and the open-order cap is 2. Quote freshness is required (`requires_fresh_quote`); the age window is set by the quote-quality check, not by this resource.
- `hcs://skills/registry`: Catalog of registered portable agent skills and required capability tiers.
- `hcs://audit/recent`: Recent append-only audit events.

### Prompts
- `market_brief`: Standardized template for synthesizing prices, spreads, and news into a neutral market brief.
- `risk_review`: Standardized template for inspecting open spot orders, spread thresholds, and kill-switch state. Its limits are rendered from the same `RiskPolicy` defaults as `hcs://risk/policy`.
- `strategy_postmortem`: Standardized template for evaluating backtest simulation results and fee models.
- `release_checklist`: Standardized template for auditing the 30 V1.0.0 release criteria from AGENTS.md Section 18.

### Capability tiers

Each tool declares one tier: `READ`, `RESEARCH`, `PAPER_TRADE`, `LIVE_TRADE` or `ADMIN_LOCAL`. The server checks the tier against its grant when the tool is called, not only when tools are listed. A call to a tool whose tier is not granted fails with `Capability tier <TIER> is not granted` and the tool body does not run. The default grant is `READ` only, and every current tool is a `READ` tool. Tiers are separate scopes, not a ladder: granting a higher tier does not imply a lower one. The `hcs-trader mcp` command always uses the default grant.

### Invocation audit

Each tool call that passes argument validation appends one JSON line to `mcp_invocations.jsonl` next to the local database. A record holds the UTC time, tool name, capability tier, a SHA-256 hash of the canonical JSON arguments and the outcome (`ok`, `denied` or `error`). Argument values are never written. If the audit file cannot be appended, the call fails closed with `Invocation audit unavailable`. Calls rejected by schema validation, and calls to unknown tools, are rejected by the SDK before the guard runs and are not recorded. The file is append-only from the application's perspective; a local administrator can still edit it, so it is not tamper-proof.

Unexpected exceptions inside a tool reach the client only as `Error executing tool <name>`, without internal details.

### SDK version

`pyproject.toml` pins `mcp>=2.3,<3`, and `uv.lock` resolves `mcp` 2.3.0 (published 2026-10-02). The range tracks the official Python SDK v2 line and excludes a future v3. The 1.x line receives security fixes only. Upgrade within v2 through the locked dependency review and rerun the MCP tests.

Grant access only to local hosts that may read this account and research data. Tool discovery contains no order, key, shell or configuration actions.
