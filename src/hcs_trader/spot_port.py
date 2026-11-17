"""Venue-agnostic spot exchange port and shared execution errors.

Every spot venue adapter (Kraken, Binance testnet, ...) satisfies this port.
The port is structural so existing adapters need no inheritance changes; the
shared error bases let callers handle ambiguity and rejection uniformly.
"""

from datetime import datetime
from typing import Protocol, runtime_checkable

from hcs_trader.domain.execution import SpotOrder


class SpotExecutionError(Exception):
    """Base error for any spot venue operation."""


class SpotOrderRejected(SpotExecutionError):
    """The venue definitively rejected the request."""


class SpotAmbiguousExecutionError(SpotExecutionError):
    """The outcome is unknown; reconcile against the venue before any retry."""


@runtime_checkable
class SpotExchangePort(Protocol):
    """Spot-only order lifecycle contract. No withdrawal or transfer methods."""

    async def submit_spot_order(self, order: SpotOrder, at: datetime) -> SpotOrder:
        """Submit once. Ambiguous outcomes return RECONCILIATION_REQUIRED."""
        ...

    async def cancel_spot_order(self, order: SpotOrder, at: datetime) -> SpotOrder:
        """Cancel an open order."""
        ...

    async def reconcile_order(self, order: SpotOrder, at: datetime) -> SpotOrder:
        """Query venue truth and advance the local order accordingly."""
        ...
