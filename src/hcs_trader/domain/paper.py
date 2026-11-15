"""Long-only paper accounting for the BTC/USD simulation."""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Context, Decimal, localcontext
from enum import StrEnum

from hcs_trader.domain.market_data import DataQuality, Quote

_ZERO = Decimal(0)


class PaperSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True, slots=True)
class PaperOrder:
    idempotency_key: str
    side: PaperSide
    quantity: Decimal

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", self.idempotency_key):
            raise ValueError("Invalid paper order ID")
        if not isinstance(self.side, PaperSide):
            raise ValueError("Unsupported paper order side")
        if (
            not self.quantity.is_finite()
            or not 0 < self.quantity <= Decimal("100")
            or int(self.quantity.as_tuple().exponent) < -8
        ):
            raise ValueError(
                "Paper quantity must be positive with at most eight decimals"
            )


@dataclass(frozen=True, slots=True)
class PaperBalance:
    cash: Decimal = Decimal("10000")
    quantity: Decimal = _ZERO
    cost_basis: Decimal = _ZERO
    fees: Decimal = _ZERO
    realized_pnl: Decimal = _ZERO


@dataclass(frozen=True, slots=True)
class PaperFill:
    order: PaperOrder
    price: Decimal
    fee: Decimal
    executed_at: datetime
    balance: PaperBalance
    quote: Quote | None = None


class PaperRejected(ValueError):
    """A simulated order could not be filled under the ledger's rules."""


class PaperConflict(ValueError):
    """An order ID was reused with a different intent."""


def simulated_terms(order: PaperOrder, quote: Quote) -> tuple[Decimal, Decimal]:
    with localcontext(Context(prec=34)):
        if (quote.ask - quote.bid) / quote.bid > Decimal("0.01"):
            raise PaperRejected("Paper quote spread exceeds 100 basis points")
        price = quote.ask if order.side is PaperSide.BUY else quote.bid
        notional = price * order.quantity
        if not 10 <= notional <= 1000:
            raise PaperRejected("Paper order notional must be between 10 and 1000 USD")
        return price, notional * Decimal("0.0025")


def book_fill(
    balance: PaperBalance,
    order: PaperOrder,
    price: Decimal,
    fee: Decimal,
    at: datetime,
    quote: Quote | None = None,
) -> PaperFill:
    if (
        not price.is_finite()
        or not 0 < price <= Decimal("1000000000")
        or not fee.is_finite()
        or fee < 0
        or at.utcoffset() is None
    ):
        raise PaperRejected("Invalid simulated fill price, fee or execution time")
    if (
        quote is not None
        and quote.quality_at(
            at, max_age=timedelta(seconds=30), max_clock_skew=timedelta(seconds=2)
        )
        is not DataQuality.FRESH
    ):
        raise PaperRejected("Paper execution requires a fresh quote")
    with localcontext(Context(prec=34)):
        notional = order.quantity * price
        if order.side is PaperSide.BUY:
            spent = notional + fee
            if spent > balance.cash:
                raise PaperRejected("Insufficient paper USD")
            next_balance = PaperBalance(
                balance.cash - spent,
                balance.quantity + order.quantity,
                balance.cost_basis + spent,
                balance.fees + fee,
                balance.realized_pnl,
            )
        else:
            if order.quantity > balance.quantity:
                raise PaperRejected("Insufficient paper BTC")
            if fee > notional:
                raise PaperRejected("Simulated fee exceeds sale proceeds")
            proceeds = notional - fee
            remaining = balance.quantity - order.quantity
            cost = (
                balance.cost_basis * order.quantity / balance.quantity
                if remaining
                else balance.cost_basis
            )
            next_balance = PaperBalance(
                balance.cash + proceeds,
                remaining,
                balance.cost_basis - cost,
                balance.fees + fee,
                balance.realized_pnl + proceeds - cost,
            )
    return PaperFill(order, price, fee, at, next_balance, quote)
