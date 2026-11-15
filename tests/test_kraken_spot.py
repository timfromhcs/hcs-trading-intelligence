"""Tests for KrakenSpotAdapter, order lifecycle transitions and SQLite persistence."""

import base64
import hashlib
import hmac
import urllib.parse
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx2
import pytest

from hcs_trader.domain.execution import SpotOrder, SpotOrderType, SpotSide
from hcs_trader.domain.lifecycle import OrderState
from hcs_trader.kraken_spot import KrakenSpotAdapter
from hcs_trader.storage import AuditStore

# Synthetic test credentials
_API_KEY = "test-api-key-12345"
_API_SECRET = base64.b64encode(b"01234567890123456789012345678901").decode("ascii")


def _sample_order(
    client_id: str = "cl-ord-001",
    quantity: str = "0.01",
    side: SpotSide = SpotSide.BUY,
    limit_price: str | None = "50000.00",
    state: OrderState = OrderState.PREVIEWED,
    venue_order_id: str | None = None,
) -> SpotOrder:
    now = datetime.now(UTC)
    return SpotOrder(
        client_order_id=client_id,
        order_id="ord-001",
        venue="kraken",
        market="BTC/USD",
        side=side,
        order_type=SpotOrderType.LIMIT if limit_price else SpotOrderType.MARKET,
        quantity=Decimal(quantity),
        limit_price=Decimal(limit_price) if limit_price else None,
        state=state,
        risk_decision_id="risk-001",
        created_at=now,
        updated_at=now,
        venue_order_id=venue_order_id,
    )


def test_kraken_signature_computation() -> None:
    client = httpx2.AsyncClient()
    adapter = KrakenSpotAdapter(_API_KEY, _API_SECRET, client)
    path = "/0/private/AddOrder"
    nonce = 1600000000000000
    data = {"pair": "XXBTZUSD", "type": "buy", "ordertype": "limit"}

    headers = adapter._sign_request(path, data, nonce)
    assert headers["API-Key"] == _API_KEY

    # Independently verify HMAC-SHA512
    postdata = urllib.parse.urlencode(data)
    encoded = (str(nonce) + postdata).encode()
    message = path.encode() + hashlib.sha256(encoded).digest()
    secret_bytes = base64.b64decode(_API_SECRET)
    mac = hmac.new(secret_bytes, message, hashlib.sha512)
    expected_sig = base64.b64encode(mac.digest()).decode()
    assert headers["API-Sign"] == expected_sig


@pytest.mark.anyio
async def test_submit_spot_order_success() -> None:
    now = datetime.now(UTC)
    order = _sample_order()

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/0/private/AddOrder"
        return httpx2.Response(
            200,
            json={
                "error": [],
                "result": {
                    "descr": {"order": "buy 0.01000000 XXBTZUSD @ limit 50000.00"},
                    "txid": ["OXXXXX-XXXXX-XXXXX"],
                },
            },
        )

    transport = httpx2.MockTransport(handler)
    async with httpx2.AsyncClient(transport=transport) as client:
        adapter = KrakenSpotAdapter(_API_KEY, _API_SECRET, client)
        submitted = await adapter.submit_spot_order(order, at=now)

    assert submitted.state is OrderState.ACKNOWLEDGED
    assert submitted.venue_order_id == "OXXXXX-XXXXX-XXXXX"


@pytest.mark.anyio
async def test_submit_spot_order_rejected_by_exchange() -> None:
    now = datetime.now(UTC)
    order = _sample_order()

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={"error": ["EOrder:Insufficient funds"], "result": {}},
        )

    transport = httpx2.MockTransport(handler)
    async with httpx2.AsyncClient(transport=transport) as client:
        adapter = KrakenSpotAdapter(_API_KEY, _API_SECRET, client)
        rejected = await adapter.submit_spot_order(order, at=now)

    assert rejected.state is OrderState.REJECTED


@pytest.mark.anyio
async def test_submit_spot_order_timeout_requires_reconciliation() -> None:
    now = datetime.now(UTC)
    order = _sample_order()

    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.TimeoutException("Connection timed out")

    transport = httpx2.MockTransport(handler)
    async with httpx2.AsyncClient(transport=transport) as client:
        adapter = KrakenSpotAdapter(_API_KEY, _API_SECRET, client)
        ambiguous = await adapter.submit_spot_order(order, at=now)

    # Invariant 8: must advance to RECONCILIATION_REQUIRED
    assert ambiguous.state is OrderState.RECONCILIATION_REQUIRED


@pytest.mark.anyio
async def test_cancel_spot_order_success() -> None:
    now = datetime.now(UTC)
    order = _sample_order(
        state=OrderState.ACKNOWLEDGED, venue_order_id="OXXXXX-XXXXX-XXXXX"
    )

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/0/private/CancelOrder"
        return httpx2.Response(
            200,
            json={"error": [], "result": {"count": 1}},
        )

    transport = httpx2.MockTransport(handler)
    async with httpx2.AsyncClient(transport=transport) as client:
        adapter = KrakenSpotAdapter(_API_KEY, _API_SECRET, client)
        cancelled = await adapter.cancel_spot_order(order, at=now)

    assert cancelled.state is OrderState.CANCELLED


@pytest.mark.anyio
async def test_reconcile_order_filled() -> None:
    now = datetime.now(UTC)
    order = _sample_order(
        state=OrderState.ACKNOWLEDGED, venue_order_id="OXXXXX-XXXXX-XXXXX"
    )

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/0/private/QueryOrders"
        return httpx2.Response(
            200,
            json={
                "error": [],
                "result": {
                    "OXXXXX-XXXXX-XXXXX": {
                        "status": "closed",
                        "vol_exec": "0.01000000",
                        "cost": "500.00",
                        "fee": "1.25",
                    }
                },
            },
        )

    transport = httpx2.MockTransport(handler)
    async with httpx2.AsyncClient(transport=transport) as client:
        adapter = KrakenSpotAdapter(_API_KEY, _API_SECRET, client)
        filled = await adapter.reconcile_order(order, at=now)

    assert filled.state is OrderState.FILLED
    assert filled.filled_quantity == Decimal("0.01000000")
    assert filled.average_price == Decimal("50000.00")
    assert filled.fee == Decimal("1.25")


def test_spot_order_storage_persistence(tmp_path: Path) -> None:
    store = AuditStore(tmp_path / "test_spot.db")
    store.initialize()

    now = datetime.now(UTC)
    order = _sample_order(client_id="cl-ord-persist")
    store.save_spot_order(order)

    retrieved = store.get_spot_order("cl-ord-persist")
    assert retrieved is not None
    assert retrieved.client_order_id == "cl-ord-persist"
    assert retrieved.quantity == Decimal("0.01")
    assert retrieved.state is OrderState.PREVIEWED

    # Advance state to SUBMITTED -> ACKNOWLEDGED
    advanced = order.transition_to(OrderState.SUBMITTED, at=now).transition_to(
        OrderState.ACKNOWLEDGED, at=now, venue_order_id="OTX-999"
    )
    store.save_spot_order(advanced)

    updated = store.get_spot_order("cl-ord-persist")
    assert updated is not None
    assert updated.state is OrderState.ACKNOWLEDGED
    assert updated.venue_order_id == "OTX-999"

    # List open orders
    open_orders = store.list_open_spot_orders("kraken")
    assert len(open_orders) == 1
    assert open_orders[0].client_order_id == "cl-ord-persist"
