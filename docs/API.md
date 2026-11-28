# Local REST API

The service binds to `127.0.0.1:8765` only. The schema is at `/openapi.json`. Requests must carry a `Host` of `localhost`, `127.0.0.1` or `[::1]` on the bound port (400 otherwise, which stops DNS rebinding; extra names via `create_app(extra_allowed_hosts=...)`). State-changing requests must have no `Origin` or a local http origin on the bound port, and `Sec-Fetch-Site` must be absent, `same-origin` or `none` (403 otherwise). Writes with a body need `application/json` (415) and at most 1 MiB (413). Every response, including rejections and the static UI, carries a Content-Security-Policy (`default-src 'self'`, `connect-src 'self'`, no inline script, `frame-ancestors 'none'`), `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer` and `X-Frame-Options: DENY`. The checks live in `loopback.py` and are shared with MCP HTTP. There is no remote access mode. The API has no generic key export, seed export, arbitrary transfer, arbitrary signing, withdrawal or risk-limit mutation endpoint, and none may be added.

Money and prices are returned as decimal strings. Times are UTC. Endpoints that read stored data work with provider access disabled and return the original observation times.

## Implemented endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/health` | Liveness |
| GET | `/api/v1/system/status` | Mode, network flag and service status |
| GET | `/api/v1/system/config` | Non-secret configuration |
| GET | `/api/v1/system/capabilities` | Capability flags (see README) |
| GET | `/api/v1/risk/status` | Persisted kill switch and pause state |
| POST | `/api/v1/risk/pause` | Set the durable execution pause |
| POST | `/api/v1/risk/resume` | Clear the pause (local operator action) |
| POST | `/api/v1/system/pause` | Alias of `/api/v1/risk/pause` (brief naming) |
| POST | `/api/v1/system/resume` | Alias of `/api/v1/risk/resume` (brief naming; local operator action) |
| GET | `/api/v1/risk/policy` | Read-only risk policy summary |
| GET | `/api/v1/system/chains` | Per-chain capability matrix derived from the signer report |
| GET | `/api/v1/market/quotes` | Live BTC/USD book when public data is enabled, otherwise 503. `market` (registry symbol, for example `ETH/USD`) selects an archived quote for other markets, flagged `provider_issue: archived_only`; unknown markets return 422 |
| GET | `/api/v1/market/quotes/history` | Stored quotes, `limit` up to 500, optional `market` |
| GET | `/api/v1/market/candles` | Stored candles, `interval`, `limit`, optional `market` |
| GET | `/api/v1/market/candles.csv` | Same candles as CSV |
| GET | `/api/v1/market/indicators` | Offline indicators over the latest contiguous candles |
| POST | `/api/v1/backtests` | Run the SMA research simulation and store a report |
| POST | `/api/v1/backtests/walk-forward` | Expanding-window evaluation |
| GET | `/api/v1/backtests/{report_id}` | Retrieve a stored report with integrity check |
| GET | `/api/v1/news/events` | Stored official release metadata |
| GET | `/api/v1/macro/series` | Stored macro observations, optional `source` |
| GET | `/api/v1/portfolio/summary` | Simulated portfolio summary |
| GET | `/api/v1/paper/account` | Simulated balances |
| GET | `/api/v1/paper/fills` | Simulated fills |
| POST | `/api/v1/orders/paper` | Simulated BTC/USD order (needs `--paper-market-data`) |
| GET | `/api/v1/wallets` | Stored read-only wallet snapshots |
| GET | `/api/v1/market/assets` | Asset registry with status and quote assets |
| GET | `/api/v1/market/markets` | Market registry: venue pair, precision, minimum size, `status` (`read_only`, `paper`, `live_allowed`) |
| GET | `/api/v1/market/orderbook` | Top-of-book snapshot from the latest stored quote, optional `market` |
| GET | `/api/v1/data/sources` | Per-source freshness (`FRESH`, `STALE`, `DEGRADED`, `MISSING`, `INVALID`), circuit breaker state, `retry_in_seconds` and whether background ingestion is on. Read from the audit database, so it works with ingestion off |
| GET | `/api/v1/news/search` | Search stored news (FTS5 over title and snippet, plus source id), `q` and `limit`. Existing fields are kept; additive provenance fields (`article_id`, `canonical_url`, `language`, `content_hash`, `duplicate_group_id`, `source_reliability`, `revision_count`, `snippet`) and an `envelope` (`source`, `trust_tier`, `untrusted: true`) mark the text as untrusted data |
| GET | `/api/v1/portfolio/positions` | Simulated positions |
| GET | `/api/v1/wallets/{wallet_id}/balances` | Stored balance snapshots for one public address |
| GET | `/api/v1/wallets/{wallet_id}/transactions` | Reports that no history is stored locally |
| GET | `/api/v1/strategies` | Strategy candidates (alias view of the candidate list) |
| GET | `/api/v1/strategies/{strategy_id}/metrics` | Latest shadow-run metrics for a candidate |
| POST | `/api/v1/orders/preview` | Side-effect-free paper order preview using the submission rules |
| POST | `/api/v1/quant/size` | Side-effect-free bounded long-spot sizing preview from explicit Decimal inputs; returns `authorization: NONE` and never submits or authorizes an order |
| GET | `/api/v1/orders/{order_id}` | One spot order or paper fill by identifier |
| POST | `/api/v1/orders/live` | Alias of `/api/v1/orders/spot`; same gates and risk path |
| GET | `/api/v1/orders/spot` | Spot order list and lifecycle states |
| GET | `/api/v1/orders/spot/{client_order_id}` | One spot order |
| POST | `/api/v1/orders/spot` | Bounded live spot canary submission, fail-closed, needs live credentials and an authorization |
| GET | `/api/v1/research/runs` | Local research run history and statistics |
| GET | `/api/v1/audit/events` | Append-only audit events |
| GET | `/api/v1/skills` | Registered agent skills |
| GET | `/api/v1/strategies/candidates` | Strategy candidates and ladder stages |
| POST | `/api/v1/strategies/candidates` | Register a candidate with saved evidence |
| POST | `/api/v1/strategies/candidates/{candidate_id}/promote` | Evaluate gates and promote |
| GET | `/api/v1/strategies/candidates/{candidate_id}/shadow-run` | Latest shadow replay |
| POST | `/api/v1/strategies/candidates/{candidate_id}/shadow-run` | Run a shadow replay |
| GET | `/api/v1/strategies/candidates/{candidate_id}/shadow-runs` | Shadow run history |
| GET | `/api/v1/strategies/candidates/{candidate_id}/canary-approval` | Canary authorization state |
| GET | `/api/v1/system/stream` | Loopback Server-Sent Events for the current mode, pause state and UTC timestamp. `max_events` bounds the stream for tests and clients; no external data is fetched |
| GET | `/api/v1/webfetch/sources` | Registered external sources with verification state; all shipped sources start disabled |
| GET | `/api/v1/webfetch/{source_id}` | Fetch one registered source by id. The caller cannot supply a URL or path; results are marked `UNTRUSTED`, raw markup is never returned and unverified sources return 403 |

