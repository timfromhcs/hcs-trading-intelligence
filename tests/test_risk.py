"""Tests for deterministic pre-trade risk engine and policy bounds."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from hcs_trader.data.kraken import BTC_USD, SOURCE
from hcs_trader.domain.execution import (
    SpotOrder,
    SpotOrderType,
    SpotSide,
    spot_order_plan_hash,
)
from hcs_trader.domain.lifecycle import OrderState
from hcs_trader.domain.market_data import DataQuality, Quote
from hcs_trader.portfolio_state import PortfolioState
from hcs_trader.risk import RiskEngine, RiskEvaluation, RiskPolicy
from hcs_trader.spot_execution import SUPPORTED_MARKETS


def _sample_quote(
    bid: str = "50000.00",
    ask: str = "50050.00",
    age_seconds: int = 5,
) -> Quote:
    now = datetime.now(UTC)
    t = now - timedelta(seconds=age_seconds)
    return Quote(
        market=BTC_USD,
        source=SOURCE,
        bid=Decimal(bid),
        ask=Decimal(ask),
        bid_updated_at=t,
        ask_updated_at=t,
        received_at=t,
    )


def _sample_order(
    quantity: str = "0.01",
    side: SpotSide = SpotSide.BUY,
    limit_price: str | None = None,
    venue: str = "kraken",
    market: str = "BTC/USD",
) -> SpotOrder:
    now = datetime.now(UTC)
    return SpotOrder(
        client_order_id="cl-ord-001",
        order_id="ord-001",
        venue=venue,
        market=market,
        side=side,
        order_type=SpotOrderType.LIMIT if limit_price else SpotOrderType.MARKET,
        quantity=Decimal(quantity),
        limit_price=Decimal(limit_price) if limit_price else None,
        state=OrderState.PREVIEWED,
        risk_decision_id="risk-init",
        created_at=now,
        updated_at=now,
    )


def _active_policy() -> RiskPolicy:
    return RiskPolicy(
        live_mode_enabled=True,
        kill_switch_active=False,
        allowed_venues=frozenset({"kraken"}),
        allowed_markets=frozenset({"BTC/USD"}),
        min_notional_usd=Decimal("10.00"),
        max_notional_usd=Decimal("1000.00"),
        max_spread_bps=Decimal("100.00"),
        max_slippage_bps=Decimal("100.00"),
        max_open_orders=2,
        approved_strategy_versions=frozenset({"v1.0.0"}),
    )


def test_risk_rejects_when_live_disabled() -> None:
    policy = RiskPolicy(live_mode_enabled=False)
    engine = RiskEngine(policy)
    order = _sample_order()
    quote = _sample_quote()
    now = datetime.now(UTC)

    decision = engine.evaluate_order(order, quote, DataQuality.FRESH, 0, "v1.0.0", now)
    assert not decision.allowed
    assert "live mode is not enabled" in (decision.reason or "")


def test_risk_rejects_when_kill_switch_active() -> None:
    policy = RiskPolicy(live_mode_enabled=True, kill_switch_active=True)
    engine = RiskEngine(policy)
    order = _sample_order()
    quote = _sample_quote()
    now = datetime.now(UTC)

    decision = engine.evaluate_order(order, quote, DataQuality.FRESH, 0, "v1.0.0", now)
    assert not decision.allowed
    assert "kill switch is active" in (decision.reason or "")


def test_risk_rejects_disallowed_venue() -> None:
    engine = RiskEngine(_active_policy())
    order = _sample_order(venue="binance")
    quote = _sample_quote()
    now = datetime.now(UTC)

    decision = engine.evaluate_order(order, quote, DataQuality.FRESH, 0, "v1.0.0", now)
    assert not decision.allowed
    assert "binance" in (decision.reason or "")


def test_risk_rejects_disallowed_market() -> None:
    engine = RiskEngine(_active_policy())
    order = _sample_order(market="DGB/USD")
    quote = _sample_quote()
    now = datetime.now(UTC)

    decision = engine.evaluate_order(order, quote, DataQuality.FRESH, 0, "v1.0.0", now)
    assert not decision.allowed
    assert "DGB/USD" in (decision.reason or "")


def test_risk_rejects_unapproved_strategy() -> None:
    engine = RiskEngine(_active_policy())
    order = _sample_order()
    quote = _sample_quote()
    now = datetime.now(UTC)

    decision = engine.evaluate_order(
        order, quote, DataQuality.FRESH, 0, "untested-v2", now
    )
    assert not decision.allowed
    assert "untested-v2" in (decision.reason or "")


def test_risk_rejects_stale_or_missing_quote() -> None:
    engine = RiskEngine(_active_policy())
    order = _sample_order()
    now = datetime.now(UTC)

    # Missing quote
    decision = engine.evaluate_order(order, None, DataQuality.STALE, 0, "v1.0.0", now)
    assert not decision.allowed
    assert "fresh market quote" in (decision.reason or "")

    # Stale quote
    quote = _sample_quote(age_seconds=60)
    decision = engine.evaluate_order(order, quote, DataQuality.STALE, 0, "v1.0.0", now)
    assert not decision.allowed
    assert "fresh market quote" in (decision.reason or "")


def test_risk_rejects_wide_spread() -> None:
    engine = RiskEngine(_active_policy())
    order = _sample_order()
    # 2% spread (limit is 1%)
    quote = _sample_quote(bid="50000.00", ask="51000.00")
    now = datetime.now(UTC)

    decision = engine.evaluate_order(order, quote, DataQuality.FRESH, 0, "v1.0.0", now)
    assert not decision.allowed
    assert "spread" in (decision.reason or "").lower()


def test_risk_rejects_notional_bounds() -> None:
    engine = RiskEngine(_active_policy())
    now = datetime.now(UTC)
    quote = _sample_quote()

    # Below min notional ($50000 * 0.0001 = $5, min is $10)
    small_order = _sample_order(quantity="0.0001")
    decision = engine.evaluate_order(
        small_order, quote, DataQuality.FRESH, 0, "v1.0.0", now
    )
    assert not decision.allowed
    assert "below minimum" in (decision.reason or "")

    # Above max notional ($50000 * 0.05 = $2500, max is $1000)
    large_order = _sample_order(quantity="0.05")
    decision = engine.evaluate_order(
        large_order, quote, DataQuality.FRESH, 0, "v1.0.0", now
    )
    assert not decision.allowed
    assert "exceeds maximum" in (decision.reason or "")


def test_risk_rejects_excess_slippage() -> None:
    engine = RiskEngine(_active_policy())
    now = datetime.now(UTC)
    quote = _sample_quote(bid="50000.00", ask="50050.00")

    # Buy limit 2% above ask (limit is 1% slippage)
    bad_buy = _sample_order(
        quantity="0.01",
        side=SpotSide.BUY,
        limit_price="51100.00",
    )
    decision = engine.evaluate_order(
        bad_buy, quote, DataQuality.FRESH, 0, "v1.0.0", now
    )
    assert not decision.allowed
    assert "slippage" in (decision.reason or "").lower()


def test_risk_rejects_max_open_orders_exceeded() -> None:
    engine = RiskEngine(_active_policy())
    now = datetime.now(UTC)
    order = _sample_order()
    quote = _sample_quote()

    decision = engine.evaluate_order(order, quote, DataQuality.FRESH, 2, "v1.0.0", now)
    assert not decision.allowed
    assert "active orders" in (decision.reason or "")


def test_risk_approves_valid_spot_order() -> None:
    engine = RiskEngine(_active_policy())
    now = datetime.now(UTC)
    order = _sample_order(quantity="0.01", limit_price="50060.00")
    quote = _sample_quote(bid="50000.00", ask="50050.00")

    decision = engine.evaluate_order(order, quote, DataQuality.FRESH, 0, "v1.0.0", now)
    assert decision.allowed
    assert decision.reason is None
    assert decision.decision_id.startswith("risk-")


# --- Portfolio-driven checks (RE-01), plan hash / deviation / gates (RE-06/10) ---


def _pf(**kw: object) -> PortfolioState:
    base: dict[str, object] = {
        "hot_equity_usd": Decimal("10000"),
        "peak_equity_usd": Decimal("10000"),
        "realized_pnl_today_usd": Decimal("0"),
        "available_quote_usd": Decimal("5000"),
        "base_balances": {"BTC": Decimal("1")},
    }
    base.update(kw)
    return PortfolioState(**base)  # type: ignore[arg-type]


def _eval(order: SpotOrder, **kw: object) -> RiskEvaluation:
    engine = RiskEngine(_active_policy())
    return engine.evaluate_order(
        order,
        _sample_quote(),
        DataQuality.FRESH,
        0,
        "v1.0.0",
        datetime.now(UTC),
        **kw,  # type: ignore[arg-type]
    )


def test_buy_within_risk_budget_allowed_and_oversize_rejected() -> None:
    ok = _eval(
        _sample_order(quantity="0.0003"),
        portfolio=_pf(),
        invalidation_price=Decimal("49550"),
    )
    assert ok.allowed, ok.reason
    big = _eval(
        _sample_order(quantity="0.01"),
        portfolio=_pf(),
        invalidation_price=Decimal("40000"),
    )
    assert not big.allowed and "risk budget" in (big.reason or "")


def test_buy_requires_valid_invalidation() -> None:
    d = _eval(_sample_order(quantity="0.0003"), portfolio=_pf())
    assert not d.allowed and "invalidation" in (d.reason or "")
    d = _eval(
        _sample_order(quantity="0.0003"),
        portfolio=_pf(),
        invalidation_price=Decimal("60000"),
    )
    assert not d.allowed and "not positive" in (d.reason or "")


def test_loss_drawdown_and_consecutive_loss_halts() -> None:
    order = _sample_order(quantity="0.0003")
    inv = Decimal("49550")
    for pf, text in (
        (_pf(realized_pnl_today_usd=Decimal("-100")), "Daily loss"),
        (_pf(hot_equity_usd=Decimal("9400")), "drawdown"),
        (_pf(consecutive_losses=3), "Consecutive"),
    ):
        d = _eval(order, portfolio=pf, invalidation_price=inv)
        assert not d.allowed and text in (d.reason or "")


def test_exposure_cash_and_position_limits() -> None:
    order = _sample_order(quantity="0.0003")
    inv = Decimal("49550")
    d = _eval(
        order,
        portfolio=_pf(exposure_usd={"BTC": Decimal("4990")}),
        invalidation_price=inv,
    )
    assert not d.allowed and "exposure" in (d.reason or "")
    d = _eval(
        order,
        portfolio=_pf(available_quote_usd=Decimal("10")),
        invalidation_price=inv,
    )
    assert not d.allowed and "quote balance" in (d.reason or "")
    d = _eval(order, portfolio=_pf(open_positions=2), invalidation_price=inv)
    assert not d.allowed and "positions" in (d.reason or "")


def test_sell_requires_inventory() -> None:
    sell = _sample_order(quantity="0.01", side=SpotSide.SELL)
    d = _eval(sell, portfolio=_pf(base_balances={"BTC": Decimal("0")}))
    assert not d.allowed and "inventory" in (d.reason or "")
    assert _eval(sell, portfolio=_pf()).allowed


def test_fee_edge_gate() -> None:
    d = _eval(
        _sample_order(quantity="0.0003"),
        portfolio=_pf(),
        invalidation_price=Decimal("49550"),
        expected_edge_bps=Decimal("50"),
    )
    assert not d.allowed and "edge" in (d.reason or "")
    policy = RiskPolicy(
        live_mode_enabled=True,
        allowed_markets=frozenset({"BTC/USD"}),
        estimated_fee_bps=Decimal("200"),
    )
    d = RiskEngine(policy).evaluate_order(
        _sample_order(quantity="0.0003"),
        _sample_quote(),
        DataQuality.FRESH,
        0,
        "v1.0.0",
        datetime.now(UTC),
        portfolio=_pf(),
        invalidation_price=Decimal("49550"),
    )
    assert not d.allowed and "fee" in (d.reason or "").lower()


def test_require_portfolio_state_fails_closed() -> None:
    policy = RiskPolicy(live_mode_enabled=True, require_portfolio_state=True)
    d = RiskEngine(policy).evaluate_order(
        _sample_order(),
        _sample_quote(),
        DataQuality.FRESH,
        0,
        "v1.0.0",
        datetime.now(UTC),
    )
    assert not d.allowed and "portfolio state" in (d.reason or "")


@pytest.mark.parametrize(
    ("portfolio", "expected"),
    [
        (None, "portfolio state is required"),
        (_pf(), "Total portfolio equity is required"),
        (_pf(total_equity_usd=Decimal("19999")), "portfolio limit"),
        (_pf(total_equity_usd=Decimal("20000")), None),
        (
            _pf(
                hot_equity_usd=Decimal("10001"),
                peak_equity_usd=Decimal("10001"),
                total_equity_usd=Decimal("30000"),
            ),
            "USD cap",
        ),
    ],
)
def test_hot_wallet_allocation_limits(
    portfolio: PortfolioState | None, expected: str | None
) -> None:
    policy = RiskPolicy(
        live_mode_enabled=True,
        max_hot_wallet_usd=Decimal("10000"),
        hot_fraction_of_portfolio=Decimal("0.5"),
    )
    decision = RiskEngine(policy).evaluate_order(
        _sample_order(quantity="0.0003"),
        _sample_quote(),
        DataQuality.FRESH,
        0,
        "v1.0.0",
        datetime.now(UTC),
        portfolio=portfolio,
        invalidation_price=Decimal("49550"),
    )
    assert decision.allowed == (expected is None)
    if expected is not None:
        assert expected in (decision.reason or "")


@pytest.mark.parametrize("total", [Decimal("NaN"), Decimal("9999")])
def test_portfolio_total_cannot_be_invalid_or_omit_hot_equity(total: Decimal) -> None:
    with pytest.raises(ValueError, match="total portfolio equity"):
        _pf(total_equity_usd=total)


def test_limit_price_deviation_both_sides() -> None:
    low_buy = _eval(_sample_order(limit_price="40000.00"))
    assert not low_buy.allowed and "below bid" in (low_buy.reason or "")
    high_sell = _eval(_sample_order(side=SpotSide.SELL, limit_price="60000.00"))
    assert not high_sell.allowed and "above ask" in (high_sell.reason or "")


def test_health_reconcile_and_market_gates() -> None:
    order = _sample_order()
    assert "unhealthy" in (_eval(order, venue_healthy=False).reason or "")
    stale = datetime.now(UTC) - timedelta(hours=1)
    assert "reconciliation" in (_eval(order, last_reconciled_at=stale).reason or "")
    assert "unresolved" in (_eval(order, unresolved_market_orders=1).reason or "")
    fresh = datetime.now(UTC)
    assert _eval(order, venue_healthy=True, last_reconciled_at=fresh).allowed


@pytest.mark.parametrize(
    ("healthy", "age_seconds", "expected"),
    [
        (None, 0, "venue health is required"),
        (False, 0, "venue is unhealthy"),
        (True, None, "venue reconciliation is required"),
        (True, 301, "reconciliation is stale"),
        (True, -1, "reconciliation is stale"),
        (True, 300, None),
    ],
)
def test_live_reconciliation_window(
    healthy: bool | None, age_seconds: int | None, expected: str | None
) -> None:
    quote = _sample_quote()
    at = quote.received_at
    engine = RiskEngine(replace(_active_policy(), require_venue_reconciliation=True))
    decision = engine.evaluate_order(
        _sample_order(),
        quote,
        DataQuality.FRESH,
        0,
        "v1.0.0",
        at,
        venue_healthy=healthy,
        last_reconciled_at=(
            at - timedelta(seconds=age_seconds) if age_seconds is not None else None
        ),
    )
    assert decision.allowed == (expected is None)
    if expected is not None:
        assert expected in (decision.reason or "")


def test_plan_hash_bound_and_mutation_changes_it() -> None:
    order = _sample_order(limit_price="50060.00")
    d = _eval(order)
    assert d.plan_hash == spot_order_plan_hash(order)
    for mutated in (
        replace(order, quantity=order.quantity + Decimal("0.001")),
        replace(order, limit_price=Decimal("50061.00")),
        replace(order, side=SpotSide.SELL),
        replace(order, market="ETH/USD"),
    ):
        assert spot_order_plan_hash(mutated) != d.plan_hash
    assert spot_order_plan_hash(replace(order, risk_decision_id="x")) == d.plan_hash


def test_default_policy_markets_within_executable_scope() -> None:
    assert RiskPolicy().allowed_markets <= SUPPORTED_MARKETS
