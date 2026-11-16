"""Kraken authenticated spot-only adapter with withdrawals disabled."""

import base64
import binascii
import hashlib
import hmac
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime
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
            payload = response.json()
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
