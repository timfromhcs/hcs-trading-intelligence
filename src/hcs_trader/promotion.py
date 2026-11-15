"""Formal strategy promotion pipeline and champion/challenger evaluation."""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from hcs_trader.backtest import BacktestResult
from hcs_trader.holdout import HoldoutResult
from hcs_trader.walk_forward import WalkForwardResult


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
