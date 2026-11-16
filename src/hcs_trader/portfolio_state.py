"""Typed hot-wallet portfolio snapshot consumed by the deterministic risk engine."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from types import MappingProxyType


def _frozen(values: Mapping[str, Decimal], name: str) -> Mapping[str, Decimal]:
    for key, value in values.items():
        if not value.is_finite() or value < 0:
            raise ValueError(f"{name}[{key}] must be a non-negative finite Decimal")
    return MappingProxyType(dict(values))


@dataclass(frozen=True, slots=True)
class PortfolioState:
    """Hot-wallet state in USD terms; never includes cold-reserve funds."""

    hot_equity_usd: Decimal
    peak_equity_usd: Decimal
    realized_pnl_today_usd: Decimal
    available_quote_usd: Decimal
    exposure_usd: Mapping[str, Decimal] = field(default_factory=dict)
    base_balances: Mapping[str, Decimal] = field(default_factory=dict)
    open_positions: int = 0
    consecutive_losses: int = 0

    def __post_init__(self) -> None:
        for name in (
            "hot_equity_usd",
            "peak_equity_usd",
            "realized_pnl_today_usd",
            "available_quote_usd",
        ):
            if not getattr(self, name).is_finite():
                raise ValueError(f"{name} must be finite")
        if self.hot_equity_usd <= 0 or self.peak_equity_usd < self.hot_equity_usd:
            raise ValueError("equity must be positive and not exceed peak equity")
        if self.available_quote_usd < 0:
            raise ValueError("available_quote_usd must be non-negative")
        if self.open_positions < 0 or self.consecutive_losses < 0:
            raise ValueError("counters must be non-negative")
        object.__setattr__(self, "exposure_usd", _frozen(self.exposure_usd, "exposure"))
        object.__setattr__(
            self, "base_balances", _frozen(self.base_balances, "base_balances")
        )

    @property
    def total_exposure_usd(self) -> Decimal:
        return sum(self.exposure_usd.values(), Decimal(0))

    @property
    def drawdown_fraction(self) -> Decimal:
        return (self.peak_equity_usd - self.hot_equity_usd) / self.peak_equity_usd
