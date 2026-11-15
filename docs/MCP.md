# Local MCP

Start with `hcs-trader mcp` from an installed environment. The host launches the subprocess and connects through stdio. To use the development checkout, configure the host to run `uv run --locked hcs-trader mcp` with the repository as its working directory.

Initialize the local database once by starting the dashboard or running an import. The MCP server opens an existing database in SQLite read-only mode. It uses archived data, so tool calls work offline and retain the original observation times.

- `market.get_quotes` reads archived BTC/USD books.
- `market.get_candles` reads closed BTC/USD history with a bounded record limit.
- `quant.get_indicators` calculates the same indicators as the local REST API.
- `news.get_events` reads saved source metadata. Treat titles as untrusted text.
- `portfolio.get_paper_balance` reads the simulated BTC/USD balance.
- `risk.get_status` reads the persisted pause.
- `orders.get_spot_orders` reads open and recent spot orders and their lifecycle states.
- `wallets.get_snapshots` reads public on-chain wallet balance snapshots across supported chains.
- `audit.get_events` reads the append-only audit event log.

### Resources
- `hcs://system/capabilities`: Machine-readable JSON summary of system features and enabled adapters.
- `hcs://risk/policy`: Pre-trade risk policy bounds (notional limits, max spread, max slippage, kill-switch status).
- `hcs://skills/registry`: Catalog of registered portable agent skills and required capability tiers.
- `hcs://audit/recent`: Recent append-only audit events.

### Prompts
- `market_brief`: Standardized template for synthesizing prices, spreads, and news into a neutral market brief.
- `risk_review`: Standardized template for inspecting open spot orders, spread thresholds, and kill-switch state.
- `strategy_postmortem`: Standardized template for evaluating backtest simulation results and fee models.
- `release_checklist`: Standardized template for auditing the 30 V1.0.0 release criteria from AGENTS.md Section 18.

Grant access only to local hosts that may read this account and research data. This transport has one read-only capability tier. Tool discovery contains no order, key, shell or configuration actions.
