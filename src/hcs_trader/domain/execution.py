"""Domain types for spot order execution, state lifecycle, and venue mapping."""

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from hcs_trader.domain.lifecycle import OrderState

_ID = re.compile(r"^[A-Za-z0-9_\-:./]{1,64}$")


class SpotSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class SpotOrderType(StrEnum):
    LIMIT = "limit"
    MARKET = "market"


@dataclass(frozen=True, slots=True)
class SpotCashTrade:
    """Local BTC/USD cash deltas keyed externally by the venue execution ID."""

    btc: Decimal
    usd: Decimal
    usd_fee: Decimal


@dataclass(frozen=True, slots=True)
class SpotCanaryApproval:
    candidate_id: str
    candidate_version: str
    dataset_hash: str
    key_fingerprint: str
    max_notional: Decimal
    expires_at: datetime
    stage: str
    active: bool


@dataclass(frozen=True, slots=True)
class SpotOrder:
    client_order_id: str
    order_id: str
    venue: str
    market: str
    side: SpotSide
    order_type: SpotOrderType
    quantity: Decimal
    limit_price: Decimal | None
    state: OrderState
    risk_decision_id: str
    created_at: datetime
    updated_at: datetime
    venue_order_id: str | None = None
    filled_quantity: Decimal | None = None
    average_price: Decimal | None = None
    fee: Decimal | None = None

    def __post_init__(self) -> None:
        if not _ID.match(self.client_order_id):
            raise ValueError("Invalid client_order_id")
        if not _ID.match(self.order_id):
            raise ValueError("Invalid order_id")
        if not _ID.match(self.venue):
            raise ValueError("Invalid venue")
        if not _ID.match(self.market):
            raise ValueError("Invalid market")
        if not self.quantity.is_finite() or self.quantity <= 0:
            raise ValueError("Quantity must be a positive finite Decimal")
        if self.limit_price is not None and (
            not self.limit_price.is_finite() or self.limit_price <= 0
        ):
            raise ValueError("Limit price must be a positive finite Decimal")
        if self.created_at.utcoffset() is None:
            raise ValueError("created_at must have a UTC offset")
        if self.updated_at.utcoffset() is None:
            raise ValueError("updated_at must have a UTC offset")

    def transition_to(
        self,
        new_state: OrderState,
        at: datetime,
        venue_order_id: str | None = None,
        filled_quantity: Decimal | None = None,
        average_price: Decimal | None = None,
        fee: Decimal | None = None,
    ) -> "SpotOrder":
        """Advance the order lifecycle with strict transition validation."""
        validated_state = self.state.advance(new_state)
        return SpotOrder(
            client_order_id=self.client_order_id,
            order_id=self.order_id,
            venue=self.venue,
            market=self.market,
            side=self.side,
            order_type=self.order_type,
            quantity=self.quantity,
            limit_price=self.limit_price,
            state=validated_state,
            risk_decision_id=self.risk_decision_id,
            created_at=self.created_at,
            updated_at=at,
            venue_order_id=venue_order_id or self.venue_order_id,
            filled_quantity=filled_quantity or self.filled_quantity,
            average_price=average_price or self.average_price,
            fee=fee or self.fee,
        )


def spot_order_plan_hash(order: SpotOrder) -> str:
    """Canonical SHA-256 over the economically relevant order fields.

    Excludes lifecycle state, timestamps and the risk decision id so the hash
    computed before risk evaluation stays stable through the state machine,
    while any change to venue, market, side, type, size, price or the client
    order id produces a different hash.
    """
    limit = "none" if order.limit_price is None else str(order.limit_price.normalize())
    payload = "|".join(
        (
            "spot-plan-v1",
            order.venue,
            order.market,
            order.side.value,
            order.order_type.value,
            str(order.quantity.normalize()),
            limit,
            order.client_order_id,
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
