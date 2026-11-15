"""Formal strategy promotion pipeline and champion/challenger evaluation."""

import asyncio
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from hcs_trader.backtest import BacktestResult, run_backtest
from hcs_trader.data.kraken import BTC_USD, SOURCE
from hcs_trader.domain.market_data import Candle
from hcs_trader.domain.paper import PaperRejected
from hcs_trader.holdout import CANDIDATE_JSON, RESULT_JSON, HoldoutResult
from hcs_trader.walk_forward import PeriodPair, WalkForwardResult


class CandidateNotFound(ValueError):
    """Raised when a strategy candidate does not exist in storage."""


class PromotionRejected(ValueError):
    """Raised when a promotion gate or evidence check rejects transition."""


class ReportPayload(BaseModel):
    schema_version: Literal[1] = 1
    report: BacktestResult | WalkForwardResult


class PromotionStage(StrEnum):
    RESEARCH = "RESEARCH"
    UNIT_TESTED = "UNIT_TESTED"
    BACKTESTED = "BACKTESTED"
    WALK_FORWARD_PASSED = "WALK_FORWARD_PASSED"
    PAPER_SHADOW = "PAPER_SHADOW"
    PAPER_APPROVED = "PAPER_APPROVED"
    LIVE_CANARY = "LIVE_CANARY"
    LIVE_BOUNDED = "LIVE_BOUNDED"


PROMOTION_LADDER: tuple[PromotionStage, ...] = (
    PromotionStage.RESEARCH,
    PromotionStage.UNIT_TESTED,
    PromotionStage.BACKTESTED,
    PromotionStage.WALK_FORWARD_PASSED,
    PromotionStage.PAPER_SHADOW,
    PromotionStage.PAPER_APPROVED,
    PromotionStage.LIVE_CANARY,
    PromotionStage.LIVE_BOUNDED,
)


class StrategyCandidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    candidate_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=64)
    version: str = Field(min_length=1, max_length=32)
    stage: PromotionStage = PromotionStage.RESEARCH
    parameters: dict[str, Any] = Field(default_factory=dict)
    dataset_hash: str = Field(min_length=1, max_length=64)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class PromotionGateResult:
    candidate_id: str
    from_stage: PromotionStage
    to_stage: PromotionStage
    passed: bool
    reasons: list[str]
    evaluated_at: datetime


@dataclass(frozen=True, slots=True)
class ComparisonMetrics:
    net_return: Decimal
    max_drawdown: Decimal
    fills: int

    def __post_init__(self) -> None:
        if not self.net_return.is_finite() or self.net_return < -1:
            raise ValueError("Net return must be finite and at least -100%")
        if not self.max_drawdown.is_finite() or not 0 <= self.max_drawdown <= 1:
            raise ValueError("Max drawdown must be between 0 and 1")
        if self.fills < 0:
            raise ValueError("Fill count must be nonnegative")

    @classmethod
    def from_backtest(cls, result: BacktestResult) -> "ComparisonMetrics":
        return cls(result.net_return, result.max_drawdown, len(result.fills))


@dataclass(frozen=True, slots=True)
class ChampionChallengerVerdict:
    champion_id: str
    challenger_id: str
    challenger_wins: bool
    reasons: list[str]
    metrics: dict[str, str]


def next_promotion_stage(current: PromotionStage) -> PromotionStage | None:
    try:
        idx = PROMOTION_LADDER.index(current)
        if idx + 1 < len(PROMOTION_LADDER):
            return PROMOTION_LADDER[idx + 1]
    except ValueError:
        pass
    return None


