"""Kraken authenticated spot-only adapter with withdrawals disabled."""

import base64
import binascii
import hashlib
import hmac
import time
import urllib.parse
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx2

from hcs_trader.data.kraken import KrakenQuoteProvider
from hcs_trader.domain.execution import SpotOrder, SpotOrderType
from hcs_trader.domain.lifecycle import OrderState

KRAKEN_PAIR_MAP = {
    "BTC/USD": "XXBTZUSD",
    "LTC/USD": "XLTCZUSD",
    "ETH/USD": "XETHZUSD",
}
_CASH_ASSETS = {"XXBT": "BTC", "XBT": "BTC", "ZUSD": "USD", "USD": "USD"}


class KrakenExecutionError(Exception):
    """Base error for Kraken trading operations."""


class KrakenOrderRejected(KrakenExecutionError):
    """The exchange rejected the proposed spot order."""


class KrakenAmbiguousExecutionError(KrakenExecutionError):
    """Response timed out or failed with unknown state; reconciliation required."""


@dataclass(frozen=True, slots=True)
class KrakenOrderValidation:
    client_order_id: str
    market: str
    validated_at: datetime
    validated: bool = True


@dataclass(frozen=True, slots=True)
class KrakenCashBalance:
    total: Decimal
    held: Decimal

    @property
    def available(self) -> Decimal:
        return self.total - self.held


@dataclass(frozen=True, slots=True)
class KrakenSpotBalances:
    btc: KrakenCashBalance
    usd: KrakenCashBalance
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class _LedgerEntry:
    at: Decimal
    asset: str
    amount: Decimal
    fee: Decimal
    balance: Decimal


def _balance_amount(value: object, *, signed: bool = False) -> Decimal:
    try:
        if not isinstance(value, (str, int, Decimal)) or isinstance(value, bool):
            raise ValueError
        amount = Decimal(value)
    except (InvalidOperation, ValueError):
        raise KrakenExecutionError("Invalid Kraken balance amount") from None
    if not amount.is_finite() or (not signed and amount < 0):
        raise KrakenExecutionError("Invalid Kraken balance amount")
    return amount


def _parse_cash_entry(row: dict[str, Any]) -> _LedgerEntry:
    code = row.get("asset")
    asset = _CASH_ASSETS.get(code) if isinstance(code, str) else None
    if asset is None or row.get("type") not in ("deposit", "withdrawal", "trade"):
        raise KrakenExecutionError("Unsupported event in Kraken BTC/USD cash ledger")
    return _LedgerEntry(
        _balance_amount(row.get("time")),
        asset,
        _balance_amount(row.get("amount"), signed=True),
        _balance_amount(row.get("fee")),
        _balance_amount(row.get("balance")),
    )


