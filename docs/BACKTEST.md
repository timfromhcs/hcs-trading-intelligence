# SMA trend research backtest

The initial research strategy evaluates short and long simple moving averages using completed BTC/USD closes. A short average above the long average sets a long target. Other values set a flat target. The first target requires a full warm-up. Default periods are 10 and 20 candles.

The simulator applies the target at the following candle's open. It assumes that the prior candle is available at its close and that an order can be acted on at the next open. Historical candle data supplies no publication delay or actual opening liquidity. Buys use a cash allocation, and the simulated open determines the base quantity. Fill capacity is capped by a configurable fraction of the previous candle's volume. Unfilled quantity cancels. The simulator applies spread, slippage, fees, a base quantity step and a minimum notional.

Results include the strategy version, a SHA-256 fingerprint of normalized market inputs, the full configuration, simulated fills, fees, net return, and peak-to-trough drawdown at candle closes. Open inventory is marked at the final close. The report omits hypothetical liquidation costs on inventory that remains open.

The current model covers contiguous, archived spot candles for one market. Its results are for research. Order-book replay, measured latency and portfolio-wide benchmarks remain future work.

## Walk-forward evaluation

`POST /api/v1/backtests/walk-forward` evaluates candidate SMA periods using expanding training windows and successive, non-overlapping test windows. Candidates share simulation parameters and begin at the same warm-up boundary. Training net return selects the candidate, with ties following request order. Each test starts with fresh cash and uses preceding closes for warm-up. The benchmark attempts a buy at the start of each scored window under the same allocation, costs, and liquidity model, then holds its filled quantity.

The final window is excluded from all candidate selection. A final candidate is fitted on the development segment and scored once per request against that holdout. Rerunning the exploratory endpoint with new candidates can indirectly expose the holdout to tuning.

## Saved reports

Both backtest endpoints store the completed versioned report in local SQLite and return its read URL in the `Location` header. The ID is the SHA-256 fingerprint of the report payload. Identical reports retain the first recording time. The local API exposes no edit or deletion operation for reports and verifies the fingerprint on read. The fingerprint detects payload corruption and does not attest to the provenance of a database controlled by a local administrator.

## Separately held evaluation data

Keep the later candles outside the development database. A dataset curator prepares the JSON file and shares only its lowercase SHA-256 hash until selection is complete. The file is a JSON array using the domain `Candle` fields. Each item includes `market` with `venue`, `base` and `quote` asset IDs represented as objects, plus `source`, `interval_minutes`, `opened_at`, `received_at`, `open`, `high`, `low`, `close`, `volume`, and `trade_count`. Use exact decimal strings and UTC timestamps.

1. Run `hcs-trader freeze-research HASH --interval 60` with development candles already stored. Supply `--config FILE` to use a JSON configuration matching the walk-forward request. The default expects 100 held-out candles.
2. After freezing, obtain the committed file and run `hcs-trader score-holdout HASH FILE`. It must begin exactly where the development segment ends. The command prints the result and stores the first assessment in SQLite.

One SQLite database allows one frozen candidate and one recorded result per file hash. Reusing the exact file is idempotent. Custody of the file and protection against pre-selection access remain with the curator. A hash alone does not hide a dataset that has already been distributed.
