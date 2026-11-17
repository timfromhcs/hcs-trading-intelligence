"""Deterministic System 1 loop: strategy -> risk gate -> preview.

The loop never signs, never calls a model, and never submits an order; an
approved proposal only becomes a PREVIEW outcome for a downstream, separately
authorized step. It works with no model installed. State is a small immutable
value that survives restarts and makes cycles idempotent.
"""

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Literal

from hcs_trader.domain.market_data import Candle, DataQuality
from hcs_trader.strategy import (
    Direction,
    MarketState,
    PortfolioState,
    SignalProposal,
    Strategy,
    is_expired,
)

type Status = Literal["NO_TRADE", "REJECTED", "PREVIEW"]


@dataclass(frozen=True, slots=True)
class RiskVerdict:
    approved: bool
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LoopState:
    cursor: datetime | None = None
    run_count: int = 0
    last_success_at: datetime | None = None
    consecutive_failures: int = 0
    circuit_open: bool = False

    def to_json(self) -> str:
        def stamp(value: datetime | None) -> str | None:
            return value.isoformat() if value else None

        return json.dumps(
            {
                "cursor": stamp(self.cursor),
                "run_count": self.run_count,
                "last_success_at": stamp(self.last_success_at),
                "consecutive_failures": self.consecutive_failures,
                "circuit_open": self.circuit_open,
            },
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, raw: str) -> "LoopState":
        data = json.loads(raw)

        def parse(value: str | None) -> datetime | None:
            return datetime.fromisoformat(value) if value else None

        return cls(
            parse(data["cursor"]),
            int(data["run_count"]),
            parse(data["last_success_at"]),
            int(data["consecutive_failures"]),
            bool(data["circuit_open"]),
        )


@dataclass(frozen=True, slots=True)
class CycleOutcome:
    run_id: str
    correlation_id: str
    status: Status
    reasons: tuple[str, ...]
    proposal: SignalProposal | None = None


def run_cycle(
    state: LoopState,
    *,
    run_id: str,
    correlation_id: str,
    strategy: Strategy,
    candles: Sequence[Candle],
    portfolio: PortfolioState,
    risk_gate: Callable[[SignalProposal], RiskVerdict],
    now: datetime,
    max_age_candles: int = 2,
    max_failures: int = 3,
) -> tuple[LoopState, CycleOutcome]:
    def done(
        new: LoopState,
        status: Status,
        *reasons: str,
        proposal: SignalProposal | None = None,
    ) -> tuple[LoopState, CycleOutcome]:
        return new, CycleOutcome(run_id, correlation_id, status, reasons, proposal)

    if state.circuit_open:
        return done(state, "NO_TRADE", "circuit_open")
    if not candles:
        return done(state, "NO_TRADE", "no_candles")
    latest = candles[-1]
    if state.cursor is not None and latest.closed_at <= state.cursor:
        # Idempotent restart: this candle was already processed.
        return done(state, "NO_TRADE", "already_processed")

    window = timedelta(minutes=latest.interval_minutes) * max_age_candles
    quality = (
        DataQuality.FRESH
        if timedelta(0) <= now - latest.closed_at <= window
        else DataQuality.STALE
    )
    advanced = replace(state, cursor=latest.closed_at, run_count=state.run_count + 1)
    try:
        market_state = MarketState(
            latest.market, latest.interval_minutes, tuple(candles), quality
        )
        proposal = strategy.evaluate(market_state, portfolio)
        if proposal.direction is Direction.NO_TRADE:
            return done(
                _success(advanced, now),
                "NO_TRADE",
                *proposal.reasons,
                proposal=proposal,
            )
        if is_expired(proposal, now):
            return done(_success(advanced, now), "NO_TRADE", "proposal_expired")
        verdict = risk_gate(proposal)
    except Exception:  # noqa: BLE001 - any fault fails closed and is counted
        failures = state.consecutive_failures + 1
        failed = replace(
            advanced,
            consecutive_failures=failures,
            circuit_open=failures >= max_failures,
        )
        return done(failed, "NO_TRADE", "cycle_failed")
    if not verdict.approved:
        return done(
            _success(advanced, now),
            "REJECTED",
            *(verdict.reasons or ("risk_rejected",)),
            proposal=proposal,
        )
    return done(_success(advanced, now), "PREVIEW", proposal=proposal)


def _success(state: LoopState, now: datetime) -> LoopState:
    return replace(state, last_success_at=now, consecutive_failures=0)
