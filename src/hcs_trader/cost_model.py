"""One deterministic transaction-cost model shared by research previews.

The formulas are the ones the backtester already uses: taker fee on notional,
half the quoted spread plus explicit slippage as price impact. Keeping them in
one place lets backtest, paper and risk quote identical costs. No module here
places an order or relaxes a risk limit.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Protocol

_BPS = Decimal(10_000)


def _require_positive(value: object, name: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise ValueError(f"{name} must be a positive finite Decimal")


def _require_non_negative(value: object, name: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise ValueError(f"{name} must be a non-negative finite Decimal")


class _CostConfig(Protocol):
    @property
    def fee_bps(self) -> Decimal: ...

    @property
    def spread_bps(self) -> Decimal: ...

    @property
    def slippage_bps(self) -> Decimal: ...


@dataclass(frozen=True, slots=True)
class CostModel:
    fee_bps: Decimal = Decimal("25")
    spread_bps: Decimal = Decimal("10")
    slippage_bps: Decimal = Decimal("5")

    def __post_init__(self) -> None:
        for name in ("fee_bps", "spread_bps", "slippage_bps"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ValueError(f"{name} must be a finite Decimal")
            if not Decimal(0) <= value <= Decimal(500):
                raise ValueError(f"{name} must be between 0 and 500 basis points")

    @property
    def fee_rate(self) -> Decimal:
        return self.fee_bps / _BPS

    @property
    def impact_rate(self) -> Decimal:
        return (self.spread_bps / 2 + self.slippage_bps) / _BPS

    def execution_price(
        self, reference: Decimal, side: Literal["buy", "sell"]
    ) -> Decimal:
        """Apply adverse price impact to a reference price."""
        _require_positive(reference, "Reference price")
        if side not in ("buy", "sell"):
            raise ValueError("Side must be buy or sell")
        factor = 1 + self.impact_rate if side == "buy" else 1 - self.impact_rate
        return reference * factor

    def fee(self, notional: Decimal) -> Decimal:
        _require_non_negative(notional, "Notional")
        return notional * self.fee_rate

    def slippage_cost(self, quantity: Decimal, reference: Decimal) -> Decimal:
        _require_non_negative(quantity, "Quantity")
        _require_positive(reference, "Reference price")
        return quantity * reference * self.impact_rate

    @classmethod
    def from_config(cls, config: _CostConfig) -> "CostModel":
        """Build the shared model from any config with the same cost fields."""
        return cls(
            fee_bps=config.fee_bps,
            spread_bps=config.spread_bps,
            slippage_bps=config.slippage_bps,
        )
