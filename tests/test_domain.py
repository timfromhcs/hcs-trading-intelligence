from decimal import Decimal

import pytest

from hcs_trader.domain.assets import AssetId, MarketId, Money, NetworkId, Quantity
from hcs_trader.domain.lifecycle import OrderState, TransactionState


def test_asset_identity_separates_network_and_namespace() -> None:
    cash = AssetId.parse("fiat:iso4217/currency:EUR")
    token = AssetId.parse("eip155:1/erc20:fixture-token")
    assert str(cash) == "fiat:iso4217/currency:EUR"
    assert token != AssetId.parse("eip155:10/erc20:fixture-token")
    assert MarketId("fixture", token, cash).quote == cash


@pytest.mark.parametrize(
    "value",
    ["EUR", "EIP155:1/erc20:token", "eip155:1/ERC20:token", "eip155:1/erc20:token:two"],
)
def test_invalid_asset_id_is_rejected(value: str) -> None:
    with pytest.raises(ValueError):
        AssetId.parse(value)


def test_market_requires_distinct_assets_and_valid_venue() -> None:
    euro = AssetId.parse("fiat:iso4217/currency:EUR")
    with pytest.raises(ValueError, match="differ"):
        MarketId("fixture", euro, euro)
    with pytest.raises(ValueError, match="venue"):
        MarketId("bad venue", euro, euro)


def test_money_preserves_exact_signed_decimal() -> None:
    euro = AssetId.parse("fiat:iso4217/currency:EUR")
    assert Money(euro, Decimal("-0.001")).amount == Decimal("-0.001")


@pytest.mark.parametrize("value", [Decimal("NaN"), Decimal("Infinity"), 0.1])
def test_money_rejects_ambiguous_or_nonfinite_amounts(value: Decimal) -> None:
    with pytest.raises(ValueError, match="finite Decimal"):
        Money(AssetId.parse("fiat:iso4217/currency:EUR"), value)


@pytest.mark.parametrize("value", [-1, True, Decimal("1")])
def test_quantity_rejects_invalid_base_units(value: int) -> None:
    asset = AssetId(NetworkId("fixture:chain"), "native", "coin")
    assert Quantity(asset, 0).base_units == 0
    with pytest.raises(ValueError, match="integer base units"):
        Quantity(asset, value)


def test_ambiguous_order_must_reconcile_before_progress() -> None:
    submitted = OrderState.PREVIEWED.advance(OrderState.SUBMITTED)
    unknown = submitted.advance(OrderState.RECONCILIATION_REQUIRED)
    with pytest.raises(ValueError, match="Invalid order transition"):
        unknown.advance(OrderState.SUBMITTED)
    partial = unknown.advance(OrderState.PARTIALLY_FILLED)
    assert partial.advance(OrderState.PARTIALLY_FILLED) is partial
    filled = partial.advance(OrderState.FILLED)
    with pytest.raises(ValueError):
        filled.advance(OrderState.CANCELLED)


def test_ambiguous_transaction_must_reconcile_before_progress() -> None:
    signed = TransactionState.PREVIEWED.advance(TransactionState.SIGNED)
    submitted = signed.advance(TransactionState.SUBMITTED)
    unknown = submitted.advance(TransactionState.RECONCILIATION_REQUIRED)
    with pytest.raises(ValueError, match="Invalid transaction transition"):
        unknown.advance(TransactionState.SUBMITTED)
    broadcast = unknown.advance(TransactionState.BROADCAST)
    confirmed = broadcast.advance(TransactionState.CONFIRMED)
    with pytest.raises(ValueError):
        confirmed.advance(TransactionState.SIGNED)
