# Spot execution and deterministic risk

Phase 8 develops a bounded Kraken spot adapter and an independent pre-trade risk engine. The local application currently exposes order history and status. Integrating submission with a durable operator gate and proving venue behavior are release work.

## Non-negotiable execution boundaries

In accordance with [AGENTS.md](../AGENTS.md):
- **Spot only:** Leverage, margin borrowing, short selling, perpetual contracts, and derivatives are strictly forbidden. Any request specifying leverage or borrowed funds is rejected immediately.
- **Withdrawals disabled:** The execution adapter uses `AddOrder`, `CancelOrder`, `QueryOrders`, `OpenOrders` and `ClosedOrders`. The adapter and tool catalogue expose no transfer or withdrawal capability.
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
- **Fail-closed reconciliation:** A timeout, inconclusive HTTP response or live acknowledgement missing its venue ID leaves the order in `RECONCILIATION_REQUIRED`. With a venue ID, the adapter queries `QueryOrders`. Without it, it searches open and closed orders by exact client ID. A missing or non-unique match remains unresolved. [Kraken open orders](https://docs.kraken.com/api-reference/account-data/get-open-orders) · [Kraken closed orders](https://docs.kraken.com/api-reference/account-data/get-closed-orders)

`KrakenSpotExecution` is the internal orchestration path for BTC/USD limit orders. After risk evaluation it commits the order in `SUBMITTED` with the risk decision ID before the request. The transaction rejects duplicates, a persisted pause, unresolved orders at the venue or a full active-order budget. A test interrupts submission while the request is in flight, opens the database again and confirms that recovery sends only order queries. The application server has no active submission endpoint.

Start with `hcs-trader serve --kraken-reconcile-credentials FILE` to enable one recovery pass during startup. Use a Kraken credential with open- and closed-order query permissions for the same account as the stored orders. The file has the same JSON shape as the validation credentials below. The startup path only calls order queries and applies an observation if the original record is still current. An unresolved order or a 30-second timeout records `spot.recovery.incomplete`. The dashboard still starts. Recovery alone reports `DATA_ONLY` and leaves execution unavailable. Startup without this option makes no private Kraken requests.

### Venue validation

`hcs-trader kraken-validate --credentials-file FILE --side buy --quantity 0.01 --limit-price 50000` explicitly requests Kraken's `validate=true` mode for a BTC/USD limit order. Use an operator-controlled credential file with JSON string fields `api_key` and `api_secret` and the minimum Kraken permissions needed to validate. The validation adapter cannot submit or cancel live orders. A successful command emits a validation result without a venue order ID or SQLite order record. A returned order ID or malformed response produces an error. The CLI contract is tested with synthetic credentials and HTTP responses. An authenticated venue run is pending. [Kraken Add Order reference](https://docs.kraken.com/api-reference/trading/add-order)

### Local canary authorization

Use `hcs-trader spot-canary-allow CANDIDATE_ID --credentials-file FILE --max-notional 75` from a local interactive terminal. The candidate must have passed paper approval. The prompt displays the venue, market, candidate, credential fingerprint and USD limit before accepting `AUTHORIZE`. The authorization lasts 24 hours, applies to one candidate and replaces the prior grant. The database stores the key fingerprint, candidate version and dataset hash. The credential secret remains in the operator's file. `hcs-trader spot-canary-revoke` clears the grant immediately for future order reservations.

The active authorization permits the local promotion gate to record `LIVE_CANARY`. A reservation through the internal execution service requires that stage, the same key fingerprint and a limit-order notional within the granted cap. The execution pause and the deterministic risk checks still apply. Serving stays without live submission, and `LIVE_BOUNDED` remains gated pending canary evidence.

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