def evaluate_promotion_gate(
    candidate: StrategyCandidate,
    target_stage: PromotionStage,
    *,
    backtest: BacktestResult | None = None,
    walk_forward: WalkForwardResult | None = None,
    holdout: HoldoutResult | None = None,
    shadow_fills: int = 0,
    is_live_enabled: bool = False,
) -> PromotionGateResult:
    reasons: list[str] = []
    now = datetime.now(UTC)
    expected_next = next_promotion_stage(candidate.stage)

    if expected_next != target_stage:
        reasons.append(
            f"Invalid transition from {candidate.stage.value} to {target_stage.value}; "
            f"expected next stage is {expected_next.value if expected_next else 'None'}"
        )
        return PromotionGateResult(
            candidate_id=candidate.candidate_id,
            from_stage=candidate.stage,
            to_stage=target_stage,
            passed=False,
            reasons=reasons,
            evaluated_at=now,
        )

    if target_stage == PromotionStage.UNIT_TESTED:
        fast = candidate.parameters.get("fast")
        slow = candidate.parameters.get("slow")
        if not (
            isinstance(fast, int) and isinstance(slow, int) and 2 <= fast < slow <= 200
        ):
            reasons.append("Parameters must contain 2 <= fast < slow <= 200 integers")

    elif target_stage == PromotionStage.BACKTESTED:
        if backtest is None:
            reasons.append("BacktestResult is required for BACKTESTED stage")
        else:
            if len(backtest.fills) < 1:
                reasons.append("Backtest produced zero trades")
            if backtest.max_drawdown > Decimal("0.25"):
                reasons.append(
                    f"Max drawdown {backtest.max_drawdown:.2%} exceeds "
                    f"25.00% risk limit"
                )

    elif target_stage == PromotionStage.WALK_FORWARD_PASSED:
        if walk_forward is None:
            reasons.append(
                "WalkForwardResult is required for WALK_FORWARD_PASSED stage"
            )
        elif not walk_forward.folds:
            reasons.append("Walk-forward evaluation series is empty")
        else:
            results = (*walk_forward.folds, walk_forward.holdout)
            if not any(fold.strategy.fills for fold in walk_forward.folds):
                reasons.append("Walk-forward folds produced zero fills")
            if not walk_forward.holdout.strategy.fills:
                reasons.append("Holdout produced zero fills")
            if any(item.strategy.max_drawdown > Decimal("0.25") for item in results):
                reasons.append("Out-of-sample drawdown exceeds the 25.00% risk limit")

    elif target_stage == PromotionStage.PAPER_SHADOW:
        if holdout is None:
            reasons.append("A scored holdout commitment is required for PAPER_SHADOW")
        elif not holdout.strategy.fills:
            reasons.append("Committed holdout produced zero fills")
        elif holdout.strategy.max_drawdown > Decimal("0.25"):
            reasons.append("Committed holdout drawdown exceeds the 25.00% risk limit")

    elif target_stage == PromotionStage.PAPER_APPROVED:
        if shadow_fills < 5:
            reasons.append(
                f"Requires at least 5 verified shadow paper fills "
                f"(observed {shadow_fills})"
            )

    elif target_stage in (PromotionStage.LIVE_CANARY, PromotionStage.LIVE_BOUNDED):
        if not is_live_enabled:
            reasons.append(
                "Live trading execution requires explicit local operator enablement"
            )

    passed = len(reasons) == 0
    return PromotionGateResult(
        candidate_id=candidate.candidate_id,
        from_stage=candidate.stage,
        to_stage=target_stage,
        passed=passed,
        reasons=reasons,
        evaluated_at=now,
    )


def compare_champion_challenger(
    champion_id: str,
    champion_result: BacktestResult,
    challenger_id: str,
    challenger_result: BacktestResult,
) -> ChampionChallengerVerdict:
    return compare_strategy_metrics(
        champion_id,
        ComparisonMetrics.from_backtest(champion_result),
        challenger_id,
        ComparisonMetrics.from_backtest(challenger_result),
    )


def compare_strategy_metrics(
    champion_id: str,
    champion_result: ComparisonMetrics,
    challenger_id: str,
    challenger_result: ComparisonMetrics,
) -> ChampionChallengerVerdict:
    reasons: list[str] = []
    champ_return = champion_result.net_return
    chall_return = challenger_result.net_return
    champ_dd = champion_result.max_drawdown
    chall_dd = challenger_result.max_drawdown
    champ_trades = champion_result.fills
    chall_trades = challenger_result.fills

    metrics = {
        "champion_net_return": str(champ_return),
        "challenger_net_return": str(chall_return),
        "champion_max_drawdown": str(champ_dd),
        "challenger_max_drawdown": str(chall_dd),
        "champion_trades": str(champ_trades),
        "challenger_trades": str(chall_trades),
    }

    if chall_trades < 1:
        reasons.append("Challenger produced zero trades")
    if chall_return < champ_return:
        reasons.append(
            f"Challenger net return ({chall_return:.2%}) underperformed champion "
            f"({champ_return:.2%})"
        )
    if chall_dd > champ_dd:
        reasons.append(
            f"Challenger max drawdown ({chall_dd:.2%}) exceeded champion "
            f"({champ_dd:.2%})"
        )

    wins = len(reasons) == 0
    if wins:
        reasons.append(
            "Challenger matched or exceeded champion return with equal or "
            "lower drawdown"
        )

    return ChampionChallengerVerdict(
        champion_id=champion_id,
        challenger_id=challenger_id,
        challenger_wins=wins,
        reasons=reasons,
        metrics=metrics,
    )


