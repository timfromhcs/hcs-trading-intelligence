"""Shared deterministic transaction-cost model."""

from decimal import Decimal

import pytest

from hcs_trader.backtest import BacktestConfig
from hcs_trader.cost_model import CostModel


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
