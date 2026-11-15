"""Durable submission and restart reconciliation for the Kraken spot adapter."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from hcs_trader.data.kraken import BTC_USD, SOURCE
from hcs_trader.domain.execution import SpotOrder, SpotOrderType
from hcs_trader.domain.lifecycle import OrderState
from hcs_trader.domain.market_data import Quote
from hcs_trader.kraken_spot import KrakenSpotAdapter
from hcs_trader.risk import RiskEngine, RiskRejected
from hcs_trader.storage import AuditStore


class KrakenSpotExecution:
    def __init__(
        self, store: AuditStore, adapter: KrakenSpotAdapter, risk: RiskEngine
    ) -> None:
        self._store = store
        self._adapter = adapter
        self._risk = risk

    async def submit(
        self,
        order: SpotOrder,
        quote: Quote,
        *,
        candidate_id: str,
        strategy_version: str,
    ) -> SpotOrder:
        if self._adapter.validate_only or order.state != OrderState.PREVIEWED:
            raise RiskRejected("Submission requires a live adapter and a preview")
        if (
            order.venue != BTC_USD.venue
            or order.market != "BTC/USD"
            or quote.market != BTC_USD
            or quote.source != SOURCE
            or order.order_type != SpotOrderType.LIMIT
            or order.limit_price is None
        ):
            raise RiskRejected(
                "Submission requires a Kraken BTC/USD limit order and quote"
            )
        if (
            order.quantity * order.limit_price > self._risk.policy.max_notional_usd
            or order.quantity != round(order.quantity, 8)
            or order.limit_price != round(order.limit_price, 2)
        ):
            raise RiskRejected(
                "Limit notional or order precision exceeds supported bounds"
            )

        at = datetime.now(UTC)
        quality = quote.quality_at(
            at, max_age=timedelta(seconds=30), max_clock_skew=timedelta(seconds=2)
        )
        verdict = self._risk.evaluate_order(
            order,
            quote,
            quality,
            len(self._store.list_open_spot_orders(order.venue)),
            strategy_version,
            at,
        )
        if not verdict.allowed:
            raise RiskRejected(verdict.reason or "Order rejected by risk policy")
        preview = replace(order, risk_decision_id=verdict.decision_id)
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
            raise RiskRejected("Order state changed during submission; reconcile it")
        return observed

    async def reconcile_startup(self) -> list[SpotOrder]:
        results = []
        for order in self._store.list_open_spot_orders("kraken"):
            observed = await self._adapter.reconcile_order(order, at=datetime.now(UTC))
            if self._store.apply_spot_observation(order, observed):
                results.append(observed)
        return results