def get_report_payload(store: Any, report_id: str) -> ReportPayload:
    """Retrieve and validate the integrity of a stored backtest/walk-forward report."""
    if not isinstance(report_id, str) or not re.fullmatch(r"[a-f0-9]{64}", report_id):
        raise PromotionRejected("Invalid report ID format")
    record = store.backtest_report(report_id)
    if record is None:
        raise PromotionRejected("Backtest report not found")
    if sha256(record.payload.encode("utf-8")).hexdigest() != report_id:
        raise PromotionRejected("Backtest report integrity check failed")
    try:
        return ReportPayload.model_validate_json(record.payload)
    except ValidationError as error:
        raise PromotionRejected("Backtest report format is unsupported") from error


def get_candidate_backtest_report(
    store: Any, candidate: StrategyCandidate
) -> BacktestResult:
    """Retrieve and verify the base backtest report for a strategy candidate."""
    report_id = candidate.parameters.get("backtest_report_id")
    if not isinstance(report_id, str) or not re.fullmatch(r"[a-f0-9]{64}", report_id):
        raise PromotionRejected("Candidate needs a saved backtest report ID")
    report = get_report_payload(store, report_id).report
    if (
        not isinstance(report, BacktestResult)
        or report.dataset_sha256 != candidate.dataset_hash
        or report.config.fast_period != candidate.parameters.get("fast")
        or report.config.slow_period != candidate.parameters.get("slow")
    ):
        raise PromotionRejected("Backtest report does not match the candidate")
    return report


def get_latest_shadow_run(store: Any, candidate_id: str) -> tuple[str, BacktestResult]:
    """Retrieve the latest verified paper shadow run for a candidate."""
    saved = store.latest_shadow_run(candidate_id)
    if saved is None:
        raise PromotionRejected("A persisted paper shadow run is required")
    run_id, payload = saved
    if sha256(payload.encode("utf-8")).hexdigest() != run_id:
        raise PromotionRejected("Invalid shadow run payload")
    try:
        parsed = ReportPayload.model_validate_json(payload).report
    except ValidationError as error:
        raise PromotionRejected("Invalid shadow run report format") from error
    if not isinstance(parsed, BacktestResult):
        raise PromotionRejected("Shadow run report must be a BacktestResult")
    return run_id, parsed


def list_shadow_runs_for_candidate(
    store: Any, candidate_id: str, limit: int = 25
) -> list[tuple[str, datetime, BacktestResult]]:
    """Retrieve recent verified shadow runs for a candidate ordered newest first."""
    records = store.list_shadow_runs(candidate_id, limit=limit)
    results: list[tuple[str, datetime, BacktestResult]] = []
    for run_id, evaluated_until, payload in records:
        if sha256(payload.encode("utf-8")).hexdigest() != run_id:
            continue
        try:
            parsed = ReportPayload.model_validate_json(payload).report
            if isinstance(parsed, BacktestResult):
                evaluated_dt = (
                    evaluated_until
                    if isinstance(evaluated_until, datetime)
                    else datetime.fromisoformat(evaluated_until)
                )
                results.append((run_id, evaluated_dt, parsed))
        except (ValidationError, ValueError):
            continue
    return results


