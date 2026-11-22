"""Deterministic pre-trade risk engine and immutable policy enforcement."""

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from uuid import uuid4

from hcs_trader.domain.execution import SpotOrder, SpotSide, spot_order_plan_hash
from hcs_trader.domain.market_data import DataQuality, Quote
from hcs_trader.domain.markets import MARKET_REGISTRY
from hcs_trader.portfolio_state import PortfolioState

_BPS = Decimal("10000")


class RiskRejected(Exception):
    """The proposed order violates deterministic pre-trade risk policy."""


@dataclass(frozen=True, slots=True)
class RiskEvaluation:
    decision_id: str
    allowed: bool
    reason: str | None
    evaluated_at: datetime
    plan_hash: str | None = None


@dataclass(frozen=True, slots=True)
class RiskPolicy:
    """Pre-trade limits evaluated independently before any order submission."""

    live_mode_enabled: bool = False
    kill_switch_active: bool = False
    allowed_venues: frozenset[str] = frozenset({"kraken"})
    # Must stay equal to the executable scope (spot_execution.SUPPORTED_MARKETS).
    allowed_markets: frozenset[str] = MARKET_REGISTRY.live_allowed_symbols()
    min_notional_usd: Decimal = Decimal("10.00")
    max_notional_usd: Decimal = Decimal("1000.00")
    max_spread_bps: Decimal = Decimal("100.00")  # 1.00%
    max_slippage_bps: Decimal = Decimal("100.00")  # 1.00%
    max_open_orders: int = 2
    approved_strategy_versions: frozenset[str] = frozenset({"v1.0.0"})
    # Portfolio-driven limits (AGENTS.md 6.4 starter defaults, configurable).
    require_portfolio_state: bool = False
    risk_fraction: Decimal = Decimal("0.0025")
    max_daily_loss_fraction: Decimal = Decimal("0.01")
    max_drawdown_fraction: Decimal = Decimal("0.05")
    max_positions: int = 2
    max_asset_exposure_fraction: Decimal = Decimal("0.5")
    max_total_exposure_fraction: Decimal = Decimal("1.0")
    max_consecutive_losses: int = 3
    estimated_fee_bps: Decimal = Decimal("40")
    max_fee_bps: Decimal = Decimal("100")
    min_edge_to_fee_multiple: Decimal = Decimal("2")
    max_limit_deviation_bps: Decimal = Decimal("100")
    max_reconcile_age_seconds: int = 300
    # Consecutive rejected verdicts/venue rejects that trip the kill switch (7.2).
    max_consecutive_rejects: int = 5
    max_hot_wallet_usd: Decimal | None = None
    hot_fraction_of_portfolio: Decimal | None = None
    require_venue_reconciliation: bool = False


def _canonical(value: object) -> object:
    if isinstance(value, frozenset):
        return sorted(str(item) for item in value)
    if isinstance(value, Decimal):
        return str(value)
    return value


def policy_version(policy: RiskPolicy) -> str:
    """Stable content hash identifying the exact policy a decision was made under."""
    fields = {
        key: _canonical(val) for key, val in asdict(policy).items() if val is not None
    }
    if not policy.require_venue_reconciliation:
        fields.pop("require_venue_reconciliation")
    blob = json.dumps(fields, sort_keys=True, separators=(",", ":"))
    return "policy-" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


