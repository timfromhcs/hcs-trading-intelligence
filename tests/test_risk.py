"""Tests for deterministic pre-trade risk engine and policy bounds."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from hcs_trader.data.kraken import BTC_USD, SOURCE
from hcs_trader.domain.execution import SpotOrder, SpotOrderType, SpotSide
from hcs_trader.domain.lifecycle import OrderState
from hcs_trader.domain.market_data import DataQuality, Quote
from hcs_trader.risk import RiskEngine, RiskPolicy


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