def execute_shadow_run(
    store: Any,
    candidate_id: str,
    candles: Sequence[Candle] | None = None,
    at: datetime | None = None,
) -> tuple[str, BacktestResult]:
    """Execute and persist a paper shadow replay for a candidate in PAPER_SHADOW."""
    candidate = store.get_strategy_candidate(candidate_id)
    if candidate is None:
        raise CandidateNotFound(f"Strategy candidate '{candidate_id}' not found")
    if candidate.stage != PromotionStage.PAPER_SHADOW:
        raise PromotionRejected("Candidate must be in PAPER_SHADOW")
    source = get_candidate_backtest_report(store, candidate)
    now = at or datetime.now(UTC)
    boundary = max(candidate.updated_at, source.dataset_closed_at)
    available_candles = (
        candles
        if candles is not None
        else store.candle_history(BTC_USD, SOURCE, 60, limit=1000)
    )
    filtered = [c for c in available_candles if c.closed_at <= now]
    start = next(
        (
            index
            for index, candle in enumerate(filtered)
            if candle.opened_at >= boundary
        ),
        len(filtered),
    )
    warmup = source.config.slow_period
    if (
        not warmup <= start < len(filtered)
        or filtered[start].opened_at - filtered[start - 1].opened_at
        != filtered[start].closed_at - filtered[start].opened_at
        or filtered[start - 1].opened_at >= boundary
    ):
        raise PromotionRejected(
            "Need contiguous hourly candles across the shadow boundary"
        )
    try:
        result = run_backtest(
            filtered[start - warmup :], source.config, trade_start=warmup
        )
    except ValueError as error:
        raise PromotionRejected(str(error)) from error
    payload = ReportPayload(report=result).model_dump_json()
    run_id = sha256(payload.encode("utf-8")).hexdigest()
    saved = store.record_shadow_run(
        candidate, run_id, result.dataset_closed_at, payload
    )
    if not saved:
        raise PromotionRejected("Candidate changed during shadow simulation")
    return run_id, result


def run_forward_shadow_observation(
    store: Any,
    candidate_id: str | None = None,
    at: datetime | None = None,
) -> list[tuple[str, str, BacktestResult]]:
    """Evaluates forward shadow simulation for candidates in PAPER_SHADOW.

    Returns list of (candidate_id, run_id, BacktestResult) for newly recorded runs.
    """
    if candidate_id is not None:
        cand = store.get_strategy_candidate(candidate_id)
        if cand is None:
            raise CandidateNotFound(f"Strategy candidate '{candidate_id}' not found")
        candidates = [cand]
    else:
        candidates = store.list_strategy_candidates(stage=PromotionStage.PAPER_SHADOW)

    now = at or datetime.now(UTC)
    candles = store.candle_history(BTC_USD, SOURCE, 60, limit=1000)
    results: list[tuple[str, str, BacktestResult]] = []
    for cand in candidates:
        if cand.stage != PromotionStage.PAPER_SHADOW:
            continue
        try:
            run_id, result = execute_shadow_run(
                store, cand.candidate_id, candles=candles, at=now
            )
            results.append((cand.candidate_id, run_id, result))
        except (PromotionRejected, PaperRejected):
            continue
    return results


async def watch_shadow(
    store: Any,
    candidate_id: str | None = None,
    every_seconds: int = 3600,
) -> None:
    """Foreground scheduled observer for forward shadow execution."""
    store.initialize()
    while True:
        results = run_forward_shadow_observation(store, candidate_id)
        for cid, run_id, result in results:
            print(
                f"[{datetime.now(UTC).isoformat()}] Shadow run for '{cid}': "
                f"run_id={run_id[:12]} fills={len(result.fills)} "
                f"net_return={result.net_return:.2%} dd={result.max_drawdown:.2%}",
                flush=True,
            )
        await asyncio.sleep(every_seconds)


