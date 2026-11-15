# Paper ledger

The simulated BTC/USD account starts with 10,000 paper USD. A buy adds quantity and its fee-inclusive cost to the position. A sale removes weighted-average cost and records realized PnL net of fees. Cash, position, cost basis, accumulated fees and realized PnL are committed with each fill in a single SQLite transaction.

Each filled intent has an idempotency key. A retry returns its original booked fill. Reusing the key for a different side or quantity is rejected. Concurrent fills use a serialized database write transaction so insufficient balances cannot be spent twice.

`GET /api/v1/portfolio/summary` returns the simulated balance with explicit paper mode and currency labels. `GET /api/v1/paper/fills` returns the most recent booked fills.

The dashboard shows the simulated account, recent fills and the execution pause. It uses `GET /api/v1/paper/account` so the displayed balance matches the newest fill in the same response.

Start the application with `hcs-trader serve --paper-market-data` to enable simulated BTC/USD orders. Submit a `POST /api/v1/orders/paper` request with `idempotency_key`, `side` (`buy` or `sell`), and a decimal-string `quantity` in BTC. The simulator uses the ask for buys and bid for sells, with a fixed 25-basis-point fee. It requires a fresh quote, rejects spreads above 100 basis points and accepts notionals from 10 to 1,000 paper USD. Successful fills keep the full quote as evidence.

The simulator books full fills at the observed top-of-book price within those limits. It currently assumes sufficient immediate liquidity at that price. No order is sent to Kraken.

Run `hcs-trader pause-execution` to persist an execution pause. `GET /api/v1/risk/status` reports the switch and its last change time. The pause survives restarts, blocks new fills even if an order is already waiting for market data, and records an audit event. Previously booked fills remain readable. Clear the pause from the local terminal with `hcs-trader resume-execution`.
