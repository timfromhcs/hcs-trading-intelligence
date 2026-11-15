"""Chronological strategy selection and out-of-sample evaluation."""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal

from hcs_trader.backtest import BacktestConfig, BacktestResult, run_backtest
from hcs_trader.domain.market_data import Candle


@dataclass(frozen=True, slots=True)
class PeriodPair:
    fast: int
    slow: int


@dataclass(frozen=True, slots=True)
class WalkForwardConfig:
    training_candles: int = 200
    test_candles: int = 50
    holdout_candles: int = 100
    candidates: tuple[PeriodPair, ...] = (
        PeriodPair(5, 10),
        PeriodPair(10, 20),
        PeriodPair(15, 30),
    )
    simulation: BacktestConfig = BacktestConfig()

    def __post_init__(self) -> None:
        if not 1 <= len(self.candidates) <= 8 or len(set(self.candidates)) != len(
            self.candidates
        ):
            raise ValueError("Provide one to eight distinct candidate period pairs")
        if min(self.training_candles, self.test_candles, self.holdout_candles) < 1:
            raise ValueError("Window sizes must be positive")
        for pair in self.candidates:
            if not 2 <= pair.fast < pair.slow <= 200:
                raise ValueError("Require 2 <= fast < slow <= 200")
        if self.training_candles <= max(pair.slow for pair in self.candidates):
            raise ValueError(
                "Training must contain an execution candle after every warm-up"
            )


@dataclass(frozen=True, slots=True)
class Evaluation:
    started_at: datetime
    ended_at: datetime
    evaluated_candles: int
    selected: PeriodPair
    selection_dataset_sha256: str
    training_return: Decimal
    strategy: BacktestResult
    benchmark: BacktestResult


@dataclass(frozen=True, slots=True)
class WalkForwardResult:
    config: WalkForwardConfig
    folds: tuple[Evaluation, ...]
    holdout: Evaluation


def walk_forward(
    candles: Sequence[Candle], config: WalkForwardConfig
) -> WalkForwardResult:
    test_size = config.test_candles
    boundary = len(candles) - config.holdout_candles
    if boundary < config.training_candles + test_size:
        raise ValueError(
            "Need enough candles for training, a test window and the final holdout"
        )

    warmup = max(pair.slow for pair in config.candidates)
    development = candles[:boundary]
    folds = []
    for start in range(
        config.training_candles, len(development) - test_size + 1, test_size
    ):
        winner = select_candidate(development[:start], config)
        folds.append(_evaluate(development, start, start + test_size, winner, warmup))

    # The final fit only receives development data. There is no refit after scoring.
    final = select_candidate(development, config)
    holdout = _evaluate(candles, boundary, len(candles), final, warmup)
    return WalkForwardResult(config, tuple(folds), holdout)


def select_candidate(
    training: Sequence[Candle], config: WalkForwardConfig
) -> BacktestResult:
    warmup = max(pair.slow for pair in config.candidates)
    results = (
        run_backtest(
            training,
            replace(config.simulation, fast_period=pair.fast, slow_period=pair.slow),
            trade_start=warmup,
        )
        for pair in config.candidates
    )
    # max retains the first candidate on a tie.
    return max(results, key=lambda result: result.net_return)


def _evaluate(
    candles: Sequence[Candle],
    start: int,
    end: int,
    winner: BacktestResult,
    warmup: int,
) -> Evaluation:
    data = candles[start - warmup : end]
    return Evaluation(
        candles[start].opened_at,
        candles[end - 1].closed_at,
        end - start,
        PeriodPair(winner.config.fast_period, winner.config.slow_period),
        winner.dataset_sha256,
        winner.net_return,
        run_backtest(data, winner.config, trade_start=warmup),
        run_backtest(data, winner.config, trade_start=warmup, buy_and_hold=True),
    )
