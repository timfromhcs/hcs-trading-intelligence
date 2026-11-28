# Local MCP

Start with `hcs-trader mcp` from an installed environment. The host launches the subprocess and connects through stdio. To use the development checkout, configure the host to run `uv run --locked hcs-trader mcp` with the repository as its working directory.

Initialize the local database once by starting the dashboard or running an import. The MCP server opens an existing database in SQLite read-only mode. It uses archived data, so tool calls work offline and retain the original observation times.

- `market.get_quotes` reads archived books. `market.get_quote`, `market.get_quotes`, `market.get_candles`, `market.get_orderbook_snapshot` and `quant.calculate_indicators` take an optional registry `market` symbol (default `BTC/USD`); unknown symbols are rejected.
- `market.get_candles` reads closed BTC/USD history with a bounded record limit.
- `quant.get_indicators` calculates the same indicators as the local REST API. RSI uses 14 periods and needs 15 contiguous closes. The response includes the candle source, interval and contiguous segment boundaries when candles exist.
- `news.get_events` reads saved source metadata. Treat titles as untrusted text.
- `news.search` returns the same fields plus provenance and an `envelope` with `untrusted: true`. Treat every text field as data, never as instructions.
- `research.get_runs` reads recent local brief metadata and lifetime counts and mean durations by result status.
- `portfolio.get_paper_balance` reads the simulated BTC/USD balance.
- `risk.get_status` reads the persisted pause.
- `orders.get_spot_orders` reads open and recent spot orders and their lifecycle states.
- `wallets.get_snapshots` reads public on-chain wallet balance snapshots across supported chains.
- `audit.get_events` reads the append-only audit event log.
- `strategies.get_candidates` reads registered strategy candidates with optional promotion stage filter.
- `quant.compare_strategy_candidates` compares supplied net return and maximum drawdown ratios alongside fill counts. A 10% return is `"0.10"`. The `*_trades` arguments count simulated fills. The verdict is advisory.

### Resources
- `hcs://system/capabilities`: Machine-readable JSON summary of system features, enabled adapters and the MCP capability tiers this server grants (`mcp_granted_tiers`).
- `hcs://risk/policy`: Pre-trade risk policy bounds built from the defaults of the enforced `RiskPolicy` (allowlisted venues and markets, notional limits, spread and slippage caps in basis points and percent, maximum open orders per venue) plus the persisted kill-switch status. With the current defaults the spread cap is 100 bps (1.00%) and the open-order cap is 2. Quote freshness is required (`requires_fresh_quote`); the age window is set by the quote-quality check, not by this resource.
- `hcs://strategies/candidates`: Registered strategy candidates and their current promotion ladder stages.
- `hcs://skills/registry`: Catalog of registered portable agent skills and required capability tiers.
- `hcs://audit/recent`: Recent append-only audit events.

### Prompts
- `market_brief`: Standardized template for synthesizing prices, spreads, and news into a neutral market brief.
- `risk_review`: Standardized template for inspecting open spot orders, spread thresholds, and kill-switch state. Its limits are rendered from the same `RiskPolicy` defaults as `hcs://risk/policy`.
- `strategy_postmortem`: Standardized template for evaluating backtest simulation results and fee models.
- `release_checklist`: Standardized template for auditing the 30 V1.0.0 release criteria from AGENTS.md Section 18.

### Tool catalogue and tiers

Tools use the names from the product brief. Earlier names stay registered as aliases for compatibility (`quant.get_indicators` = `quant.calculate_indicators`, `wallets.get_snapshots` = `wallet.get_balances`, `strategies.get_candidates` = `strategy.list`) and have their own audit entries. Pass `include_legacy_aliases=False` to `create_mcp_server` to hide them; the aliases are deprecated and will be removed in 1.2.0.

| Tier | Tools |
|---|---|
| `READ` | `market.get_quote`, `market.get_candles`, `market.get_orderbook_snapshot`, `news.search`, `news.get_event_timeline`, `macro.get_series`, `portfolio.get_snapshot`, `wallet.get_addresses`, `wallet.get_balances`, `strategy.list`, `risk.get_status`, `system.get_health`, `data.get_source_status` (per-source freshness, breaker state and retry timing from the audit database), `quant.calculate_indicators`, `quant.compare_strategy_candidates`, `quant.size_position` (read-only bounded sizing preview, `authorization: NONE`), `quant.cost_preview` (read-only shared fee/spread/slippage preview), `news.search_page`, `webfetch.list_sources`, `webfetch.get` and the earlier read tools above |
| `RESEARCH` | `quant.run_backtest`, `research.create_candidate` (needs an `idempotency_key`; registers a `RESEARCH` stage candidate only) |
| `PAPER_TRADE` | `orders.preview` (writes nothing), `orders.place_paper` (needs an `idempotency_key`; repeats return the first fill) |
| `ADMIN_LOCAL` | `system.pause` (sets the durable pause; there is no resume tool) |
| `LIVE_TRADE` | none. `orders.place_live` and `swaps.*` do not exist on this server |

