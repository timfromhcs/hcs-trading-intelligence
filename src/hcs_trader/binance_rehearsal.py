"""Binance Spot TESTNET rehearsal flow producing redacted evidence.

Places one resting far-from-market LIMIT BUY on the testnet, proves the
idempotency/reconciliation/cancel paths, and records what happened. Testnet
funds only; never targets mainnet (the adapter profile forbids it).
"""

import secrets
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

from hcs_trader.binance_testnet import (
    VENUE,
    BinanceSpotTestnetAdapter,
    BinanceTestnetError,
    SymbolRules,
)
from hcs_trader.domain.execution import SpotOrder, SpotOrderType, SpotSide
from hcs_trader.domain.lifecycle import OrderState

PRICE_FRACTION = Decimal("0.7")
NOTIONAL_MARGIN = Decimal("1.5")
REDACTED = "[REDACTED]"
_SENSITIVE_KEYS = frozenset(
    {"signature", "secret", "api_key", "apikey", "x-mbx-apikey"}
)


def plan_rehearsal_order(
    last_price: Decimal, rules: SymbolRules
) -> tuple[Decimal, Decimal]:
    """Pick a resting limit price (tick-aligned) and quantity (step-aligned)."""
    price = (last_price * PRICE_FRACTION / rules.tick_size).to_integral_value(
        ROUND_FLOOR
    ) * rules.tick_size
    if price <= 0:
        raise ValueError("Computed rehearsal price is not positive")
    quantity = (
        max(rules.min_notional * NOTIONAL_MARGIN / price, rules.min_qty)
        / rules.step_size
    ).to_integral_value(ROUND_CEILING) * rules.step_size
    return price, quantity


def redact_evidence(value: Any, secrets_to_hide: Iterable[str]) -> Any:
    """Recursively drop sensitive keys and mask any string containing a secret."""
    hidden = [s for s in secrets_to_hide if s]
    if isinstance(value, dict):
        return {
            k: REDACTED
            if str(k).lower() in _SENSITIVE_KEYS
            else redact_evidence(v, hidden)
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact_evidence(v, hidden) for v in value]
    if isinstance(value, str) and any(s in value for s in hidden):
        return REDACTED
    return value


def _order_summary(order: SpotOrder) -> dict[str, Any]:
    return {
        "state": order.state.value,
        "client_order_id": order.client_order_id,
        "venue_order_id": order.venue_order_id,
        "quantity": str(order.quantity),
        "limit_price": str(order.limit_price),
        "filled_quantity": str(order.filled_quantity),
    }


async def _cleanup(
    adapter: BinanceSpotTestnetAdapter,
    order: SpotOrder,
    placed: SpotOrder | None,
    now: Callable[[], datetime],
) -> bool:
    """Best-effort: make sure no rehearsal order is left resting on the testnet."""
    try:
        current = placed
        if current is None:
            ambiguous = order.transition_to(OrderState.SUBMITTED, now()).transition_to(
                OrderState.RECONCILIATION_REQUIRED, now()
            )
            current = await adapter.reconcile_order(
                ambiguous, now(), never_acknowledged=True
            )
        if current.state in (OrderState.ACKNOWLEDGED, OrderState.PARTIALLY_FILLED):
            cancelled = await adapter.cancel_spot_order(current, now())
            return cancelled.state is OrderState.CANCELLED
        return current.state in (
            OrderState.CANCELLED,
            OrderState.FILLED,
            OrderState.REJECTED,
        )
    except (BinanceTestnetError, ValueError, KeyError, ArithmeticError):
        return False


async def run_rehearsal(
    adapter: BinanceSpotTestnetAdapter,
    *,
    market: str = "BTC/USDT",
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    id_factory: Callable[[], str] = lambda: secrets.token_hex(6),
    secrets_to_hide: Iterable[str] = (),
) -> dict[str, Any]:
    """Run the rehearsal and return a redacted evidence document."""
    started = now()
    steps: list[dict[str, Any]] = []
    order: SpotOrder | None = None
    placed: SpotOrder | None = None

    def record(name: str, ok: bool, **detail: Any) -> None:
        steps.append({"name": name, "ok": ok, "detail": detail})

    try:
        server_ms = await adapter.server_time()
        record("server_time", True, server_time_ms=server_ms)
        account = await adapter.get_account()
        record(
            "account",
            True,
            can_trade=account.can_trade,
            can_withdraw=account.can_withdraw,
            permissions=list(account.permissions),
            nonzero_balances={
                b.asset: str(b.free) for b in account.balances if b.free > 0
            },
        )
        last = await adapter.get_price(market)
        rules = await adapter.symbol_rules(market)
        price, quantity = plan_rehearsal_order(last, rules)
        record(
            "plan",
            True,
            last_price=str(last),
            limit_price=str(price),
            quantity=str(quantity),
        )

        stamp = now()
        order = SpotOrder(
            client_order_id=f"hcs-rh-{id_factory()}",
            order_id=f"rehearsal-{id_factory()}",
            venue=VENUE,
            market=market,
            side=SpotSide.BUY,
            order_type=SpotOrderType.LIMIT,
            quantity=quantity,
            limit_price=price,
            state=OrderState.PREVIEWED,
            risk_decision_id="rehearsal",
            created_at=stamp,
            updated_at=stamp,
        )
        placed = await adapter.submit_and_reconcile(order, now())
        record(
            "submit", placed.state is OrderState.ACKNOWLEDGED, **_order_summary(placed)
        )
        if placed.state is not OrderState.ACKNOWLEDGED:
            raise BinanceTestnetError(f"Order not acknowledged: {placed.state.value}")

        open_orders = await adapter.open_orders(market)
        listed = any(o.client_order_id == order.client_order_id for o in open_orders)
        record("open_orders", listed, count=len(open_orders))

        queried = await adapter.query_order_by_client_id(market, order.client_order_id)
        record(
            "query_by_client_id",
            queried is not None and queried.status == "NEW",
            status=queried.status if queried else None,
        )

        # Replaying the same client id must never create a second order.
        replay = await adapter.submit_spot_order(order, now())
        record(
            "duplicate_client_id_replay",
            replay.state in (OrderState.RECONCILIATION_REQUIRED, OrderState.REJECTED),
            state=replay.state.value,
        )
        after_replay = [
            o
            for o in await adapter.open_orders(market)
            if o.client_order_id == order.client_order_id
        ]
        record("single_open_order_after_replay", len(after_replay) == 1)

        cancelled = await adapter.cancel_spot_order(placed, now())
        record(
            "cancel",
            cancelled.state is OrderState.CANCELLED,
            **_order_summary(cancelled),
        )
        order = None
        trades = await adapter.fills(market)
        record("my_trades", True, count=len(trades))
        balances = await adapter.balances()
        record(
            "balances_after",
            True,
            nonzero={b.asset: str(b.free) for b in balances if b.free > 0},
        )
    except (BinanceTestnetError, ValueError) as error:
        record("failure", False, error=type(error).__name__, message=str(error))
        if order is not None:
            ok = await _cleanup(adapter, order, placed, now)
            record("cleanup_cancel", ok)

    evidence: dict[str, Any] = {
        "schema": 1,
        "venue": VENUE,
        "base_url": adapter.profile.base_url,
        "market": market,
        "started_at": started.isoformat(),
        "finished_at": now().isoformat(),
        "passed": all(step["ok"] for step in steps) and bool(steps),
        "steps": steps,
        "notes": "Testnet only. No real funds. Secrets and signatures are redacted.",
    }
    return redact_evidence(evidence, secrets_to_hide)  # type: ignore[no-any-return]
