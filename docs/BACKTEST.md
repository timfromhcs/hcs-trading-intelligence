# SMA trend research backtest

The initial research strategy evaluates short and long simple moving averages using completed BTC/USD closes. A short average above the long average sets a long target. Other values set a flat target. The first target requires a full warm-up. Default periods are 10 and 20 candles.

The simulator applies the target at the following candle's open. It assumes that the prior candle is available at its close and that an order can be acted on at the next open. Historical candle data supplies no publication delay or actual opening liquidity. Buys use a cash allocation, and the simulated open determines the base quantity. Fill capacity is capped by a configurable fraction of the previous candle's volume. Unfilled quantity cancels. The simulator applies spread, slippage, fees, a base quantity step and a minimum notional.

Results include the strategy version, a SHA-256 fingerprint of normalized market inputs, the full configuration, simulated fills, fees, net return, and peak-to-trough drawdown at candle closes. Open inventory is marked at the final close. The report omits hypothetical liquidation costs on inventory that remains open.

The current model covers contiguous, archived spot candles for one market. Its results are for research. Order-book replay, measured latency and portfolio-wide benchmarks remain future work.

## Walk-forward evaluation

`POST /api/v1/backtests/walk-forward` evaluates candidate SMA periods using expanding training windows and successive, non-overlapping test windows. Candidates share simulation parameters and begin at the same warm-up boundary. Training net return selects the candidate, with ties following request order. Each test starts with fresh cash and uses preceding closes for warm-up. The benchmark attempts a buy at the start of each scored window under the same allocation, costs, and liquidity model, then holds its filled quantity.

The final window is excluded from all candidate selection. A final candidate is fitted on the development segment and scored once per request against that holdout. Rerunning the endpoint with new candidates can indirectly expose the holdout to tuning. Research promotion will require a separately managed sealed dataset and immutable experiment record.
