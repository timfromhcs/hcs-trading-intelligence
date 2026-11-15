"""Commitment-based holdout evaluation for a frozen research candidate."""

import hmac
import re
from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256

from pydantic import TypeAdapter, ValidationError

from hcs_trader.backtest import STRATEGY, BacktestConfig, BacktestResult, run_backtest
from hcs_trader.domain.market_data import Candle
from hcs_trader.walk_forward import WalkForwardConfig, select_candidate

MAX_HOLDOUT_CANDLES = 1000
MAX_HOLDOUT_BYTES = 1_048_576
_CANDLE_DATA = TypeAdapter(list[Candle])


@dataclass(frozen=True, slots=True)
class FrozenCandidate:
    strategy_version: str
    holdout_sha256: str
    development_sha256: str
    holdout_candles: int
    selection_config: WalkForwardConfig
    selected: BacktestConfig
    warmup: tuple[Candle, ...]


@dataclass(frozen=True, slots=True)
class HoldoutResult:
    holdout_sha256: str
    development_sha256: str
    strategy: BacktestResult
    benchmark: BacktestResult


CANDIDATE_JSON = TypeAdapter(FrozenCandidate)
RESULT_JSON = TypeAdapter(HoldoutResult)


def freeze_candidate(
    development: Sequence[Candle],
    config: WalkForwardConfig,
    holdout_sha256: str,
) -> FrozenCandidate:
    if not re.fullmatch(r"[a-f0-9]{64}", holdout_sha256):
        raise ValueError("A lowercase SHA-256 holdout commitment is required")
    if config.holdout_candles > MAX_HOLDOUT_CANDLES:
        raise ValueError("The committed holdout exceeds the supported window size")
    if len(development) < config.training_candles:
        raise ValueError("Insufficient development candles")
    selected = select_candidate(development, config)
    warmup_count = max(pair.slow for pair in config.candidates)
    return FrozenCandidate(
        STRATEGY,
        holdout_sha256,
        selected.dataset_sha256,
        config.holdout_candles,
        config,
        selected.config,
        tuple(development[-warmup_count:]),
    )


def score_holdout(candidate: FrozenCandidate, payload: bytes) -> HoldoutResult:
    if candidate.strategy_version != STRATEGY:
        raise ValueError("Frozen candidate requires its original strategy version")
    if len(payload) > MAX_HOLDOUT_BYTES:
        raise ValueError("Holdout file exceeds the supported size")
    if not hmac.compare_digest(sha256(payload).hexdigest(), candidate.holdout_sha256):
        raise ValueError("Holdout file does not match the commitment")
    try:
        holdout = _CANDLE_DATA.validate_json(payload)
    except ValidationError:
        raise ValueError("Holdout file is not a valid candle dataset") from None
    if not 1 <= len(holdout) == candidate.holdout_candles <= MAX_HOLDOUT_CANDLES:
        raise ValueError("Holdout candle count does not match the frozen candidate")
    if not candidate.warmup or candidate.warmup[-1].closed_at != holdout[0].opened_at:
        raise ValueError("Holdout must begin at the end of the development interval")
    candles = (*candidate.warmup, *holdout)
    boundary = len(candidate.warmup)
    return HoldoutResult(
        candidate.holdout_sha256,
        candidate.development_sha256,
        run_backtest(candles, candidate.selected, trade_start=boundary),
        run_backtest(
            candles, candidate.selected, trade_start=boundary, buy_and_hold=True
        ),
    )
