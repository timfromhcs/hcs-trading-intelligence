"""Shared deterministic transaction-cost model."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from hcs_trader.backtest import BacktestConfig
from hcs_trader.cost_model import CostModel
from hcs_trader.domain.market_data import Quote
from hcs_trader.domain.markets import DEFAULT_MARKET_ID
from hcs_trader.domain.paper import PaperOrder, PaperSide, simulated_terms


def test_default_costs_match_the_backtest_formulas() -> None:
    model = CostModel()
    assert model.fee_rate == Decimal("25") / Decimal(10_000)
    assert model.impact_rate == (Decimal("10") / 2 + Decimal("5")) / Decimal(10_000)
    assert model.execution_price(Decimal("100"), "buy") == Decimal("100.1")
    assert model.execution_price(Decimal("100"), "sell") == Decimal("99.9")
    assert model.fee(Decimal("1000")) == Decimal("2.5")
    assert model.slippage_cost(Decimal("2"), Decimal("100")) == Decimal("0.2")


def test_config_bridge_uses_the_same_values() -> None:
    config = BacktestConfig(
        fee_bps=Decimal("40"), spread_bps=Decimal("12"), slippage_bps=Decimal("8")
    )
    model = CostModel.from_config(config)
    assert model.fee_bps == config.fee_bps
    assert model.impact_rate == (Decimal("6") + Decimal("8")) / Decimal(10_000)


def test_paper_fee_matches_the_shared_model() -> None:
    now = datetime.now(UTC)
    quote = Quote(
        DEFAULT_MARKET_ID,
        "fixture",
        Decimal("100"),
        Decimal("100.1"),
        now - timedelta(seconds=1),
        now - timedelta(seconds=1),
        now,
    )
    price, fee = simulated_terms(
        PaperOrder("paper-1", PaperSide.BUY, Decimal("1")), quote
    )
    assert price == quote.ask
    assert fee == CostModel().fee(price)
    assert fee == price * Decimal("0.0025")


def test_zero_costs_are_allowed() -> None:
    model = CostModel(
        fee_bps=Decimal("0"), spread_bps=Decimal("0"), slippage_bps=Decimal("0")
    )
    assert model.execution_price(Decimal("100"), "buy") == Decimal("100")
    assert model.fee(Decimal("1000")) == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"fee_bps": Decimal("-1")},
        {"spread_bps": Decimal("501")},
        {"slippage_bps": Decimal("NaN")},
        {"fee_bps": Decimal("Infinity")},
    ],
)
def test_invalid_basis_points_are_rejected(kwargs: dict[str, Decimal]) -> None:
    with pytest.raises(ValueError):
        CostModel(**kwargs)


def test_invalid_prices_and_quantities_are_rejected() -> None:
    model = CostModel()
    with pytest.raises(ValueError):
        model.execution_price(Decimal("0"), "buy")
    with pytest.raises(ValueError):
        model.execution_price(Decimal("100"), "hold")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        model.fee(Decimal("-1"))
    with pytest.raises(ValueError):
        model.slippage_cost(Decimal("-1"), Decimal("100"))


@given(
    fee=st.decimals(min_value="0", max_value="500", places=2),
    spread=st.decimals(min_value="0", max_value="500", places=2),
    slippage=st.decimals(min_value="0", max_value="500", places=2),
    price=st.decimals(min_value="0.01", max_value="1000000", places=2),
    quantity=st.decimals(min_value="0", max_value="100000", places=4),
    notional=st.decimals(min_value="0", max_value="1000000", places=2),
)
def test_costs_are_non_negative_and_adverse(
    fee: Decimal,
    spread: Decimal,
    slippage: Decimal,
    price: Decimal,
    quantity: Decimal,
    notional: Decimal,
) -> None:
    model = CostModel(fee_bps=fee, spread_bps=spread, slippage_bps=slippage)
    buy = model.execution_price(price, "buy")
    sell = model.execution_price(price, "sell")
    assert buy >= price >= sell
    assert model.fee(notional) >= 0
    assert model.slippage_cost(quantity, price) >= 0
    if fee == spread == slippage == 0:
        assert buy == sell == price
        assert model.fee(notional) == 0
