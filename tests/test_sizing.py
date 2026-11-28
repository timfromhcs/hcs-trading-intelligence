"""Deterministic position sizing and its read-only REST preview."""

from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from hcs_trader.api import create_app
from hcs_trader.sizing import SizingInput, SizingRejected, size_long_position
from hcs_trader.storage import AuditStore

_BASE = SizingInput(
    hot_equity=Decimal("10000"),
    entry_price=Decimal("100"),
    invalidation_price=Decimal("95"),
    risk_fraction=Decimal("0.0025"),
    max_position_notional=Decimal("2000"),
    available_quote=Decimal("5000"),
    estimated_cost_per_unit=Decimal("0.5"),
    min_quantity=Decimal("0.01"),
    quantity_decimals=8,
)


def _input(**overrides: Decimal) -> SizingInput:
    return replace(_BASE, **cast(dict[str, Any], overrides))


def test_size_is_bounded_by_risk_budget() -> None:
    result = size_long_position(_input())
    # risk budget 25, risk/unit 5.5 -> 4.5454...; cap 20; cash 50.
    assert result.quantity < Decimal("4.55")
    assert result.risk_amount == Decimal("25.00")
    assert result.risk_per_unit == Decimal("5.5")
    assert result.notional <= Decimal("2000")


def test_size_is_bounded_by_cash() -> None:
    result = size_long_position(_input(available_quote=Decimal("150")))
    assert result.quantity == Decimal("1.5")


def test_size_is_bounded_by_notional_cap() -> None:
    result = size_long_position(_input(max_position_notional=Decimal("250")))
    assert result.quantity == Decimal("2.5")


@pytest.mark.parametrize(
    "overrides",
    [
        {"invalidation_price": Decimal("100")},
        {"invalidation_price": Decimal("101")},
        {"risk_fraction": Decimal("0")},
        {"hot_equity": Decimal("-1")},
        {"available_quote": Decimal("-1")},
        {"estimated_cost_per_unit": Decimal("-1")},
        {"min_quantity": Decimal("100")},
    ],
)
def test_invalid_inputs_are_rejected(overrides: dict[str, Decimal]) -> None:
    with pytest.raises(SizingRejected):
        size_long_position(_input(**overrides))


def test_nan_and_infinite_inputs_are_rejected() -> None:
    with pytest.raises(SizingRejected):
        _input(entry_price=Decimal("NaN"))
    with pytest.raises(SizingRejected):
        _input(hot_equity=Decimal("Infinity"))


def test_rest_preview_is_read_only(tmp_path: Path) -> None:
    store = AuditStore(tmp_path / "audit.sqlite")
    store.initialize()
    client = TestClient(create_app(store), base_url="http://127.0.0.1")
    payload = {
        "hot_equity": "10000",
        "entry_price": "100",
        "invalidation_price": "95",
        "risk_fraction": "0.0025",
        "max_position_notional": "2000",
        "available_quote": "5000",
        "estimated_cost_per_unit": "0.5",
        "min_quantity": "0.01",
    }
    response = client.post("/api/v1/quant/size", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["authorization"] == "NONE"
    assert Decimal(body["quantity"]) > 0
    assert client.get("/api/v1/orders/spot").json() == []


def test_rest_preview_rejects_bad_input(tmp_path: Path) -> None:
    store = AuditStore(tmp_path / "audit.sqlite")
    store.initialize()
    client = TestClient(create_app(store), base_url="http://127.0.0.1")
    payload = {
        "hot_equity": "10000",
        "entry_price": "100",
        "invalidation_price": "100",
        "risk_fraction": "0.0025",
        "max_position_notional": "2000",
        "available_quote": "5000",
    }
    assert client.post("/api/v1/quant/size", json=payload).status_code == 422
