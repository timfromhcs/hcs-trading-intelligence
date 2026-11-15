"""Allowed transitions for spot orders and chain transactions."""

from enum import StrEnum


class OrderState(StrEnum):
    PREVIEWED = "PREVIEWED"
    SUBMITTED = "SUBMITTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"

    def advance(self, target: "OrderState") -> "OrderState":
        if not isinstance(target, OrderState) or target not in _ORDER_TRANSITIONS.get(
            self, ()
        ):
            raise ValueError(f"Invalid order transition: {self} -> {target}")
        return target


_O = OrderState
_ORDER_TRANSITIONS = {
    _O.PREVIEWED: (_O.SUBMITTED, _O.REJECTED, _O.CANCELLED),
    _O.SUBMITTED: (
        _O.ACKNOWLEDGED,
        _O.PARTIALLY_FILLED,
        _O.FILLED,
        _O.REJECTED,
        _O.CANCELLED,
        _O.RECONCILIATION_REQUIRED,
    ),
    _O.ACKNOWLEDGED: (
        _O.PARTIALLY_FILLED,
        _O.FILLED,
        _O.CANCELLED,
        _O.RECONCILIATION_REQUIRED,
    ),
    _O.PARTIALLY_FILLED: (
        _O.PARTIALLY_FILLED,
        _O.FILLED,
        _O.CANCELLED,
        _O.RECONCILIATION_REQUIRED,
    ),
    _O.RECONCILIATION_REQUIRED: (
        _O.ACKNOWLEDGED,
        _O.PARTIALLY_FILLED,
        _O.FILLED,
        _O.REJECTED,
        _O.CANCELLED,
    ),
}


class TransactionState(StrEnum):
    PREVIEWED = "PREVIEWED"
    SIGNED = "SIGNED"
    SUBMITTED = "SUBMITTED"
    BROADCAST = "BROADCAST"
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"

    def advance(self, target: "TransactionState") -> "TransactionState":
        if not isinstance(
            target, TransactionState
        ) or target not in _TRANSACTION_TRANSITIONS.get(self, ()):
            raise ValueError(f"Invalid transaction transition: {self} -> {target}")
        return target


_T = TransactionState
_TRANSACTION_TRANSITIONS = {
    _T.PREVIEWED: (_T.SIGNED, _T.FAILED),
    _T.SIGNED: (_T.SUBMITTED, _T.FAILED),
    _T.SUBMITTED: (
        _T.BROADCAST,
        _T.CONFIRMED,
        _T.FAILED,
        _T.RECONCILIATION_REQUIRED,
    ),
    _T.BROADCAST: (_T.CONFIRMED, _T.FAILED, _T.RECONCILIATION_REQUIRED),
    _T.RECONCILIATION_REQUIRED: (_T.BROADCAST, _T.CONFIRMED, _T.FAILED),
}
