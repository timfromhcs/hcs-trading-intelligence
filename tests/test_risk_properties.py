"""Property tests: any order outside a hard risk limit is always rejected."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from hcs_trader.data.kraken import BTC_USD, SOURCE
from hcs_trader.domain.execution import SpotOrder, SpotOrderType, SpotSide
from hcs_trader.domain.lifecycle import OrderState
from hcs_trader.domain.market_data import DataQuality, Quote
from hcs_trader.portfolio_state import PortfolioState
from hcs_trader.risk import RiskEngine, RiskEvaluation, RiskPolicy

POLICY = RiskPolicy(live_mode_enabled=True)
ENGINE = RiskEngine(POLICY)
NOW = datetime(2026, 1, 1, tzinfo=UTC)
QUOTE = Quote(
    market=BTC_USD,
    source=SOURCE,
    bid=Decimal("50000"),
    ask=Decimal("50050"),
    bid_updated_at=NOW - timedelta(seconds=1),
    ask_updated_at=NOW - timedelta(seconds=1),
    received_at=NOW - timedelta(seconds=1),
)
BPS = Decimal(10000)

qty = st.decimals(Decimal("0.00000001"), Decimal("10"), places=8)
price = st.decimals(Decimal("1"), Decimal("200000"), places=2)
money = st.decimals(Decimal("0"), Decimal("1000000"), places=2)


def _order(q: Decimal, side: SpotSide, limit: Decimal | None) -> SpotOrder:
    return SpotOrder(
        client_order_id="c1",
        order_id="o1",
        venue="kraken",
        market="BTC/USD",
        side=side,
        order_type=SpotOrderType.LIMIT if limit else SpotOrderType.MARKET,
        quantity=q,
        limit_price=limit,
        state=OrderState.PREVIEWED,
        risk_decision_id="r",
        created_at=NOW,
        updated_at=NOW,
    )


def _eval(
    order: SpotOrder,
    portfolio: PortfolioState | None = None,
    invalidation: Decimal | None = None,
) -> RiskEvaluation:
    return ENGINE.evaluate_order(
        order,
        QUOTE,
        DataQuality.FRESH,
        0,
        "v1.0.0",
        NOW,
        portfolio=portfolio,
        invalidation_price=invalidation,
    )


@settings(max_examples=150, deadline=None)
@given(q=qty, side=st.sampled_from(SpotSide), limit=st.none() | price)
def test_never_allowed_outside_notional_or_price_bounds(
    q: Decimal, side: SpotSide, limit: Decimal | None
) -> None:
    d = _eval(_order(q, side, limit))
    ref = QUOTE.ask if side is SpotSide.BUY else QUOTE.bid
    if d.allowed:
        assert POLICY.min_notional_usd <= q * ref <= POLICY.max_notional_usd
        if limit is not None:
            assert abs(limit - ref) / ref <= Decimal("0.0101")


@settings(max_examples=150, deadline=None)
@given(
    q=qty,
    equity=st.decimals(Decimal("100"), Decimal("1000000"), places=2),
    inval=price,
    cash=money,
)
def test_buy_allowed_implies_risk_budget_and_cash(
    q: Decimal, equity: Decimal, inval: Decimal, cash: Decimal
) -> None:
    pf = PortfolioState(
        hot_equity_usd=equity,
        peak_equity_usd=equity,
        realized_pnl_today_usd=Decimal(0),
        available_quote_usd=cash,
    )
    d = _eval(_order(q, SpotSide.BUY, None), pf, inval)
    if d.allowed:
        ask = QUOTE.ask
        rpu = ask - inval + ask * POLICY.estimated_fee_bps / BPS
        assert rpu > 0
        assert q <= equity * POLICY.risk_fraction / rpu
        assert q * ask * (1 + POLICY.estimated_fee_bps / BPS) <= cash


@settings(max_examples=100, deadline=None)
@given(q=qty, held=money)
def test_sell_never_exceeds_inventory(q: Decimal, held: Decimal) -> None:
    base_qty = held / Decimal(100000)
    pf = PortfolioState(
        hot_equity_usd=Decimal(10000),
        peak_equity_usd=Decimal(10000),
        realized_pnl_today_usd=Decimal(0),
        available_quote_usd=Decimal(0),
        base_balances={"BTC": base_qty},
    )
    d = _eval(_order(q, SpotSide.SELL, None), pf)
    if d.allowed:
        assert q <= base_qty


@settings(max_examples=100, deadline=None)
@given(
    loss=st.decimals(Decimal("100"), Decimal("5000"), places=2),
    dd=st.decimals(Decimal("0.05"), Decimal("0.9"), places=3),
)
def test_loss_and_drawdown_halts_always_reject(loss: Decimal, dd: Decimal) -> None:
    equity = Decimal(10000)
    order = _order(Decimal("0.0003"), SpotSide.BUY, None)
    inval = Decimal("49550")
    lossy = PortfolioState(
        hot_equity_usd=equity,
        peak_equity_usd=equity,
        realized_pnl_today_usd=-loss,
        available_quote_usd=Decimal(5000),
    )
    assert not _eval(order, lossy, inval).allowed
    drawn = PortfolioState(
        hot_equity_usd=equity,
        peak_equity_usd=equity / (1 - dd),
        realized_pnl_today_usd=Decimal(0),
        available_quote_usd=Decimal(5000),
    )
    assert not _eval(order, drawn, inval).allowed
