"""System 1: deterministic, immutable strategies emitting typed proposals.

A strategy never sizes or authorizes a trade. It only proposes a direction with
invalidation and evidence; the deterministic risk engine makes the final call
(AGENTS 5.2, 5.4). Nothing here imports a model, network client or signer.
"""

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from hcs_trader.domain.assets import MarketId
from hcs_trader.domain.market_data import Candle, DataQuality
from hcs_trader.indicators import rsi, sma

_ZERO = Decimal(0)
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class Direction(StrEnum):
    LONG = "LONG"  # open or add to a spot long
    EXIT = "EXIT"  # close the existing spot long
    NO_TRADE = "NO_TRADE"


@dataclass(frozen=True, slots=True)
class WarmupRequirements:
    candles: int


@dataclass(frozen=True, slots=True)
class MarketState:
    market: MarketId
    interval_minutes: int
    candles: tuple[Candle, ...]
    quality: DataQuality

    def __post_init__(self) -> None:
        if self.interval_minutes <= 0:
            raise ValueError("interval_minutes must be positive")
        for candle in self.candles:
            if (
                candle.market != self.market
                or candle.interval_minutes != self.interval_minutes
            ):
                raise ValueError("MarketState candles must match market and interval")

    @property
    def as_of(self) -> datetime | None:
        return self.candles[-1].closed_at if self.candles else None


@dataclass(frozen=True, slots=True)
class PortfolioState:
    cash: Decimal
    position_quantity: Decimal

    def __post_init__(self) -> None:
        for value in (self.cash, self.position_quantity):
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise ValueError("Portfolio values must be non-negative Decimals")


@dataclass(frozen=True, slots=True)
class SignalProposal:
    """A proposal only. ``confidence`` is an uncalibrated score, not a probability."""

    strategy_id: str
    strategy_version: str
    direction: Direction
    market: MarketId
    timeframe_minutes: int
    entry_conditions: tuple[str, ...]
    invalidation: Decimal | None
    confidence: Decimal
    evidence_ids: tuple[str, ...]
    data_quality: DataQuality
    created_at: datetime
    expires_at: datetime
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for stamp in (self.created_at, self.expires_at):
            if stamp.tzinfo is None or stamp.utcoffset() != timedelta(0):
                raise ValueError("Proposal timestamps must be UTC")
        if self.expires_at <= self.created_at:
            raise ValueError("Proposal must expire after creation")
        if not self.confidence.is_finite() or not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        if self.invalidation is not None and (
            not self.invalidation.is_finite() or self.invalidation <= 0
        ):
            raise ValueError("invalidation must be a positive Decimal")
        if self.direction is Direction.LONG and self.invalidation is None:
            raise ValueError("A LONG proposal requires an invalidation price")
        if self.direction is Direction.NO_TRADE and not self.reasons:
            raise ValueError("NO_TRADE requires a structured reason")


@dataclass(frozen=True, slots=True)
class StrategyExplanation:
    strategy_id: str
    strategy_version: str
    summary: str
    reasons: tuple[str, ...]
    parameters: dict[str, str]


class Strategy(Protocol):
    @property
    def strategy_id(self) -> str: ...

    @property
    def version(self) -> str: ...

    def warmup_requirements(self) -> WarmupRequirements: ...

    def evaluate(
        self, market_state: MarketState, portfolio_state: PortfolioState
    ) -> SignalProposal: ...

    def explain(self, signal: SignalProposal) -> StrategyExplanation: ...


def is_expired(proposal: SignalProposal, now: datetime) -> bool:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return now >= proposal.expires_at


def evidence_id(candle: Candle) -> str:
    return (
        f"candle:{candle.source}:{candle.market.venue}:{candle.opened_at.isoformat()}"
    )


def _contiguous_closes(state: MarketState) -> list[Decimal] | None:
    """Closes of the latest gap-free run, or None if the series is inconsistent."""
    candles = state.candles
    start = 0
    for index in range(1, len(candles)):
        previous, current = candles[index - 1], candles[index]
        if previous.source != current.source or previous.closed_at > current.opened_at:
            return None
        if previous.closed_at < current.opened_at:
            start = index
    return [candle.close for candle in candles[start:]]


def _no_trade(
    strategy: "_Base", state: MarketState, now: datetime, *reasons: str
) -> SignalProposal:
    return strategy._proposal(state, Direction.NO_TRADE, now, reasons=reasons)


class _Base:
    strategy_id: str
    version: str

    def parameters(self) -> dict[str, str]:
        raise NotImplementedError

    def warmup_requirements(self) -> WarmupRequirements:
        raise NotImplementedError

    def _proposal(
        self,
        state: MarketState,
        direction: Direction,
        now: datetime,
        *,
        reasons: tuple[str, ...] = (),
        conditions: tuple[str, ...] = (),
        invalidation: Decimal | None = None,
        confidence: Decimal = _ZERO,
    ) -> SignalProposal:
        created = state.as_of or now
        return SignalProposal(
            self.strategy_id,
            self.version,
            direction,
            state.market,
            state.interval_minutes,
            conditions,
            invalidation,
            confidence,
            (evidence_id(state.candles[-1]),) if state.candles else (),
            state.quality,
            created,
            created + timedelta(minutes=state.interval_minutes),
            reasons,
        )

    def _gate(self, state: MarketState) -> tuple[list[Decimal] | None, tuple[str, ...]]:
        if state.quality is not DataQuality.FRESH:
            return None, (f"data_quality_{state.quality.value.lower()}",)
        if not state.candles:
            return None, ("no_candles",)
        closes = _contiguous_closes(state)
        if closes is None:
            return None, ("inconsistent_series",)
        if len(closes) < self.warmup_requirements().candles:
            return None, ("warmup_incomplete",)
        return closes, ()

    def explain(self, signal: SignalProposal) -> StrategyExplanation:
        return StrategyExplanation(
            self.strategy_id,
            self.version,
            f"{signal.direction.value} proposal from deterministic rules; "
            "the risk engine decides whether it can proceed.",
            signal.reasons or signal.entry_conditions,
            self.parameters(),
        )


