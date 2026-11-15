---
name: strategy-lab
version: 1.0.0
purpose: Deterministic technical indicator calculation, realistic backtesting, and walk-forward candidate evaluation
capability_tier: RESEARCH
required_tools: quant.get_indicators, market.get_candles
---

# Strategy Lab Skill

## Purpose & Triggers
- **Purpose**: Calculate mathematical indicators (RSI, MACD, SMA, EMA, ATR, Bollinger Bands) using exact Decimal arithmetic, run lookahead-free backtest simulations with realistic fees and slippage, and evaluate candidate robustness via expanding-window walk-forward analysis.
- **Triggers**: User initiates research in Strategy Lab, automated candidate generation cycle, or periodic strategy regime evaluation.

## Capability Tier & Required Tools
- **Capability Tier**: `RESEARCH` (computational analysis; no live execution authority).
- **Required MCP Tools**:
  - `quant.get_indicators`
  - `market.get_candles`

## Trusted and Untrusted Data Sources
- **Trusted Structured Data**:
  - Historical closed OHLCV candle datasets from local SQLite store.
  - Deterministic formulas with exact Decimal precision.
- **Untrusted Data**:
  - External strategy parameters proposed by third parties or unverified LLM generations (must be bounded by parameter schemas).

## Input and Output Schema
- **Inputs**:
  - `candles`: Sequence of verified OHLCV bars.
  - `strategy_type`: e.g. `RSI_TREND`, `MACD_CROSSOVER`, `VOLATILITY_BREAKOUT`.
  - `parameters`: Period lengths, entry thresholds, exit thresholds.
  - `cost_model`: Exchange fee (0.16%), bid-ask half-spread, slippage penalty.
- **Outputs**:
  - Indicator series: `RSI`, `MACD`, `MACD_Signal`, `MACD_Hist`, `SMA`, `EMA`, `ATR`, `Bollinger_Upper`, `Bollinger_Lower`.
  - Simulation metrics: `total_trades`, `win_rate`, `net_pnl`, `max_drawdown`, `profit_factor`, `sharpe_ratio`.
  - Candidate report: Canonical SHA-256 fingerprint, frozen candidate metadata, holdout pass/fail status.

## Ordered Workflow
1. Fetch closed candle series with warm-up period using `market.get_candles`.
2. Compute deterministic indicators via `quant.get_indicators`.
3. Check indicator warm-up status: discard initial bars before lookback window stabilizes.
4. Execute backtest loop strictly on next-bar open to eliminate lookahead bias.
5. Deduct exchange fees and slippage on every simulated entry and exit.
6. Run walk-forward expanding window splits to test out-of-sample persistence.
7. Output structured metrics and SHA-256 candidate artifact.

## Safe Defaults and Stop Conditions
- **Safe Defaults**: If parameters are unspecified, default to standard RSI (14 period, 30/70 thresholds) and 0.16% fee model.
- **Stop Conditions**: Terminate simulation immediately if lookahead leakage is detected (e.g. execution price matches future bar's close).

## Failure and Recovery
- If candle data contains gaps or timestamps are non-monotonic, reject simulation with `DATA_GAP_ERROR`.
- If division by zero or negative variance occurs in indicator calculation, log error and fail closed.

## Evidence and Citation
- Backtest reports must document exact `dataset_hash`, `candle_count`, `timeframe`, `fee_model`, and `parameter_fingerprint`.
- Simulated returns must never be marketed as expected live returns.

## Explicit Forbidden Actions
- **FORBIDDEN**: Promoting a candidate directly to LIVE execution.
- **FORBIDDEN**: Modifying active pre-trade risk policy limits.
- **FORBIDDEN**: Using sealed holdout data during strategy parameter tuning.
- **FORBIDDEN**: Disabling simulated fees or slippage penalties in evaluation reports.

## Acceptance Checks
- Automated tests in `tests/test_indicators.py` and `tests/test_backtest.py` pass.
- Cross-checks with pure-Python reference implementations match exact values.
- Zero lookahead test passes (future bars cannot alter past decision points).