def promote_strategy_candidate(
    store: Any,
    candidate_id: str,
    target_stage: PromotionStage | None = None,
) -> tuple[StrategyCandidate, PromotionGateResult]:
    candidate = store.get_strategy_candidate(candidate_id)
    if candidate is None:
        raise CandidateNotFound(f"Strategy candidate '{candidate_id}' not found")

    if target_stage is None:
        expected = next_promotion_stage(candidate.stage)
        if expected is None:
            raise PromotionRejected(
                f"Candidate '{candidate_id}' is already at terminal stage "
                f"{candidate.stage.value}"
            )
        target_stage = expected

    backtest_evidence: BacktestResult | None = None
    walk_forward_evidence: WalkForwardResult | None = None
    holdout_evidence: HoldoutResult | None = None
    shadow_fills = 0
    shadow_run_id: str | None = None

    needs_backtest = (candidate.stage, target_stage) in (
        (PromotionStage.UNIT_TESTED, PromotionStage.BACKTESTED),
        (PromotionStage.BACKTESTED, PromotionStage.WALK_FORWARD_PASSED),
        (PromotionStage.WALK_FORWARD_PASSED, PromotionStage.PAPER_SHADOW),
    )
    if needs_backtest:
        report = get_candidate_backtest_report(store, candidate)
        backtest_evidence = report

        if target_stage == PromotionStage.WALK_FORWARD_PASSED:
            walk_id = candidate.parameters.get("walk_forward_report_id")
            if not isinstance(walk_id, str) or not re.fullmatch(
                r"[a-f0-9]{64}", walk_id
            ):
                raise PromotionRejected(
                    "Candidate needs a saved walk-forward report ID"
                )
            walk = get_report_payload(store, walk_id).report
            pair = PeriodPair(report.config.fast_period, report.config.slow_period)
            if (
                not isinstance(walk, WalkForwardResult)
                or walk.config.candidates != (pair,)
                or walk.holdout.selection_dataset_sha256 != report.dataset_sha256
                or walk.holdout.started_at != report.dataset_closed_at
                or walk.holdout.strategy.config != report.config
            ):
                raise PromotionRejected(
                    "Walk-forward report does not match the candidate"
                )
            walk_forward_evidence = walk

        if target_stage == PromotionStage.PAPER_SHADOW:
            commitment = candidate.parameters.get("holdout_commitment")
            if not isinstance(commitment, str) or not re.fullmatch(
                r"[a-f0-9]{64}", commitment
            ):
                raise PromotionRejected("Candidate needs a scored holdout commitment")
            scored = store.scored_holdout(commitment)
            if scored is None:
                raise PromotionRejected("Committed holdout has not been scored")
            try:
                frozen = CANDIDATE_JSON.validate_json(scored[0])
                outcome = RESULT_JSON.validate_json(scored[1])
            except ValidationError as error:
                raise PromotionRejected("Stored holdout format is invalid") from error
            if (
                frozen.holdout_sha256 != commitment
                or outcome.holdout_sha256 != commitment
                or frozen.development_sha256 != report.dataset_sha256
                or outcome.development_sha256 != report.dataset_sha256
                or frozen.strategy_version != report.strategy
                or frozen.selected != report.config
                or outcome.strategy.config != report.config
                or outcome.strategy.strategy != report.strategy
                or outcome.strategy.dataset_closed_at > datetime.now(UTC)
                or not frozen.warmup
                or frozen.warmup[-1].closed_at != report.dataset_closed_at
            ):
                raise PromotionRejected(
                    "Committed holdout does not match the candidate"
                )
            holdout_evidence = outcome

    if (
        candidate.stage == PromotionStage.PAPER_SHADOW
        and target_stage == PromotionStage.PAPER_APPROVED
    ):
        shadow_id, shadow_report = get_latest_shadow_run(store, candidate.candidate_id)
        if shadow_report.max_drawdown > Decimal("0.25"):
            raise PromotionRejected("Paper shadow drawdown exceeds the 25% risk limit")
        shadow_fills = len(shadow_report.fills)
        shadow_run_id = shadow_id

    is_live_enabled = (
        target_stage == PromotionStage.LIVE_CANARY
        and store.has_spot_canary_approval(candidate.candidate_id, datetime.now(UTC))
    )

    verdict = evaluate_promotion_gate(
        candidate,
        target_stage,
        backtest=backtest_evidence,
        walk_forward=walk_forward_evidence,
        holdout=holdout_evidence,
        shadow_fills=shadow_fills,
        is_live_enabled=is_live_enabled,
    )
    if not verdict.passed:
        raise PromotionRejected(
            f"Promotion gate rejected: {'; '.join(verdict.reasons)}"
        )

    updated = store.update_candidate_stage(
        candidate,
        target_stage,
        verdict.evaluated_at,
        shadow_run_id=shadow_run_id,
    )
    if updated is None:
        raise PromotionRejected("Candidate changed during promotion")
    return updated, verdict
