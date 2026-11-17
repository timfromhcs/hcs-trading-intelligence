# Strategy research

This document defines how a strategy moves from an idea to bounded use. It follows AGENTS.md section 5. Nothing here promises a return. Passing a stage means only that the stage's evidence exists.

## Principles

- Indicators and signals are deterministic code on `Decimal` values. A model may explain a result but cannot size, approve or promote anything.
- `NO_TRADE` is a valid, expected outcome. Missing, stale or gapped data produces `NO_TRADE`.
- The bundled SMA trend strategy is educational. It is not profitable and not approved for live use.
- Candidate versions are immutable. A change is a new candidate.

## Promotion ladder

`RESEARCH -> UNIT_TESTED -> BACKTESTED -> WALK_FORWARD_PASSED -> PAPER_SHADOW -> PAPER_APPROVED -> LIVE_CANARY -> LIVE_BOUNDED`

| Stage | Evidence required |
|---|---|
| `BACKTESTED` | A saved backtest report whose dataset and SMA windows match the candidate |
| `WALK_FORWARD_PASSED` | A saved expanding-window report bound to the same development data and settings |
| `PAPER_SHADOW` | A separately committed and scored holdout for the same development dataset and settings |
| `PAPER_APPROVED` | Fills from a persisted post-promotion shadow replay or forward shadow observation |
| `LIVE_CANARY` | An interactive, expiring authorization for one bounded Kraken BTC/USD order. Later stages stay blocked |
| `LIVE_BOUNDED` | Not reachable yet |

A promotion is rejected for stale candidate updates, a missing or mismatched report, contaminated holdout data, or a failed gate. The promotion code lives in `src/hcs_trader/promotion.py` and is reachable through `hcs-trader strategy-promote` and the REST promote endpoint. Do not tune a live strategy to recent losses.

## Holdout rules

1. Choose the development window and freeze the candidate with `hcs-trader freeze-research` before reading any holdout data.
2. The holdout file's commitment hash is recorded at freezing. Scoring the holdout with `score-holdout` checks that hash.
3. Look at the holdout once. A second tuning round on the same holdout invalidates it, and a new holdout is needed.
4. Dataset confidentiality before freezing remains the curator's responsibility. The software cannot prove you did not look.
5. Economic data must carry its source snapshot date. Revised values must not enter a decision made before their release.

## Costs and bias controls

Backtests include fees, spread, slippage and partial fills, use no future candles, and reset indicators after gaps. See [BACKTEST.md](BACKTEST.md) for the assumptions and [INDICATORS.md](INDICATORS.md) for the definitions. Report net-of-cost return against a cost-matched buy-and-hold benchmark, along with drawdown and trade count. A handful of trades is not evidence of skill.

## Paper and shadow observation

Shadow replays store immutable inputs and run and fill IDs. Forward shadow observation (`strategy-shadow-run`, `strategy-shadow-watch`, `strategy-shadow-history`) records outcomes on candles that arrive after promotion. Live behaviour that diverges from paper behaviour is a reason to stop, not to adjust the gate.

## Learning loops

Any future learning agent may only propose new candidates and reports. It may not change active strategies or risk limits, enlarge size after wins, or train on a sealed holdout.
