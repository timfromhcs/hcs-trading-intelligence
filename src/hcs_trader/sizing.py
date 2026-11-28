"""Deterministic spot position sizing for research previews.

This module never submits an order and never authorizes one. It computes an
upper bound from explicit inputs; the deterministic risk engine still makes the
final decision. Every monetary value is an exact Decimal and nothing is rounded
silently.
"""

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal


class SizingRejected(ValueError):
    """Inputs cannot produce a bounded, valid long quantity."""


@dataclass(frozen=True, slots=True)
class SizingInput:
    hot_equity: Decimal
    entry_price: Decimal
    invalidation_price: Decimal
    risk_fraction: Decimal
    max_position_notional: Decimal
    available_quote: Decimal
    estimated_cost_per_unit: Decimal = Decimal("0")
    min_quantity: Decimal = Decimal("0")
    quantity_decimals: int = 8

    def __post_init__(self) -> None:
        values = (
            self.hot_equity,
            self.entry_price,
            self.invalidation_price,
            self.risk_fraction,
            self.max_position_notional,
            self.available_quote,
            self.estimated_cost_per_unit,
            self.min_quantity,
        )
        if not all(
            isinstance(value, Decimal) and value.is_finite() for value in values
        ):
            raise SizingRejected("All inputs must be finite Decimals")
        if self.hot_equity <= 0 or self.entry_price <= 0:
            raise SizingRejected("Equity and entry price must be positive")
        if self.invalidation_price < 0 or self.invalidation_price >= self.entry_price:
            raise SizingRejected("Invalidation must be below the entry price")
        if not Decimal("0") < self.risk_fraction <= Decimal("1"):
            raise SizingRejected("Risk fraction must be in (0, 1]")
        if self.max_position_notional <= 0 or self.available_quote < 0:
            raise SizingRejected("Notional cap and cash balance are invalid")
        if self.estimated_cost_per_unit < 0 or self.min_quantity < 0:
            raise SizingRejected("Costs and minimums cannot be negative")
        if not 0 <= self.quantity_decimals <= 18:
            raise SizingRejected("Quantity precision is outside the supported range")


@dataclass(frozen=True, slots=True)
class SizingResult:
    quantity: Decimal
    notional: Decimal
    risk_amount: Decimal
    risk_per_unit: Decimal
    bounds: tuple[str, str, str]


def _decimal_places(value: Decimal) -> int:
    exponent = value.as_tuple().exponent
    if not isinstance(exponent, int):
        raise SizingRejected("Quantity must be finite")
    return max(0, -exponent)


def size_long_position(data: SizingInput) -> SizingResult:
    """Bound a long spot order by risk, notional cap and available cash."""
    risk_per_unit = (
        data.entry_price - data.invalidation_price + data.estimated_cost_per_unit
    )
    if risk_per_unit <= 0:
        raise SizingRejected("Risk per unit must be positive")
    risk_amount = data.hot_equity * data.risk_fraction
    by_risk = risk_amount / risk_per_unit
    by_cap = data.max_position_notional / data.entry_price
    by_cash = data.available_quote / data.entry_price
    bounds = (
        f"{by_risk:.18f}",
        f"{by_cap:.18f}",
        f"{by_cash:.18f}",
    )
    quantity = min(by_risk, by_cap, by_cash)
    # Round down to the venue precision: rounding down can only reduce exposure.
    quantum = Decimal(1).scaleb(-data.quantity_decimals)
    quantity = quantity.quantize(quantum, rounding=ROUND_FLOOR)
    if quantity <= 0:
        raise SizingRejected("No positive quantity fits the bounds")
    if quantity < data.min_quantity:
        raise SizingRejected("Quantity is below the configured minimum")
    if _decimal_places(quantity) > data.quantity_decimals:
        raise SizingRejected("Quantity exceeds the allowlisted precision")
    return SizingResult(
        quantity=quantity,
        notional=quantity * data.entry_price,
        risk_amount=risk_amount,
        risk_per_unit=risk_per_unit,
        bounds=bounds,
    )