@dataclass(frozen=True, slots=True)
class SmaTrendStrategy(_Base):
    """Educational SMA crossover. Not profitable or production-approved."""

    fast: int = 10
    slow: int = 20
    strategy_id: str = "sma-trend"
    version: str = "2"

    def __post_init__(self) -> None:
        if not 2 <= self.fast < self.slow <= 200:
            raise ValueError("Require 2 <= fast < slow <= 200")

    def parameters(self) -> dict[str, str]:
        return {"fast": str(self.fast), "slow": str(self.slow)}

    def warmup_requirements(self) -> WarmupRequirements:
        return WarmupRequirements(self.slow)

    def evaluate(
        self, market_state: MarketState, portfolio_state: PortfolioState
    ) -> SignalProposal:
        closes, reasons = self._gate(market_state)
        now = market_state.as_of or _EPOCH
        if closes is None:
            return _no_trade(self, market_state, now, *reasons)
        quick, long = sma(closes, self.fast), sma(closes, self.slow)
        if quick is None or long is None:
            return _no_trade(self, market_state, now, "warmup_incomplete")
        holding = portfolio_state.position_quantity > 0
        if quick > long and not holding:
            return self._proposal(
                market_state,
                Direction.LONG,
                now,
                conditions=(f"sma{self.fast} > sma{self.slow}",),
                invalidation=min(c.low for c in market_state.candles[-self.slow :]),
                confidence=min(Decimal(1), (quick - long) / long * 10),
            )
        if quick < long and holding:
            return self._proposal(
                market_state,
                Direction.EXIT,
                now,
                conditions=(f"sma{self.fast} < sma{self.slow}",),
                confidence=Decimal("0.5"),
            )
        return _no_trade(self, market_state, now, "no_state_change")


@dataclass(frozen=True, slots=True)
class RsiRegimeStrategy(_Base):
    """Educational mean-reversion entry inside an uptrend filter."""

    rsi_period: int = 14
    oversold: Decimal = Decimal(30)
    overbought: Decimal = Decimal(70)
    trend_period: int = 50
    strategy_id: str = "rsi-regime"
    version: str = "1"

    def __post_init__(self) -> None:
        if self.rsi_period < 2 or self.trend_period < 2:
            raise ValueError("Periods must be at least 2")
        if not 0 < self.oversold < self.overbought < 100:
            raise ValueError("Require 0 < oversold < overbought < 100")

    def parameters(self) -> dict[str, str]:
        return {
            "rsi_period": str(self.rsi_period),
            "oversold": str(self.oversold),
            "overbought": str(self.overbought),
            "trend_period": str(self.trend_period),
        }

    def warmup_requirements(self) -> WarmupRequirements:
        return WarmupRequirements(max(self.rsi_period + 1, self.trend_period))

    def evaluate(
        self, market_state: MarketState, portfolio_state: PortfolioState
    ) -> SignalProposal:
        closes, reasons = self._gate(market_state)
        now = market_state.as_of or _EPOCH
        if closes is None:
            return _no_trade(self, market_state, now, *reasons)
        value, trend = rsi(closes, self.rsi_period), sma(closes, self.trend_period)
        if value is None or trend is None:
            return _no_trade(self, market_state, now, "warmup_incomplete")
        holding = portfolio_state.position_quantity > 0
        if holding and value >= self.overbought:
            return self._proposal(
                market_state,
                Direction.EXIT,
                now,
                conditions=(f"rsi{self.rsi_period} >= {self.overbought}",),
                confidence=Decimal("0.5"),
            )
        if not holding and value <= self.oversold and closes[-1] > trend:
            return self._proposal(
                market_state,
                Direction.LONG,
                now,
                conditions=(
                    f"rsi{self.rsi_period} <= {self.oversold}",
                    f"close > sma{self.trend_period}",
                ),
                invalidation=min(
                    c.low for c in market_state.candles[-self.rsi_period :]
                ),
                confidence=(self.oversold - value) / self.oversold,
            )
        return _no_trade(self, market_state, now, "no_entry_or_exit_condition")


def replay_signals(
    strategy: Strategy,
    candles: Sequence[Candle],
    portfolio: PortfolioState,
    *,
    start: int | None = None,
) -> Iterator[SignalProposal]:
    """Yield the proposal formed at each candle close using only candles up to it.

    Look-ahead is impossible by construction: ``evaluate`` receives a prefix.
    """
    if not candles:
        return
    first = strategy.warmup_requirements().candles if start is None else start
    market, interval = candles[0].market, candles[0].interval_minutes
    for end in range(max(first, 1), len(candles) + 1):
        state = MarketState(market, interval, tuple(candles[:end]), DataQuality.FRESH)
        yield strategy.evaluate(state, portfolio)
