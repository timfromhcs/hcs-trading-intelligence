"""Kraken authenticated spot-only adapter with withdrawals disabled."""

import base64
import hashlib
import hmac
import time
import urllib.parse
from datetime import datetime
from decimal import Decimal
from typing import Any

import httpx2

from hcs_trader.domain.execution import SpotOrder, SpotOrderType
from hcs_trader.domain.lifecycle import OrderState

KRAKEN_PAIR_MAP = {
    "BTC/USD": "XXBTZUSD",
}


class KrakenExecutionError(Exception):
    """Base error for Kraken trading operations."""


class KrakenOrderRejected(KrakenExecutionError):
    """The exchange rejected the proposed spot order."""


class KrakenAmbiguousExecutionError(KrakenExecutionError):
    """Response timed out or failed with unknown state; reconciliation required."""


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

    def _sign_request(
        self, url_path: str, data: dict[str, Any], nonce: int
    ) -> dict[str, str]:
        """Compute Kraken HMAC-SHA512 signature over path and hashed postdata."""
        postdata = urllib.parse.urlencode(data)
        encoded = (str(nonce) + postdata).encode()
        message = url_path.encode() + hashlib.sha256(encoded).digest()
        try:
            secret_bytes = base64.b64decode(self.api_secret)
        except Exception as err:
            raise KrakenExecutionError(f"Invalid base64 API secret: {err}") from err
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
            )
        except (httpx2.TimeoutException, httpx2.NetworkError) as err:
            raise KrakenAmbiguousExecutionError(
                f"Network timeout contacting Kraken private API {endpoint}: {err}"
            ) from err

        if response.status_code >= 500:
            raise KrakenAmbiguousExecutionError(
                f"Kraken 5xx server error on {endpoint}: HTTP {response.status_code}"
            )

        try:
            payload = response.json()
        except Exception as err:
            raise KrakenAmbiguousExecutionError(
                f"Unparseable response from Kraken on {endpoint}: {err}"
            ) from err

        errors = payload.get("error", [])
        if errors:
            raise KrakenOrderRejected(f"Kraken API error: {', '.join(errors)}")

        result = payload.get("result", {})
        return dict(result) if isinstance(result, dict) else {"result": result}

    async def submit_spot_order(self, order: SpotOrder, at: datetime) -> SpotOrder:
        """Submit a long spot limit/market order to Kraken."""
        # 1. Enforce strict spot bounds
        mapped_pair = KRAKEN_PAIR_MAP.get(order.market)
        if not mapped_pair:
            raise KrakenOrderRejected(
                f"Market '{order.market}' has no mapped Kraken spot pair"
            )

        # 2. Build order payload (no leverage parameter permitted)
        data: dict[str, Any] = {
            "pair": mapped_pair,
            "type": order.side.value,
            "ordertype": order.order_type.value,
            "volume": f"{order.quantity:.8f}",
            "cl_ord_id": order.client_order_id,
        }
        if order.order_type is SpotOrderType.LIMIT and order.limit_price is not None:
            data["price"] = f"{order.limit_price:.2f}"
        if self.validate_only:
            data["validate"] = "true"

        # 3. Transition to SUBMITTED
        submitted_order = order.transition_to(OrderState.SUBMITTED, at=at)

        # 4. Transmit request
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
        venue_txid = txids[0] if txids else None
        return submitted_order.transition_to(
            OrderState.ACKNOWLEDGED, at=at, venue_order_id=venue_txid
        )

    async def cancel_spot_order(self, order: SpotOrder, at: datetime) -> SpotOrder:
        """Cancel an open spot order on Kraken."""
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
        if not order.venue_order_id:
            return order.transition_to(OrderState.REJECTED, at=at)

        info = await self.query_order_status(order.venue_order_id)
        if not info:
            return order.transition_to(OrderState.REJECTED, at=at)

        status = info.get("status")
        vol_exec = Decimal(str(info.get("vol_exec", "0")))
        cost = Decimal(str(info.get("cost", "0")))
        fee = Decimal(str(info.get("fee", "0")))
        avg_price = cost / vol_exec if vol_exec > 0 else None

        if status == "closed":
            return order.transition_to(
                OrderState.FILLED,
                at=at,
                filled_quantity=vol_exec,
                average_price=avg_price,
                fee=fee,
            )
        if status in ("open", "pending"):
            return order.transition_to(OrderState.ACKNOWLEDGED, at=at)
        if status == "canceled":
            return order.transition_to(OrderState.CANCELLED, at=at)
        if status == "expired":
            return order.transition_to(OrderState.REJECTED, at=at)

        return order