`GET /api/v1/news/search` and `GET /api/v1/audit/events` accept an opaque `cursor` and return `X-Next-Cursor` plus `X-Total-Count`. All `GET` responses under `/api/v1/` carry a content `ETag`; sending `If-None-Match` returns `304` without a body. Streaming responses are excluded. The committed OpenAPI snapshot is checked by `tests/test_openapi_snapshot.py`.

Live order submission passes the same deterministic risk engine as every other path and is off by default.

## Planned endpoints

These come from the product brief and do not exist yet. Calling them returns 404.

| Path | Purpose |
|---|---|
| `/api/v1/system/sources` | Data source registry with quotas, terms and attribution |

The brief's pause and resume names (`/api/v1/system/pause`, `/api/v1/system/resume`) and `/api/v1/orders/live` are implemented as aliases of the original paths. Both REST and MCP call the same application service in `src/hcs_trader/application/`. See [ADR 0008](ADR/0008-rest-path-deviation.md).

## Errors

Errors use FastAPI's JSON `detail`. Typical codes: 403 foreign origin, 404 unknown resource, 422 invalid parameter, 503 provider disabled or storage unavailable. Messages do not contain secrets or internal paths.

## Auth scopes

The REST API relies on loopback binding and origin checks. Per-client scopes and tokens exist only for MCP, where tiers are enforced at tool execution (see [MCP](MCP.md)). Remote access is not supported.