class KrakenSpotAdapter:
    """Spot-only Kraken adapter. Margin, leverage, and withdrawals are forbidden."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        http_client: httpx2.AsyncClient,
        base_url: str = "https://api.kraken.com",
        *,
        validate_only: bool = False,
    ) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.client = http_client
        self.base_url = base_url.rstrip("/")
        self.validate_only = validate_only

    @property
    def key_fingerprint(self) -> str:
        return hashlib.sha256(self.api_key.encode("utf-8")).hexdigest()

    async def trading_is_online(self) -> bool:
        return await KrakenQuoteProvider(self.client).trading_is_online()

    def _sign_request(
        self, url_path: str, data: dict[str, Any], nonce: int
    ) -> dict[str, str]:
        """Compute Kraken HMAC-SHA512 signature over path and hashed postdata."""
        postdata = urllib.parse.urlencode(data)
        encoded = (str(nonce) + postdata).encode()
        message = url_path.encode() + hashlib.sha256(encoded).digest()
        try:
            secret_bytes = base64.b64decode(self.api_secret, validate=True)
        except (ValueError, binascii.Error):
            raise KrakenExecutionError("Invalid base64 API secret") from None
        if not secret_bytes:
            raise KrakenExecutionError("API secret must not be empty")
        mac = hmac.new(secret_bytes, message, hashlib.sha512)
        signature = base64.b64encode(mac.digest()).decode()
        return {
            "API-Key": self.api_key,
            "API-Sign": signature,
        }

    async def _post_private(
        self, endpoint: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        """Send authenticated POST request to private endpoint."""
        url_path = f"/0/private/{endpoint}"
        nonce = int(time.time() * 1000000)
        request_data = {"nonce": nonce, **data}
        headers = self._sign_request(url_path, request_data, nonce)

        try:
            response = await self.client.post(
                f"{self.base_url}{url_path}",
                data=request_data,
                headers=headers,
                timeout=10.0,
                follow_redirects=False,
            )
        except httpx2.RequestError as err:
            raise KrakenAmbiguousExecutionError(
                f"Network failure contacting Kraken private API {endpoint}"
            ) from err

        if not response.is_success:
            raise KrakenAmbiguousExecutionError(
                f"Kraken HTTP error on {endpoint}: HTTP {response.status_code}"
            )

        try:
            payload = response.json(parse_float=Decimal)
        except Exception as err:
            raise KrakenAmbiguousExecutionError(
                f"Unparseable response from Kraken on {endpoint}: {err}"
            ) from err

        if not isinstance(payload, dict):
            raise KrakenAmbiguousExecutionError("Invalid Kraken response envelope")
        errors = payload.get("error")
        if not isinstance(errors, list) or any(
            not isinstance(item, str) for item in errors
        ):
            raise KrakenAmbiguousExecutionError("Invalid Kraken error list")
        if errors:
            raise KrakenOrderRejected(f"Kraken API error: {', '.join(errors)}")

        result = payload.get("result")
        if not isinstance(result, dict):
            raise KrakenAmbiguousExecutionError("Invalid Kraken result")
        return result

    async def read_spot_balances(self) -> KrakenSpotBalances:
        """Read cash balances for the dedicated BTC/USD spot account."""
        result = await self._post_private("BalanceEx", {})
        observed_at = datetime.now(UTC)
        balances: dict[str, KrakenCashBalance] = {}
        for code, row in result.items():
            if not isinstance(row, dict) or "balance" not in row:
                raise KrakenExecutionError("Malformed Kraken balance row")
            total = _balance_amount(row["balance"])
            held = _balance_amount(row.get("hold_trade", 0))
            if _balance_amount(row.get("credit", 0)) or _balance_amount(
                row.get("credit_used", 0)
            ):
                raise KrakenExecutionError("Kraken account uses unsupported credit")
            if held > total:
                raise KrakenExecutionError("Kraken trade hold exceeds cash balance")
            asset = _CASH_ASSETS.get(code)
            if asset is None:
                if total:
                    raise KrakenExecutionError(
                        "Kraken account holds unsupported assets"
                    )
                continue
            if asset in balances:
                raise KrakenExecutionError("Duplicate Kraken cash asset")
            balances[asset] = KrakenCashBalance(total, held)
        empty = KrakenCashBalance(Decimal(0), Decimal(0))
        return KrakenSpotBalances(
            balances.get("BTC", empty), balances.get("USD", empty), observed_at
        )

    async def verify_cash_ledger(self) -> KrakenSpotBalances:
        """Replay Kraken cash history from zero and compare current totals."""
        end = int(time.time())
        entries: dict[str, _LedgerEntry] = {}
        params: dict[str, str | int] = {"with_cursor": "true", "end": end}
        seen_cursors: set[str] = set()
        for _ in range(20):
            result = await self._post_private("Ledgers", params)
            page = result.get("ledger")
            cursor = result.get("cursor")
            if not isinstance(page, dict) or (
                cursor is not None and not isinstance(cursor, dict)
            ):
                raise KrakenExecutionError("Malformed Kraken ledger page")
            for entry_id, row in page.items():
                if not entry_id or entry_id in entries or not isinstance(row, dict):
                    raise KrakenExecutionError(
                        "Duplicate or malformed Kraken ledger entry"
                    )
                entry = _parse_cash_entry(row)
                prior = next(reversed(entries.values()), None)
                if entry.at > end or (prior is not None and entry.at > prior.at):
                    raise KrakenExecutionError(
                        "Kraken ledger entries are out of chronological order"
                    )
                entries[entry_id] = entry
            next_cursor = cursor.get("next") if cursor else None
            if next_cursor is None:
                break
            if (
                not page
                or not isinstance(next_cursor, str)
                or not next_cursor
                or next_cursor in seen_cursors
            ):
                raise KrakenExecutionError("Kraken ledger pagination did not advance")
            seen_cursors.add(next_cursor)
            params["cursor"] = next_cursor
        else:
            raise KrakenExecutionError(
                "Kraken cash ledger exceeds the verification page limit"
            )

        totals = {"BTC": Decimal(0), "USD": Decimal(0)}
        for entry in reversed(entries.values()):
            if totals[entry.asset] + entry.amount - entry.fee != entry.balance:
                raise KrakenExecutionError(
                    "Kraken cash ledger does not reconcile from zero"
                )
            totals[entry.asset] = entry.balance
        snapshot = await self.read_spot_balances()
        if (snapshot.btc.total, snapshot.usd.total) != (totals["BTC"], totals["USD"]):
            raise KrakenExecutionError(
                "Kraken cash ledger differs from current balances"
            )
        return snapshot

    def _order_fields(self, order: SpotOrder) -> dict[str, str]:
        if order.venue != "kraken" or order.state != OrderState.PREVIEWED:
            raise KrakenOrderRejected("Expected a previewed Kraken order")
        mapped_pair = KRAKEN_PAIR_MAP.get(order.market)
        if not mapped_pair:
            raise KrakenOrderRejected(
                f"Market '{order.market}' has no mapped Kraken spot pair"
            )

        data = {
            "pair": mapped_pair,
            "type": order.side.value,
            "ordertype": order.order_type.value,
            "volume": f"{order.quantity:.8f}",
            "cl_ord_id": order.client_order_id,
        }
        if order.order_type is SpotOrderType.LIMIT:
            if order.limit_price is None:
                raise KrakenOrderRejected("A limit order requires a price")
            data["price"] = f"{order.limit_price:.2f}"
        return data

    async def validate_spot_order(
        self, order: SpotOrder, at: datetime
    ) -> KrakenOrderValidation:
        if not self.validate_only:
            raise KrakenOrderRejected("Validation requires a validation-only adapter")
        fields = self._order_fields(order)
        result = await self._post_private("AddOrder", {**fields, "validate": "true"})
        description = result.get("descr")
        if result.get("txid") not in (None, [], "") or not (
            isinstance(description, dict)
            and isinstance(description.get("order"), str)
            and description["order"]
        ):
            raise KrakenAmbiguousExecutionError(
                "Kraken did not confirm validation-only"
            )
        return KrakenOrderValidation(order.client_order_id, order.market, at)

    async def submit_spot_order(self, order: SpotOrder, at: datetime) -> SpotOrder:
        """Submit a long spot limit/market order to Kraken."""
        if self.validate_only:
            raise KrakenOrderRejected("Validation-only adapter cannot send an order")
        data = self._order_fields(order)
        submitted_order = order.transition_to(OrderState.SUBMITTED, at=at)

        try:
            result = await self._post_private("AddOrder", data)
        except KrakenAmbiguousExecutionError:
            # Must transition to RECONCILIATION_REQUIRED
            return submitted_order.transition_to(
                OrderState.RECONCILIATION_REQUIRED, at=at
            )
        except KrakenOrderRejected:
            return submitted_order.transition_to(OrderState.REJECTED, at=at)

        txids = result.get("txid", [])
        venue_txid = (
            txids[0]
            if isinstance(txids, list)
            and len(txids) == 1
            and isinstance(txids[0], str)
            and txids[0]
            else None
        )
        if venue_txid is None:
            return submitted_order.transition_to(
                OrderState.RECONCILIATION_REQUIRED, at=at
            )
        return submitted_order.transition_to(
            OrderState.ACKNOWLEDGED, at=at, venue_order_id=venue_txid
        )

    async def cancel_spot_order(self, order: SpotOrder, at: datetime) -> SpotOrder:
        """Cancel an open spot order on Kraken."""
        if self.validate_only:
            raise KrakenOrderRejected("Validation-only adapter cannot cancel an order")
        if not order.venue_order_id:
            raise KrakenOrderRejected("Cannot cancel order without venue_order_id")
        await self._post_private("CancelOrder", {"txid": order.venue_order_id})
        return order.transition_to(OrderState.CANCELLED, at=at)

    async def query_order_status(self, venue_order_id: str) -> dict[str, Any]:
        """Query single order status from Kraken."""
        result = await self._post_private("QueryOrders", {"txid": venue_order_id})
        info = result.get(venue_order_id, {})
        return dict(info) if isinstance(info, dict) else {}

    async def verify_open_orders(self, expected: Sequence[SpotOrder]) -> None:
        """Compare the authenticated account's open orders with the local ledger."""
        local = {order.venue_order_id: order for order in expected}
        if (
            None in local
            or len(local) != len(expected)
            or any(
                order.state
                not in {OrderState.ACKNOWLEDGED, OrderState.PARTIALLY_FILLED}
                or order.venue != "kraken"
                or order.market != "BTC/USD"
                or order.order_type != SpotOrderType.LIMIT
                for order in expected
            )
        ):
            raise KrakenExecutionError("Local Kraken orders require reconciliation")
        result = await self._post_private("OpenOrders", {})
        remote = result.get("open")
        if not isinstance(remote, dict) or result.get("cursor"):
            raise KrakenExecutionError("Incomplete Kraken open order snapshot")
        if remote.keys() != local.keys():
            raise KrakenExecutionError(
                "Kraken open order identities differ from the ledger"
            )
        for venue_id, order in local.items():
            if not self._matches_open_order(order, remote[venue_id]):
                raise KrakenExecutionError(
                    "Kraken open order record differs from the ledger"
                )

    @staticmethod
    def _matches_open_order(order: SpotOrder, entry: object) -> bool:
        if not isinstance(entry, dict) or entry.get("status") not in (
            "open",
            "pending",
        ):
            return False
        description = entry.get("descr")
        if not isinstance(description, dict) or (
            entry.get("cl_ord_id") != order.client_order_id
            or description.get("pair") not in ("XBTUSD", "XXBTZUSD", "XBT/USD")
            or description.get("type") != order.side.value
            or description.get("ordertype") != "limit"
            or description.get("leverage") != "none"
        ):
            return False
        try:
            filled = _balance_amount(entry.get("vol_exec"))
            cost = _balance_amount(entry.get("cost"))
            fee = _balance_amount(entry.get("fee"))
            return (
                _balance_amount(entry.get("vol")) == order.quantity
                and filled == (order.filled_quantity or 0)
                and (cost / filled if filled else None) == order.average_price
                and (filled > 0 or cost == 0)
                and fee == (order.fee or 0)
                and _balance_amount(description.get("price")) == order.limit_price
            )
        except KrakenExecutionError:
            return False

    async def reconcile_order(self, order: SpotOrder, at: datetime) -> SpotOrder:
        """Reconcile an ambiguous or submitted order against venue state."""
        if order.state not in {
            OrderState.SUBMITTED,
            OrderState.ACKNOWLEDGED,
            OrderState.PARTIALLY_FILLED,
            OrderState.RECONCILIATION_REQUIRED,
        }:
            return order
        try:
            venue_id = order.venue_order_id
            if venue_id is None:
                venue_id, info = await self._find_order(order)
            else:
                info = await self.query_order_status(venue_id)
            if (
                not venue_id
                or not info
                or (info.get("cl_ord_id") not in (None, order.client_order_id))
            ):
                raise KrakenAmbiguousExecutionError("Order identity is unconfirmed")
            status = info.get("status")
            volume = Decimal(str(info["vol_exec"]))
            cost = Decimal(str(info["cost"]))
            fee = Decimal(str(info["fee"]))
            if (
                any(not value.is_finite() or value < 0 for value in (volume, cost, fee))
                or not (order.filled_quantity or Decimal(0)) <= volume <= order.quantity
                or (volume > 0 and cost == 0)
            ):
                raise KrakenAmbiguousExecutionError("Invalid cumulative order values")
            if status == "closed" and volume == order.quantity:
                state = OrderState.FILLED
            elif status in ("canceled", "expired"):
                state = OrderState.CANCELLED
            elif status in ("open", "pending") and volume < order.quantity:
                state = (
                    OrderState.PARTIALLY_FILLED if volume else OrderState.ACKNOWLEDGED
                )
            else:
                raise KrakenAmbiguousExecutionError("Order status remains unconfirmed")
            if state == order.state and state == OrderState.ACKNOWLEDGED:
                return order
            return order.transition_to(
                state,
                at=at,
                venue_order_id=venue_id,
                filled_quantity=volume,
                average_price=cost / volume if volume else None,
                fee=fee,
            )
        except (KrakenExecutionError, KeyError, InvalidOperation):
            if order.state is OrderState.RECONCILIATION_REQUIRED:
                return order
            return order.transition_to(OrderState.RECONCILIATION_REQUIRED, at=at)

    async def _find_order(self, order: SpotOrder) -> tuple[str | None, dict[str, Any]]:
        matches: dict[str, dict[str, Any]] = {}
        filters = {"cl_ord_id": order.client_order_id}
        for endpoint, field in (("OpenOrders", "open"), ("ClosedOrders", "closed")):
            result = await self._post_private(endpoint, filters)
            entries = result.get(field)
            if not isinstance(entries, dict):
                raise KrakenAmbiguousExecutionError("Invalid Kraken order collection")
            for order_id, entry in entries.items():
                if (
                    isinstance(entry, dict)
                    and entry.get("cl_ord_id") == order.client_order_id
                ):
                    matches[order_id] = entry
        return next(iter(matches.items())) if len(matches) == 1 else (None, {})
