"""Deterministic pre-trade risk engine and immutable policy enforcement."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import uuid4

from hcs_trader.domain.execution import SpotOrder, SpotSide
from hcs_trader.domain.market_data import DataQuality, Quote


class RiskRejected(Exception):
    """The proposed order violates deterministic pre-trade risk policy."""


@dataclass(frozen=True, slots=True)
class RiskEvaluation:
    decision_id: str
    allowed: bool
    reason: str | None
    evaluated_at: datetime


@dataclass(frozen=True, slots=True)
class RiskPolicy:
    """Pre-trade limits evaluated independently before any order submission."""

    live_mode_enabled: bool = False
    kill_switch_active: bool = False
    allowed_venues: frozenset[str] = frozenset({"kraken"})
    allowed_markets: frozenset[str] = frozenset({"BTC/USD"})
    min_notional_usd: Decimal = Decimal("10.00")
    max_notional_usd: Decimal = Decimal("1000.00")
    max_spread_bps: Decimal = Decimal("100.00")  # 1.00%
    max_slippage_bps: Decimal = Decimal("100.00")  # 1.00%
    max_open_orders: int = 2
    approved_strategy_versions: frozenset[str] = frozenset({"v1.0.0"})


class RiskEngine:
    """Deterministic pre-trade risk evaluation engine."""

    def __init__(self, policy: RiskPolicy) -> None:
        self.policy = policy

    def evaluate_order(
        self,
        order: SpotOrder,
        quote: Quote | None,
        quote_quality: DataQuality,
        current_open_orders_count: int,
        strategy_version: str,
        at: datetime,
    ) -> RiskEvaluation:
        """Evaluate pre-trade rules and return a typed decision."""
        decision_id = f"risk-{uuid4().hex[:12]}"

        # 1. Kill switch check
        if self.policy.kill_switch_active:
            return RiskEvaluation(
                decision_id=decision_id,
                allowed=False,
                reason="Signing rejected: kill switch is active",
                evaluated_at=at,
            )

        # 2. Live mode check
        if not self.policy.live_mode_enabled:
            return RiskEvaluation(
                decision_id=decision_id,
                allowed=False,
                reason=(
                    "Live execution rejected: live mode is not enabled (PAPER default)"
                ),
                evaluated_at=at,
            )

        # 3. Venue and Market allowlists
        if order.venue not in self.policy.allowed_venues:
            return RiskEvaluation(
                decision_id=decision_id,
                allowed=False,
                reason=(f"Venue '{order.venue}' is not allowlisted for live execution"),
                evaluated_at=at,
            )

        if order.market not in self.policy.allowed_markets:
            return RiskEvaluation(
                decision_id=decision_id,
                allowed=False,
                reason=(
                    f"Market '{order.market}' is not allowlisted for live execution"
                ),
                evaluated_at=at,
            )

        # 4. Strategy version check
        if strategy_version not in self.policy.approved_strategy_versions:
            return RiskEvaluation(
                decision_id=decision_id,
                allowed=False,
                reason=(
                    f"Strategy version '{strategy_version}' is not approved "
                    "for live trading"
                ),
                evaluated_at=at,
            )

        # 5. Quote freshness check
        if quote_quality is not DataQuality.FRESH or quote is None:
            return RiskEvaluation(
                decision_id=decision_id,
                allowed=False,
                reason=(
                    "Pre-trade rejected: live execution requires a fresh market quote"
                ),
                evaluated_at=at,
            )

        # 6. Spread check
        spread = (quote.ask - quote.bid) / quote.bid
        max_spread = self.policy.max_spread_bps / Decimal("10000")
        if spread > max_spread:
            return RiskEvaluation(
                decision_id=decision_id,
                allowed=False,
                reason=(
                    f"Market spread {spread * 100:.2f}% exceeds policy limit "
                    f"{self.policy.max_spread_bps / 100:.2f}%"
                ),
                evaluated_at=at,
            )

        # 7. Notional bounds check
        reference_price = quote.ask if order.side is SpotSide.BUY else quote.bid
        notional = order.quantity * reference_price
        if notional < self.policy.min_notional_usd:
            return RiskEvaluation(
                decision_id=decision_id,
                allowed=False,
                reason=(
                    f"Order notional ${notional:.2f} is below minimum allowed "
                    f"${self.policy.min_notional_usd:.2f}"
                ),
                evaluated_at=at,
            )

        if notional > self.policy.max_notional_usd:
            return RiskEvaluation(
                decision_id=decision_id,
                allowed=False,
                reason=(
                    f"Order notional ${notional:.2f} exceeds maximum allowed "
                    f"${self.policy.max_notional_usd:.2f}"
                ),
                evaluated_at=at,
            )

        # 8. Limit price slippage check
        if order.limit_price is not None:
            if order.side is SpotSide.BUY and order.limit_price < quote.ask:
                # Buy limit below ask may not fill immediately
                pass
            if order.side is SpotSide.BUY and order.limit_price > quote.ask:
                slippage = (order.limit_price - quote.ask) / quote.ask
                if slippage > self.policy.max_slippage_bps / Decimal("10000"):
                    return RiskEvaluation(
                        decision_id=decision_id,
                        allowed=False,
                        reason="Buy limit slippage exceeds policy cap",
                        evaluated_at=at,
                    )
            if order.side is SpotSide.SELL and order.limit_price < quote.bid:
                slippage = (quote.bid - order.limit_price) / quote.bid
                if slippage > self.policy.max_slippage_bps / Decimal("10000"):
                    return RiskEvaluation(
                        decision_id=decision_id,
                        allowed=False,
                        reason="Sell limit slippage exceeds policy cap",
                        evaluated_at=at,
                    )

        # 9. Maximum open orders check
        if current_open_orders_count >= self.policy.max_open_orders:
            return RiskEvaluation(
                decision_id=decision_id,
                allowed=False,
                reason=(
                    f"Venue '{order.venue}' already has {current_open_orders_count} "
                    f"active orders (cap: {self.policy.max_open_orders})"
                ),
                evaluated_at=at,
            )

        return RiskEvaluation(
            decision_id=decision_id,
            allowed=True,
            reason=None,
            evaluated_at=at,
        )