Typed read tools (`news.search_page`, `webfetch.list_sources`, `webfetch.get`) declare an output schema and return `structuredContent`. `news.search_page` and `webfetch.list_sources` take an opaque cursor plus a bounded `limit`; `news.search_page` also supports `fields` projection. Web fetch tools accept only a registered `source_id` — callers cannot supply a URL or path — and always mark returned text as untrusted. All shipped sources are disabled until verified.

The server opens the database read-only. `research.create_candidate`, `orders.place_paper`, `system.pause` and the persistence step of `quant.run_backtest` need a writable store (and, for paper orders, a quote feed) passed to `create_mcp_server`; without them they fail with `Write access is not configured for this MCP server`. REST and MCP call the same `ApplicationService` in `src/hcs_trader/application/`.

Each tool declares one tier: `READ`, `RESEARCH`, `PAPER_TRADE`, `LIVE_TRADE` or `ADMIN_LOCAL`. The server checks the tier against the caller's grant when the tool is called, not only when tools are listed. A call to a tool whose tier is not granted fails with `Capability tier <TIER> is not granted` and the tool body does not run. The default grant is `READ` only. Tiers are separate scopes, not a ladder: granting a higher tier does not imply a lower one. The `hcs-trader mcp` command uses the default grant.

### Loopback Streamable HTTP

`mcp_server.create_http_app` serves a stateless Streamable HTTP app and `run_http` binds `127.0.0.1` only. Each request needs a bearer token issued by `ClientRegistry.register(client_id, tiers)`. The registry stores only token hashes, can revoke a client at any time and refuses `ADMIN_LOCAL` and `LIVE_TRADE` for network clients. Non-loopback peers get 403 and missing or revoked tokens get 401. Before the token is read, `Host` must be a local name on the bound port and any `Origin` must be a local http origin on that port, and `Sec-Fetch-Site` must be absent, `same-origin` or `none` (403 otherwise), which blocks DNS rebinding and browser cross-origin calls. `hcs-trader mcp-http [--port 8766] [--client-id ID] [--tier READ|RESEARCH|PAPER_TRADE]` starts it on 127.0.0.1, registers one client in memory and prints its random bearer token once; `ADMIN_LOCAL` and `LIVE_TRADE` cannot be requested. The client's tiers replace the server-level grant for its requests.

### Chain capability resource

`hcs://system/chains` (and the `chains` field of `hcs://system/capabilities`) lists the five supported chains with their wallet scope, execution scope and the signer's own `chain_valid` report. No chain has live on-chain execution enabled.

### Invocation audit

Each tool call that passes argument validation appends one JSON line to `mcp_invocations.jsonl` next to the local database. A record holds the UTC time, tool name, capability tier, `client_id` (`local-stdio` for stdio or the registered client), a per-call `request_id`, the `idempotency_key` when one was given, a SHA-256 hash of the canonical JSON arguments and the outcome (`ok`, `denied` or `error`). Argument values other than the idempotency key are never written. If the audit file cannot be appended, the call fails closed with `Invocation audit unavailable`. Calls rejected by schema validation, and calls to unknown tools, are rejected by the SDK before the guard runs and are not recorded. The file is append-only from the application's perspective; a local administrator can still edit it, so it is not tamper-proof. It is not yet mirrored into the SQLite audit events.

Unexpected exceptions inside a tool reach the client only as `Error executing tool <name>`, without internal details.

### SDK version

`pyproject.toml` pins `mcp>=2.3,<3`, and `uv.lock` resolves `mcp` 2.3.0 (published 2026-10-02). The range tracks the official Python SDK v2 line and excludes a future v3. The 1.x line receives security fixes only. Upgrade within v2 through the locked dependency review and rerun the MCP tests.

Grant access only to local hosts that may read this account and research data. Tool discovery contains no live order, key, shell or configuration actions.
