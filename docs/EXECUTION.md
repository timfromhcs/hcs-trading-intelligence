# Spot execution and deterministic risk

Phase 8 introduces bounded live spot execution through an audited CEX adapter and an independent pre-trade risk engine.

## Non-negotiable execution boundaries

In accordance with [AGENTS.md](../AGENTS.md):
- **Spot only:** Leverage, margin borrowing, short selling, perpetual contracts, and derivatives are strictly forbidden. Any request specifying leverage or borrowed funds is rejected immediately.
- **Withdrawals disabled:** The execution adapter implements only spot order endpoints (`AddOrder`, `CancelOrder`, `QueryOrders`, `OpenOrders`). Withdrawal, internal transfer, and sweep endpoints are deliberately omitted from the codebase and cannot be invoked by agents or tools.
- **Default mode:** The application starts in `PAPER` mode by default. Real-money live execution requires explicit operator enablement.
- **Persistent kill switch:** If the kill switch is triggered, all new order submissions are halted immediately.

## Deterministic pre-trade risk engine

The `RiskEngine` independently inspects every proposed spot order before submission against the active `RiskPolicy`:
1. **Kill switch:** Must be clear.
2. **Live mode:** Must be explicitly enabled.
3. **Allowlists:** Venue (e.g. `kraken`) and market (e.g. `BTC/USD`) must be on the approved allowlist.
4. **Strategy approval:** Strategy version must be in the approved set (e.g. `v1.0.0`).
5. **Quote freshness:** Order evaluation requires a `FRESH` top-of-book quote with age within configured bounds.
6. **Spread limit:** Bid-ask spread cannot exceed policy limit (e.g. 100 bps / 1.00%).
7. **Notional boundaries:** Order notional must reside within configured minimum and maximum bounds ($10.00 to $1,000.00).
8. **Slippage protection:** Limit prices are checked against current top-of-book quotes; orders with expected slippage beyond the cap are rejected.
9. **Exposure & open orders:** Active open orders on the venue cannot exceed the concurrency cap (default 2).

Any violation returns a typed `RiskEvaluation` with `allowed=False` and a structured reason.

## Kraken spot adapter

The `KrakenSpotAdapter` communicates directly with Kraken's private REST API using standard HMAC-SHA512 authentication:
- `API-Key`: Header containing the API key.
- `API-Sign`: HMAC-SHA512 over the URI path and SHA-256 of `(nonce + postdata)`, keyed with the base64-decoded secret.
- **Fail-closed reconciliation:** When an order submission experiences an ambiguous outcome (network timeout or HTTP 5xx), the order is transitioned to `RECONCILIATION_REQUIRED`. The adapter does not retry blindly; instead, it queries the venue status by client order ID or venue order ID to ascertain the true state before progressing.

## Solana Jupiter swap adapter

The `JupiterSwapAdapter` provides bounded routing and calldata inspection for Solana swaps:
- **Allowlists:** Jupiter v6 program (`JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4`), SPL Token Program, System Program, Compute Budget Program, and verified token mints (`SOL`, `USDC`, `USDT`).
- **Route checks:** Enforces freshness ($\le 30\text{s}$), max slippage, max price impact, and allowlisted intermediate token hops.
- **Instruction safety:** Inspects all instruction accounts and program invocations in the transaction payload to prevent unauthorized approvals or third-party program calls.

## EVM spot swap adapter

The `EVMSwapAdapter` enforces strict calldata safety and allowance bounds:
- **Chain ID:** Rejects wrong-chain or forked RPC state.
- **Audited router allowlist:** Pins audited Uniswap Universal Router deployments.
- **Audited token allowlist:** Pins canonical ERC-20 contract addresses (e.g. WETH, USDC).
- **Exact-allowance policy:** Only exact, short-lived ERC-20 allowances for the specific swap amount are permitted. Unlimited approvals (`2**256 - 1`) are strictly rejected. A revoke-allowance helper (`approve(spender, 0)`) is provided.
- **Calldata inspection:** Validates method selectors, recipient bounds, swap deadline expiration, gas limits, and gas fee bounds.

## Venue routing for BTC, LTC, DGB

- **CEX spot routing:** Kraken Spot supports `BTC/USD`, `LTC/USD`, and `ETH/USD`.
- **DGB / on-chain execution:** Direct on-chain spend or swap for BTC, LTC, and DGB remains strictly disabled in V1 until verified PSBT and node signing integrations are independently proven. Unsupported CEX markets fail closed with explicit rejections.

## SQLite persistence

Schema version 12 introduces the `spot_orders` table storing:
- `client_order_id`: Unique client identifier for idempotency.
- `order_id`: Internal order identifier.
- `venue`, `market`, `side`, `order_type`, `quantity`, `limit_price`.
- `state`: Current lifecycle state (`PREVIEWED`, `SUBMITTED`, `ACKNOWLEDGED`, `FILLED`, `REJECTED`, `CANCELLED`, `RECONCILIATION_REQUIRED`).
- `risk_decision_id`: Audit correlation identifier.
- Timestamps and execution results (`venue_order_id`, `filled_quantity`, `average_price`, `fee`).