class RiskEngine:
    """Deterministic pre-trade risk evaluation engine."""

    def __init__(
        self, policy: RiskPolicy, pause_source: Callable[[], bool] | None = None
    ) -> None:
        self.policy = policy
        # Persisted pause/kill-switch reader (e.g. AuditStore.fail_safe_paused).
        self._pause_source = pause_source

    def _persisted_pause(self) -> bool:
        if self._pause_source is None:
            return False
        try:
            return bool(self._pause_source())
        except Exception:  # noqa: BLE001 - an unreadable pause state fails closed
            return True

    def evaluate_order(
        self,
        order: SpotOrder,
        quote: Quote | None,
        quote_quality: DataQuality,
        current_open_orders_count: int,
        strategy_version: str,
        at: datetime,
        *,
        portfolio: PortfolioState | None = None,
        invalidation_price: Decimal | None = None,
        expected_edge_bps: Decimal | None = None,
        venue_healthy: bool | None = None,
        last_reconciled_at: datetime | None = None,
        unresolved_market_orders: int = 0,
        paused: bool = False,
    ) -> RiskEvaluation:
        """Evaluate pre-trade rules; the verdict carries the order plan hash."""
        decision_id = f"risk-{uuid4().hex[:12]}"
        reason = self._reason(
            order,
            quote,
            quote_quality,
            current_open_orders_count,
            strategy_version,
            at,
            portfolio,
            invalidation_price,
            expected_edge_bps,
            venue_healthy,
            last_reconciled_at,
            unresolved_market_orders,
            paused or self._persisted_pause(),
        )
        return RiskEvaluation(
            decision_id=decision_id,
            allowed=reason is None,
            reason=reason,
            evaluated_at=at,
            plan_hash=spot_order_plan_hash(order),
        )

    def _reason(  # noqa: PLR0911, PLR0912, PLR0913
        self,
        order: SpotOrder,
        quote: Quote | None,
        quote_quality: DataQuality,
        open_orders: int,
        strategy_version: str,
        at: datetime,
        portfolio: PortfolioState | None,
        invalidation_price: Decimal | None,
        expected_edge_bps: Decimal | None,
        venue_healthy: bool | None,
        last_reconciled_at: datetime | None,
        unresolved_market_orders: int,
        paused: bool,
    ) -> str | None:
        """Return the first violated rule, or None when the order is allowed."""
        pol = self.policy
        if pol.kill_switch_active:
            return "Signing rejected: kill switch is active"
        if paused:
            return "Execution paused: persisted kill switch is active"
        if not pol.live_mode_enabled:
            return "Live execution rejected: live mode is not enabled (PAPER default)"
        if order.venue not in pol.allowed_venues:
            return f"Venue '{order.venue}' is not allowlisted for live execution"
        if order.market not in pol.allowed_markets:
            return f"Market '{order.market}' is not allowlisted for live execution"
        if not MARKET_REGISTRY.is_live_allowed(order.market):
            return (
                f"Market '{order.market}' is not live_allowed in the market "
                "registry (read-only or paper)"
            )
        if strategy_version not in pol.approved_strategy_versions:
            return (
                f"Strategy version '{strategy_version}' is not approved "
                "for live trading"
            )
        if quote_quality is not DataQuality.FRESH or quote is None:
            return "Pre-trade rejected: live execution requires a fresh market quote"

        spread = (quote.ask - quote.bid) / quote.bid
        if spread > pol.max_spread_bps / _BPS:
            return (
                f"Market spread {spread * 100:.2f}% exceeds policy limit "
                f"{pol.max_spread_bps / 100:.2f}%"
            )

        reference_price = quote.ask if order.side is SpotSide.BUY else quote.bid
        notional = order.quantity * reference_price
        if notional < pol.min_notional_usd:
            return (
                f"Order notional ${notional:.2f} is below minimum allowed "
                f"${pol.min_notional_usd:.2f}"
            )
        if notional > pol.max_notional_usd:
            return (
                f"Order notional ${notional:.2f} exceeds maximum allowed "
                f"${pol.max_notional_usd:.2f}"
            )

        reason = self._limit_price_reason(order, quote)
        if reason is not None:
            return reason

        if venue_healthy is False:
            return "Pre-trade rejected: venue is unhealthy"
        if pol.require_venue_reconciliation:
            if venue_healthy is not True:
                return "Pre-trade rejected: venue health is required"
            if last_reconciled_at is None:
                return "Pre-trade rejected: venue reconciliation is required"
        if last_reconciled_at is not None and (
            last_reconciled_at.utcoffset() is None
            or not 0
            <= (at - last_reconciled_at).total_seconds()
            <= pol.max_reconcile_age_seconds
        ):
            return "Pre-trade rejected: reconciliation is stale"
        if unresolved_market_orders > 0:
            return f"Pre-trade rejected: unresolved order exists for {order.market}"

        if open_orders >= pol.max_open_orders:
            return (
                f"Venue '{order.venue}' already has {open_orders} "
                f"active orders (cap: {pol.max_open_orders})"
            )

        if portfolio is None:
            if (
                pol.require_portfolio_state
                or pol.max_hot_wallet_usd is not None
                or pol.hot_fraction_of_portfolio is not None
            ):
                return "Pre-trade rejected: portfolio state is required"
            return None
        return self._portfolio_reason(
            order,
            notional,
            reference_price,
            portfolio,
            invalidation_price,
            expected_edge_bps,
        )

    def _limit_price_reason(self, order: SpotOrder, quote: Quote) -> str | None:
        if order.limit_price is None:
            return None
        limit = order.limit_price
        dev_cap = self.policy.max_limit_deviation_bps / _BPS
        slip_cap = self.policy.max_slippage_bps / _BPS
        if order.side is SpotSide.BUY:
            if limit > quote.ask:
                if (limit - quote.ask) / quote.ask > slip_cap:
                    return "Buy limit slippage exceeds policy cap"
            elif (quote.bid - limit) / quote.bid > dev_cap:
                return "Buy limit price deviation below bid exceeds policy cap"
        elif limit < quote.bid:
            if (quote.bid - limit) / quote.bid > slip_cap:
                return "Sell limit slippage exceeds policy cap"
        elif (limit - quote.ask) / quote.ask > dev_cap:
            return "Sell limit price deviation above ask exceeds policy cap"
        return None

    def _portfolio_reason(  # noqa: PLR0911, PLR0912
        self,
        order: SpotOrder,
        notional: Decimal,
        price: Decimal,
        pf: PortfolioState,
        invalidation: Decimal | None,
        edge_bps: Decimal | None,
    ) -> str | None:
        pol = self.policy
        if (
            pol.max_hot_wallet_usd is not None
            and pf.hot_equity_usd > pol.max_hot_wallet_usd
        ):
            return "Hot-wallet equity exceeds the configured USD cap"
        if pol.hot_fraction_of_portfolio is not None:
            if pf.total_equity_usd is None:
                return (
                    "Total portfolio equity is required for the hot-wallet share limit"
                )
            if pf.hot_equity_usd > pf.total_equity_usd * pol.hot_fraction_of_portfolio:
                return "Hot-wallet share exceeds the configured portfolio limit"
        base = order.market.split("/")[0]
        fee_bps = pol.estimated_fee_bps
        if fee_bps > pol.max_fee_bps:
            return f"Estimated fee {fee_bps} bps exceeds cap {pol.max_fee_bps} bps"
        if edge_bps is not None and edge_bps < fee_bps * pol.min_edge_to_fee_multiple:
            return "Expected edge does not sufficiently exceed estimated fees"
        if pf.drawdown_fraction >= pol.max_drawdown_fraction:
            return "Peak drawdown limit reached: new orders paused"
        daily_limit = pf.hot_equity_usd * pol.max_daily_loss_fraction
        if -pf.realized_pnl_today_usd >= daily_limit:
            return "Daily loss limit reached: new orders paused"
        if pf.consecutive_losses >= pol.max_consecutive_losses:
            return "Consecutive-loss protection active: new orders paused"
        if order.side is SpotSide.SELL:
            if order.quantity > pf.base_balances.get(base, Decimal(0)):
                return f"Sell quantity exceeds owned {base} inventory (spot only)"
            return None
        fee = notional * fee_bps / _BPS
        if notional + fee > pf.available_quote_usd:
            return "Insufficient available quote balance including fee reserve"
        held = pf.exposure_usd.get(base, Decimal(0))
        if held == 0 and pf.open_positions >= pol.max_positions:
            return f"Maximum simultaneous positions reached ({pol.max_positions})"
        if held + notional > pf.hot_equity_usd * pol.max_asset_exposure_fraction:
            return f"Projected {base} exposure exceeds per-asset cap"
        total_cap = pf.hot_equity_usd * pol.max_total_exposure_fraction
        if pf.total_exposure_usd + notional > total_cap:
            return "Projected total exposure exceeds cap"
        if invalidation is None or invalidation <= 0:
            return "Buy requires a valid invalidation price for risk sizing"
        risk_per_unit = price - invalidation + price * fee_bps / _BPS
        if risk_per_unit <= 0:
            return "Risk per unit is not positive: invalid invalidation price"
        if order.quantity > pf.hot_equity_usd * pol.risk_fraction / risk_per_unit:
            return "Order size exceeds per-trade risk budget"
        projected_loss = -pf.realized_pnl_today_usd + order.quantity * risk_per_unit
        if projected_loss > daily_limit:
            return "Projected daily loss would exceed limit"
        return None
