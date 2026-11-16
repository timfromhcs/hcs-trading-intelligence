import asyncio
import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from hcs_trader.agents.loop import CycleOutcome, LoopState, RiskVerdict, run_cycle
from hcs_trader.agents.registry import (
    FORBIDDEN_TOOLS,
    REGISTRY,
    AgentRole,
    AgentSpec,
    CandidateQuota,
    authorize,
)
from hcs_trader.data.kraken import BTC_USD, SOURCE
from hcs_trader.domain.market_data import Candle
from hcs_trader.indicators import Indicators
from hcs_trader.local_research import (
    NoneModelProvider,
    parse_proposal,
    research_report,
)
from hcs_trader.strategy import PortfolioState, SignalProposal, SmaTrendStrategy

_START = datetime(2020, 1, 1, tzinfo=UTC)


def candles(prices: list[int]) -> list[Candle]:
    out = []
    for index, value in enumerate(prices):
        opened = _START + timedelta(hours=index)
        price = Decimal(value)
        out.append(
            Candle(
                BTC_USD,
                SOURCE,
                60,
                opened,
                opened + timedelta(hours=1),
                price,
                price + 1,
                price - 1,
                price,
                Decimal(5),
                1,
            )
        )
    return out


_FLAT = PortfolioState(Decimal(1000), Decimal(0))
_STRATEGY = SmaTrendStrategy(2, 4)
_SERIES = candles([10, 11, 12, 13, 14, 15])
_NOW = _SERIES[-1].closed_at + timedelta(minutes=5)
_EVIDENCE = frozenset({"candle:a", "candle:b"})
type Gate = Callable[[SignalProposal], RiskVerdict]


def approve(_proposal: SignalProposal) -> RiskVerdict:
    return RiskVerdict(True)


def cycle(
    state: LoopState,
    gate: Gate = approve,
    *,
    series: Sequence[Candle] = _SERIES,
    now: datetime = _NOW,
    max_failures: int = 3,
) -> tuple[LoopState, CycleOutcome]:
    return run_cycle(
        state,
        run_id="r1",
        correlation_id="c1",
        strategy=_STRATEGY,
        candles=series,
        portfolio=_FLAT,
        risk_gate=gate,
        now=now,
        max_failures=max_failures,
    )


def test_cycle_previews_approved_proposal_and_is_idempotent() -> None:
    state, outcome = cycle(LoopState())
    assert outcome.status == "PREVIEW" and outcome.proposal is not None
    assert state.cursor == _SERIES[-1].closed_at and state.run_count == 1
    assert state.last_success_at == _NOW
    again, repeated = cycle(state)
    assert repeated.status == "NO_TRADE" and "already_processed" in repeated.reasons
    assert again == state
    assert LoopState.from_json(state.to_json()) == state
    assert LoopState.from_json(LoopState().to_json()) == LoopState()


def test_cycle_rejection_stale_data_and_expired_proposal() -> None:
    _, rejected = cycle(LoopState(), lambda _p: RiskVerdict(False, ("too_big",)))
    assert rejected.status == "REJECTED" and rejected.reasons == ("too_big",)
    _, stale = cycle(LoopState(), now=_NOW + timedelta(hours=10))
    assert stale.status == "NO_TRADE" and "data_quality_stale" in stale.reasons
    _, expired = cycle(LoopState(), now=_SERIES[-1].closed_at + timedelta(minutes=60))
    assert expired.status == "NO_TRADE"


def test_circuit_opens_after_repeated_gate_failures_and_blocks_trading() -> None:
    def boom(_p: SignalProposal) -> RiskVerdict:
        raise RuntimeError("gate down")

    state = LoopState()
    for index in range(3):
        series = candles([10, 11, 12, 13, 14, 15, *range(16, 16 + index)])
        now = series[-1].closed_at + timedelta(minutes=5)
        state, outcome = cycle(state, boom, series=series, now=now)
        assert outcome.status == "NO_TRADE" and outcome.reasons == ("cycle_failed",)
    assert state.circuit_open and state.consecutive_failures == 3
    _, blocked = cycle(state, series=candles([10, 11, 12, 13, 14, 15, 16]))
    assert blocked.reasons == ("circuit_open",)


def test_loop_runs_without_any_model() -> None:
    assert asyncio.run(NoneModelProvider().health()).status == "disabled"
    assert asyncio.run(NoneModelProvider().metadata()).model is None
    report = asyncio.run(research_report(Indicators(0), NoneModelProvider()))
    assert report.status == "model_disabled" and report.execution_decision == "NO_TRADE"
    assert cycle(LoopState())[1].status == "PREVIEW"


def test_registry_denies_by_default_and_never_grants_forbidden_tools() -> None:
    assert set(REGISTRY) == set(AgentRole)
    for role, spec in REGISTRY.items():
        assert not spec.allowed_tools & FORBIDDEN_TOOLS
        for tool in FORBIDDEN_TOOLS:
            assert not authorize(role, tool)
        assert not authorize(role, "unknown.tool")
    assert authorize(AgentRole.EXECUTION, "execution.submit_validated")
    assert not authorize(AgentRole.QUANT_RESEARCH, "execution.submit_validated")
    with pytest.raises(ValueError, match="forbidden"):
        AgentSpec(AgentRole.NEWS, frozenset({"wallet.withdraw"}), "bad")
    quota = CandidateQuota(2)
    assert quota.allows(1) and not quota.allows(2) and not quota.allows(-1)


def _json(**overrides: object) -> str:
    base: dict[str, object] = {
        "action": "LONG",
        "thesis": "Trend is up.",
        "uncertainties": ["Thin liquidity."],
        "citations": [{"evidence_id": "candle:a"}],
    }
    return json.dumps({**base, **overrides})


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (None, "missing_or_oversized"),
        ("x" * 9000, "missing_or_oversized"),
        ("not json", "malformed_output"),
        (_json(extra="field"), "malformed_output"),
        (_json(action="SELL_EVERYTHING"), "malformed_output"),
        (_json(quantity="5"), "malformed_output"),
        (_json(citations=[{"evidence_id": "candle:zzz"}]), "unknown_evidence_id"),
        (_json(citations=[]), "uncited_trade_action"),
        (_json(action="NO_TRADE", citations=[]), "missing_no_trade_reason"),
    ],
)
def test_malformed_system2_output_becomes_no_trade(
    raw: str | None, reason: str
) -> None:
    decision = parse_proposal(raw, _EVIDENCE)
    assert not decision.valid
    assert decision.proposal.action == "NO_TRADE"
    assert decision.proposal.no_trade_reason == reason


def test_valid_system2_proposal_is_accepted_with_known_citations() -> None:
    decision = parse_proposal(_json(candidate_suggestion="Try RSI(10)."), _EVIDENCE)
    assert decision.valid and decision.proposal.action == "LONG"
    no_trade = parse_proposal(
        _json(action="NO_TRADE", citations=[], no_trade_reason="Choppy."), _EVIDENCE
    )
    assert no_trade.valid and no_trade.proposal.no_trade_reason == "Choppy."
    assert _NOW.tzinfo is UTC
