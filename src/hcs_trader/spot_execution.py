"""Durable submission and restart reconciliation for the Kraken spot adapter."""

import sqlite3
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from hcs_trader.data.kraken import BTC_USD, SOURCE
from hcs_trader.domain.execution import SpotOrder, SpotOrderType, spot_order_plan_hash
from hcs_trader.domain.lifecycle import OrderState
from hcs_trader.domain.market_data import Quote
from hcs_trader.kraken_spot import (
    KrakenExecutionError,
    KrakenOrderValidation,
    KrakenSpotAdapter,
)
from hcs_trader.portfolio_state import PortfolioState
from hcs_trader.risk import (
    RiskEngine,
    RiskEvaluation,
    RiskPolicy,
    RiskRejected,
    policy_version,
)
from hcs_trader.storage import AuditStore, KillSwitchCause, RiskOutcome

# Markets the execution path can actually trade; RiskPolicy defaults must not exceed it.
SUPPORTED_MARKETS = frozenset({"BTC/USD"})
_CANCELLABLE = frozenset({OrderState.ACKNOWLEDGED, OrderState.PARTIALLY_FILLED})


class KrakenSpotExecution:
    def __init__(
        self,
        store: AuditStore,
        adapter: KrakenSpotAdapter,
        risk: RiskEngine,
        *,
        live_policy: Callable[[], RiskPolicy] | None = None,
    ) -> None:
        self._store = store
        self._adapter = adapter
        self._risk = risk
        self._live_policy = live_policy
        self._venue_rejects = 0

    @staticmethod
    def _check_shape(order: SpotOrder, quote: Quote) -> None:
        if (
            order.venue != BTC_USD.venue
            or order.market not in SUPPORTED_MARKETS
            or quote.market != BTC_USD
            or quote.source != SOURCE
            or order.order_type != SpotOrderType.LIMIT
            or order.limit_price is None
        ):
            raise RiskRejected(
                "Submission requires a Kraken BTC/USD limit order and quote"
            )

    def _check_bounds(self, order: SpotOrder) -> None:
        if order.limit_price is None or (
            order.quantity * order.limit_price > self._risk.policy.max_notional_usd
            or order.quantity != round(order.quantity, 8)
            or order.limit_price != round(order.limit_price, 2)
        ):
            raise RiskRejected(
                "Limit notional or order precision exceeds supported bounds"
            )

    def _persist_verdict(
        self,
        verdict: RiskEvaluation,
        strategy_version: str,
        order: SpotOrder,
        quote: Quote,
    ) -> None:
        """Persist every verdict (allow and reject); an unwritable store is NO_TRADE."""
        try:
            self._store.record_risk_decision(
                verdict,
                policy_version=policy_version(self._risk.policy),
                inputs={
                    "client_order_id": order.client_order_id,
                    "market": order.market,
                    "side": order.side.value,
                    "quantity": str(order.quantity),
                    "limit_price": str(order.limit_price),
                    "bid": str(quote.bid),
                    "ask": str(quote.ask),
                },
                strategy_version=strategy_version,
            )
        except (sqlite3.Error, ValueError) as error:
            raise RiskRejected(
                "Risk decision could not be persisted: execution blocked (NO_TRADE)"
            ) from error

    async def submit(
        self,
        order: SpotOrder,
        quote: Quote,
        *,
        candidate_id: str,
        strategy_version: str,
        portfolio: PortfolioState | None = None,
        invalidation_price: Decimal | None = None,
        expected_edge_bps: Decimal | None = None,
        venue_healthy: bool | None = None,
        last_reconciled_at: datetime | None = None,
    ) -> SpotOrder:
        if self._adapter.validate_only:
            raise RiskRejected("Submission is blocked on a validation-only adapter")
        if order.state != OrderState.PREVIEWED:
            raise RiskRejected("Submission requires a preview")
        if self._live_policy is not None and self._live_policy() != self._risk.policy:
            raise RiskRejected("LIVE policy changed; restart the execution session")
        self._check_shape(order, quote)
        self._check_bounds(order)

        at = datetime.now(UTC)
        quality = quote.quality_at(
            at, max_age=timedelta(seconds=30), max_clock_skew=timedelta(seconds=2)
        )
        plan_hash = spot_order_plan_hash(order)
        open_orders = self._store.list_open_spot_orders(order.venue)
        verdict = self._risk.evaluate_order(
            order,
            quote,
            quality,
            len(open_orders),
            strategy_version,
            at,
            portfolio=portfolio,
            invalidation_price=invalidation_price,
            expected_edge_bps=expected_edge_bps,
            venue_healthy=venue_healthy,
            last_reconciled_at=last_reconciled_at,
            unresolved_market_orders=sum(
                1
                for o in open_orders
                if o.market == order.market
                and o.client_order_id != order.client_order_id  # dup: store rejects
            ),
            paused=self._store.fail_safe_paused(),
        )
        self._persist_verdict(verdict, strategy_version, order, quote)
        if not verdict.allowed:
            await self._after_rejection()
            raise RiskRejected(verdict.reason or "Order rejected by risk policy")
        if verdict.plan_hash != plan_hash:
            raise RiskRejected("Order plan hash does not match the risk verdict")
        preview = replace(order, risk_decision_id=verdict.decision_id)
        if spot_order_plan_hash(preview) != verdict.plan_hash:
            raise RiskRejected(
                "Order changed after risk evaluation; plan hash mismatch"
            )
        reserved = preview.transition_to(OrderState.SUBMITTED, at=at)
        self._store.reserve_spot_submission(
            reserved,
            self._risk.policy.max_open_orders,
            candidate_id=candidate_id,
            strategy_version=strategy_version,
            key_fingerprint=self._adapter.key_fingerprint,
        )
        observed = await self._adapter.submit_spot_order(preview, at=at)
        if not self._store.apply_spot_observation(reserved, observed):
            await self.trip(
                KillSwitchCause.RECONCILIATION_MISMATCH,
                f"order {order.client_order_id} changed during submission",
            )
            raise RiskRejected("Order state changed during submission; reconcile it")
        await self._after_observation(observed)
        return observed

    async def dry_run(
        self,
        order: SpotOrder,
        quote: Quote,
        *,
        strategy_version: str,
        portfolio: PortfolioState | None = None,
        invalidation_price: Decimal | None = None,
        expected_edge_bps: Decimal | None = None,
    ) -> KrakenOrderValidation:
        """Risk-check an order, then ask Kraken to validate it (validate=true).

        Needs a validation-only adapter. No order row is reserved or stored and no
        funds can move; the verdict is audited but never persisted as an approval.
        The same deterministic rules apply, with live mode treated as enabled only
        for this evaluation. A persisted pause or kill switch still blocks it.
        """
        if not self._adapter.validate_only or order.state != OrderState.PREVIEWED:
            raise RiskRejected("Dry run requires a validation-only adapter and preview")
        self._check_shape(order, quote)
        self._check_bounds(order)
        at = datetime.now(UTC)
        quality = quote.quality_at(
            at, max_age=timedelta(seconds=30), max_clock_skew=timedelta(seconds=2)
        )
        engine = RiskEngine(replace(self._risk.policy, live_mode_enabled=True))
        verdict = engine.evaluate_order(
            order,
            quote,
            quality,
            len(self._store.list_open_spot_orders(order.venue)),
            strategy_version,
            at,
            portfolio=portfolio,
            invalidation_price=invalidation_price,
            expected_edge_bps=expected_edge_bps,
            paused=self._store.fail_safe_paused(),
        )
        self._store.record_structured_audit(
            "spot.dry_run.evaluated",
            arguments={"market": order.market, "reason": verdict.reason},
            strategy_version=strategy_version,
            order_or_tx_id=order.client_order_id,
            outcome="ALLOWED" if verdict.allowed else RiskOutcome.REJECTED.value,
        )
        if not verdict.allowed:
            raise RiskRejected(verdict.reason or "Order rejected by risk policy")
        return await self._adapter.validate_spot_order(order, at)

    async def reconcile_startup(self) -> list[SpotOrder]:
        results = []
        for order in self._store.list_open_spot_orders("kraken"):
            observed = await self._adapter.reconcile_order(order, at=datetime.now(UTC))
            if self._store.apply_spot_observation(order, observed):
                results.append(observed)
            else:
                await self.trip(
                    KillSwitchCause.RECONCILIATION_MISMATCH,
                    f"order {order.client_order_id} diverged during reconcile",
                )
            await self._after_observation(observed)
        return results

    async def check_balance(
        self,
        asset: str,
        expected: Decimal,
        observed: Decimal,
        tolerance: Decimal = Decimal(0),
    ) -> bool:
        """Compare ledger vs venue balance; an unexplained delta trips the switch."""
        if (
            not expected.is_finite()
            or not observed.is_finite()
            or abs(expected - observed) > tolerance
        ):
            await self.trip(
                KillSwitchCause.UNEXPLAINED_BALANCE_DELTA,
                f"{asset} balance expected {expected} but venue reports {observed}",
            )
            return False
        return True

    async def trip(self, cause: KillSwitchCause, detail: str) -> None:
        """Persist a kill-switch trip, then best-effort cancel resting orders.

        Never sells or opens anything; an order that cannot be cancelled stays as
        it is for operator reconciliation.
        """
        try:
            if not self._store.kill_switch_state().active:
                self._store.trip_kill_switch(cause, detail)
        except sqlite3.Error:
            pass  # fail_safe_paused() already reports paused on storage faults
        await self.cancel_open_orders()

    async def cancel_open_orders(self) -> list[SpotOrder]:
        """Best-effort cancel of acknowledged/partially filled Kraken orders."""
        cancelled: list[SpotOrder] = []
        if self._adapter.validate_only:
            return cancelled
        try:
            open_orders = self._store.list_open_spot_orders("kraken")
        except sqlite3.Error:
            return cancelled
        for order in open_orders:
            if order.state not in _CANCELLABLE or not order.venue_order_id:
                continue
            try:
                after = await self._adapter.cancel_spot_order(
                    order, at=datetime.now(UTC)
                )
                if self._store.apply_spot_observation(order, after):
                    cancelled.append(after)
            except (KrakenExecutionError, ValueError, sqlite3.Error):
                continue
        return cancelled

    async def _after_observation(self, observed: SpotOrder) -> None:
        if observed.state is OrderState.RECONCILIATION_REQUIRED:
            await self.trip(
                KillSwitchCause.RECONCILIATION_REQUIRED,
                f"order {observed.client_order_id} needs reconciliation",
            )
        elif observed.state is OrderState.REJECTED:
            self._venue_rejects += 1
            await self._after_rejection()
        else:
            self._venue_rejects = 0

    async def _after_rejection(self) -> None:
        """Trip on a streak of rejected verdicts or venue rejects (AGENTS.md 7.2)."""
        limit = self._risk.policy.max_consecutive_rejects
        try:
            floor = self._store.execution_pause().changed_at
            streak = 0
            for decision in self._store.list_risk_decisions(limit):
                if floor is not None and decision.evaluated_at <= floor:
                    break
                if decision.outcome is not RiskOutcome.REJECTED:
                    break
                streak += 1
        except sqlite3.Error:
            streak = limit  # unreadable history fails closed
        if streak >= limit or self._venue_rejects >= limit:
            await self.trip(
                KillSwitchCause.REPEATED_REJECTS,
                f"{max(streak, self._venue_rejects)} consecutive rejects",
            )
