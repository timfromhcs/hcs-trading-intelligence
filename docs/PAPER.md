# Paper ledger

The simulated BTC/USD account starts with 10,000 paper USD. A buy adds quantity and its fee-inclusive cost to the position. A sale removes weighted-average cost and records realized PnL net of fees. Cash, position, cost basis, accumulated fees and realized PnL are committed with each fill in a single SQLite transaction.

Each filled intent has an idempotency key. A retry returns its original booked fill. Reusing the key for a different side or quantity is rejected. Concurrent fills use a serialized database write transaction so insufficient balances cannot be spent twice.

`GET /api/v1/portfolio/summary` returns the simulated balance with explicit paper mode and currency labels. `GET /api/v1/paper/fills` returns the most recent booked fills. Order submission is a separate milestone.
