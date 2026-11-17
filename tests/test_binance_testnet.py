"""Contract tests for the Binance Spot TESTNET adapter (no network, no keys)."""

import base64
import hashlib
import hmac
import json
import urllib.parse
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx2
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)

from hcs_trader.binance_testnet import (
    ALLOWED_ENDPOINTS,
    BinanceAmbiguousExecutionError,
    BinanceApiError,
    BinanceMainnetForbidden,
    BinanceOrderRejected,
    BinanceRateLimited,
    BinanceSpotTestnetAdapter,
    BinanceTestnetProfile,
    Ed25519Signer,
    HmacSha256Signer,
)
from hcs_trader.domain.execution import SpotOrder, SpotOrderType, SpotSide
from hcs_trader.domain.lifecycle import OrderState
from hcs_trader.kraken_spot import KrakenSpotAdapter
from hcs_trader.spot_port import (
    SpotAmbiguousExecutionError,
    SpotExchangePort,
    SpotOrderRejected,
)

FIXTURES = Path(__file__).parent / "fixtures" / "binance_testnet"
API_KEY = "synthetic-testnet-key"
API_SECRET = "synthetic-testnet-secret"
NOW = datetime(2026, 1, 1, tzinfo=UTC)
Handler = Callable[[httpx2.Request], httpx2.Response]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def fx(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def reply(name: str, status: int = 200, **kw: Any) -> httpx2.Response:
    return httpx2.Response(status, json=fx(name), **kw)


def order(
    state: OrderState = OrderState.PREVIEWED,
    order_type: SpotOrderType = SpotOrderType.LIMIT,
    venue: str = "binance-testnet",
    market: str = "BTC/USDT",
) -> SpotOrder:
    return SpotOrder(
        client_order_id="hcs-cl-001",
        order_id="ord-1",
        venue=venue,
        market=market,
        side=SpotSide.BUY,
        order_type=order_type,
        quantity=Decimal("0.01"),
        limit_price=Decimal("50000.00") if order_type is SpotOrderType.LIMIT else None,
        state=state,
        risk_decision_id="risk-1",
        created_at=NOW,
        updated_at=NOW,
    )


def adapter_for(
    handler: Handler,
    sleeps: list[float] | None = None,
    **kwargs: Any,
) -> tuple[BinanceSpotTestnetAdapter, httpx2.AsyncClient]:
    async def fake_sleep(delay: float) -> None:
        if sleeps is not None:
            sleeps.append(delay)

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    adapter = BinanceSpotTestnetAdapter(
        API_KEY,
        HmacSha256Signer(API_SECRET),
        client,
        clock=lambda: 1499827319.559,
        sleep=fake_sleep,
        **kwargs,
    )
    return adapter, client


def signed_query(request: httpx2.Request) -> tuple[str, str]:
    raw = request.url.query.decode()
    payload, _, signature = raw.rpartition("&signature=")
    return payload, urllib.parse.unquote(signature)


# -- guards ------------------------------------------------------------------


def test_default_profile_is_testnet() -> None:
    assert BinanceTestnetProfile().base_url == "https://testnet.binance.vision/api"
    BinanceTestnetProfile("https://api1.testnet.binance.vision/api")


@pytest.mark.parametrize(
    "url",
    [
        "https://api.binance.com/api",
        "https://api1.binance.com/api",
        "https://api.binance.us/api",
        "http://testnet.binance.vision/api",
        "https://testnet.binance.vision.evil.com/api",
        "https://evil.com@testnet.binance.vision/api",
        "https://testnet.binance.vision:8443/api",
        "https://testnet.binance.vision/sapi",
        "https://testnet.binance.vision/api?x=1",
        "https://testnet.binance.vision",
        "https://fapi.binance.com/fapi",
        "",
    ],
)
def test_profile_rejects_every_non_testnet_target(url: str) -> None:
    with pytest.raises(BinanceMainnetForbidden):
        BinanceTestnetProfile(url)


def test_adapter_refuses_mainnet_profile_and_bad_inputs() -> None:
    client = httpx2.AsyncClient()
    with pytest.raises(BinanceMainnetForbidden):
        BinanceSpotTestnetAdapter(
            API_KEY,
            HmacSha256Signer(API_SECRET),
            client,
            BinanceTestnetProfile("https://api.binance.com/api"),
        )
    with pytest.raises(ValueError, match="API key"):
        BinanceSpotTestnetAdapter("", HmacSha256Signer(API_SECRET), client)
    with pytest.raises(ValueError, match="recvWindow"):
        BinanceSpotTestnetAdapter(
            API_KEY, HmacSha256Signer(API_SECRET), client, recv_window_ms=60001
        )
    with pytest.raises(ValueError, match="secret"):
        HmacSha256Signer("")


def test_endpoint_allowlist_has_no_money_movement() -> None:
    paths = {path for _, path in ALLOWED_ENDPOINTS}
    forbidden = ("withdraw", "transfer", "sapi", "margin", "capital", "futures")
    assert not [p for p in paths if any(word in p.lower() for word in forbidden)]


def test_adapters_satisfy_the_venue_agnostic_port() -> None:
    adapter, _ = adapter_for(lambda r: reply("server_time"))
    assert isinstance(adapter, SpotExchangePort)
    kraken = KrakenSpotAdapter(
        "k", base64.b64encode(b"s").decode(), httpx2.AsyncClient()
    )
    assert isinstance(kraken, SpotExchangePort)
    assert issubclass(BinanceOrderRejected, SpotOrderRejected)
    assert issubclass(BinanceAmbiguousExecutionError, SpotAmbiguousExecutionError)


@pytest.mark.anyio
async def test_non_allowlisted_endpoint_never_reaches_the_network() -> None:
    calls: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(str(request.url))
        return reply("server_time")

    adapter, client = adapter_for(handler)
    async with client:
        with pytest.raises(BinanceOrderRejected, match="allowlisted"):
            await adapter._request(
                "POST", "/v3/withdraw", {}, retry_on_rate_limit=False
            )
        with pytest.raises(BinanceOrderRejected, match="allowlisted"):
            await adapter._request(
                "GET",
                "/sapi/v1/capital/withdraw/apply",
                None,
                retry_on_rate_limit=False,
            )
    assert calls == []


@pytest.mark.anyio
async def test_all_requests_target_testnet_and_redirects_are_not_followed() -> None:
    hosts: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        hosts.append(request.url.host)
        if request.url.path.endswith("/v3/account"):
            return httpx2.Response(
                307, headers={"Location": "https://api.binance.com/api/v3/account"}
            )
        return reply("server_time")

    adapter, client = adapter_for(handler)
    async with client:
        await adapter.server_time()
        with pytest.raises(BinanceAmbiguousExecutionError, match="redirect"):
            await adapter.get_account()
    assert set(hosts) == {"testnet.binance.vision"}


def test_credentials_are_redacted_in_repr() -> None:
    adapter, _ = adapter_for(lambda r: reply("server_time"))
    ed = Ed25519Signer(Ed25519PrivateKey.generate())
    text = f"{adapter!r} {adapter.signer!r} {ed!r}"
    assert API_SECRET not in text
    assert "redacted" in text


# -- signing -------------------------------------------------------------------


def test_hmac_matches_binance_documented_vector() -> None:
    secret = "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j"  # noqa: S105
    payload = (
        "symbol=LTCBTC&side=BUY&type=LIMIT&timeInForce=GTC&quantity=1"
        "&price=0.1&recvWindow=5000&timestamp=1499827319559"
    )
    assert HmacSha256Signer(secret).sign(payload) == (
        "c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71"
    )


def test_ed25519_signature_verifies_and_pem_loads() -> None:
    key = Ed25519PrivateKey.generate()
    signer = Ed25519Signer(key)
    payload = "symbol=BTCUSDT&timestamp=1"
    key.public_key().verify(base64.b64decode(signer.sign(payload)), payload.encode())
    pem = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    assert Ed25519Signer.from_pem(pem).sign(payload) == signer.sign(payload)
    dashes = b"-----"
    rsa_like = (
        dashes
        + b"BEGIN PRIVATE KEY"
        + dashes
        + b"\nbm90LWEta2V5\n"
        + dashes
        + b"END PRIVATE KEY"
        + dashes
        + b"\n"
    )
    with pytest.raises(ValueError):
        Ed25519Signer.from_pem(rsa_like)


@pytest.mark.anyio
async def test_signed_request_shape_and_signature() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return reply("account")

    adapter, client = adapter_for(handler)
    async with client:
        await adapter.get_account()
    request = seen[0]
    payload, signature = signed_query(request)
    assert request.headers["X-MBX-APIKEY"] == API_KEY
    assert "recvWindow=5000" in payload and "timestamp=1499827319559" in payload
    expected = hmac.new(API_SECRET.encode(), payload.encode(), hashlib.sha256)
    assert signature == expected.hexdigest()
    assert API_SECRET not in str(request.url)


@pytest.mark.anyio
async def test_public_endpoints_send_no_key_or_signature() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return reply("server_time")

    adapter, client = adapter_for(handler)
    async with client:
        assert await adapter.server_time() == 1499827319559
    assert "X-MBX-APIKEY" not in seen[0].headers
    assert b"signature" not in seen[0].url.query


# -- submit ------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    "body",
    [{}, {"symbol": "BTCUSDT", "orderId": 1}, {"orderId": "x", "status": "NEW"}],
)
async def test_malformed_200_on_submit_requires_reconciliation(
    body: dict[str, Any],
) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=body)

    adapter, client = adapter_for(handler)
    async with client:
        result = await adapter.submit_spot_order(order(), NOW)
    assert result.state is OrderState.RECONCILIATION_REQUIRED


@pytest.mark.anyio
async def test_bad_decimal_in_200_on_submit_requires_reconciliation() -> None:
    bad = {**fx("order_new_full"), "price": "not-a-number"}

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=bad)

    adapter, client = adapter_for(handler)
    async with client:
        result = await adapter.submit_spot_order(order(), NOW)
    assert result.state is OrderState.RECONCILIATION_REQUIRED


@pytest.mark.anyio
async def test_execution_status_unknown_4xx_requires_reconciliation() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            400, json={"code": -1006, "msg": "Unexpected response; status unknown"}
        )

    adapter, client = adapter_for(handler)
    async with client:
        result = await adapter.submit_spot_order(order(), NOW)
    assert result.state is OrderState.RECONCILIATION_REQUIRED


@pytest.mark.anyio
async def test_submit_limit_order_acknowledged_with_idempotent_client_id() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return reply("order_new_full")

    adapter, client = adapter_for(handler)
    async with client:
        result = await adapter.submit_spot_order(order(), NOW)
    assert result.state is OrderState.ACKNOWLEDGED
    assert result.venue_order_id == "28"
    request = seen[0]
    assert request.method == "POST" and request.url.path == "/api/v3/order"
    params = dict(request.url.params)
    assert params["newClientOrderId"] == "hcs-cl-001"
    assert params["symbol"] == "BTCUSDT" and params["side"] == "BUY"
    assert params["type"] == "LIMIT" and params["timeInForce"] == "GTC"
    assert params["quantity"] == "0.01" and params["price"] == "50000"
    assert not {"leverage", "isIsolated", "sideEffectType"} & set(params)


@pytest.mark.anyio
async def test_submit_market_order_filled_with_fee_in_quote() -> None:
    adapter, client = adapter_for(lambda r: reply("order_filled_full"))
    async with client:
        result = await adapter.submit_spot_order(
            order(order_type=SpotOrderType.MARKET), NOW
        )
    assert result.state is OrderState.FILLED
    assert result.filled_quantity == Decimal("0.01")
    assert result.average_price == Decimal("50000")
    assert result.fee == Decimal("0.00001") * Decimal("50000")


@pytest.mark.anyio
async def test_submit_rejected_by_venue() -> None:
    adapter, client = adapter_for(lambda r: reply("error_insufficient_balance", 400))
    async with client:
        result = await adapter.submit_spot_order(order(), NOW)
    assert result.state is OrderState.REJECTED


@pytest.mark.anyio
async def test_submit_timeout_is_ambiguous_and_never_retried() -> None:
    calls = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        raise httpx2.ReadTimeout("slow", request=request)

    adapter, client = adapter_for(handler)
    async with client:
        result = await adapter.submit_spot_order(order(), NOW)
        with pytest.raises(ValueError, match="Invalid order transition"):
            # A RECONCILIATION_REQUIRED order cannot be blindly re-submitted.
            await adapter.submit_spot_order(result, NOW)
    assert result.state is OrderState.RECONCILIATION_REQUIRED
    assert calls == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("response_factory"),
    [
        lambda: httpx2.Response(503, text="unavailable"),
        lambda: reply("error_backend_timeout", 200),
        lambda: httpx2.Response(200, text="<html>not json</html>"),
        lambda: reply("error_duplicate_order", 400),
        lambda: reply("error_too_many_requests", 429),
        lambda: httpx2.Response(418, json={"code": -1003, "msg": "banned"}),
    ],
    ids=["5xx", "-1007", "unparseable", "duplicate-client-id", "429", "418"],
)
async def test_submit_ambiguous_outcomes_require_reconciliation(
    response_factory: Callable[[], httpx2.Response],
) -> None:
    calls = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        return response_factory()

    adapter, client = adapter_for(handler)
    async with client:
        result = await adapter.submit_spot_order(order(), NOW)
    assert result.state is OrderState.RECONCILIATION_REQUIRED
    assert calls == 1


@pytest.mark.anyio
async def test_submit_and_reconcile_queries_by_client_id_before_any_retry() -> None:
    paths: list[tuple[str, str]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        paths.append((request.method, request.url.path))
        if request.method == "POST":
            raise httpx2.ReadTimeout("slow", request=request)
        assert dict(request.url.params)["origClientOrderId"] == "hcs-cl-001"
        return reply("order_query_new")

    adapter, client = adapter_for(handler)
    async with client:
        result = await adapter.submit_and_reconcile(order(), NOW)
    # The order actually reached the venue: reconciliation discovers it.
    assert result.state is OrderState.ACKNOWLEDGED
    assert paths == [("POST", "/api/v3/order"), ("GET", "/api/v3/order")]


@pytest.mark.anyio
async def test_timeout_then_unknown_on_venue_is_rejected_not_blindly_resent() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.method == "POST":
            raise httpx2.ConnectTimeout("t", request=request)
        return reply("error_unknown_order", 400)

    adapter, client = adapter_for(handler)
    async with client:
        result = await adapter.submit_and_reconcile(order(), NOW)
    assert result.state is OrderState.REJECTED


@pytest.mark.anyio
async def test_local_validation_rejects_before_any_request() -> None:
    calls = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        return reply("order_new_full")

    adapter, client = adapter_for(handler)
    async with client:
        with pytest.raises(BinanceOrderRejected, match="venue"):
            await adapter.submit_spot_order(order(venue="kraken"), NOW)
        with pytest.raises(BinanceOrderRejected, match="mapped"):
            await adapter.submit_spot_order(order(market="DGB/USDT"), NOW)
        no_price = replace(order(), limit_price=None)
        with pytest.raises(BinanceOrderRejected, match="limit price"):
            await adapter.submit_spot_order(no_price, NOW)
    assert calls == 0


# -- reconcile / cancel / reads ---------------------------------------------


@pytest.mark.anyio
async def test_reconcile_filled_collects_fills_and_fee() -> None:
    paths: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        paths.append(request.url.path)
        if request.url.path == "/api/v3/myTrades":
            assert dict(request.url.params)["orderId"] == "28"
            return reply("my_trades")
        return reply("order_query_filled")

    adapter, client = adapter_for(handler)
    amb = order(OrderState.RECONCILIATION_REQUIRED)
    async with client:
        result = await adapter.reconcile_order(amb, NOW)
    assert result.state is OrderState.FILLED
    assert result.average_price == Decimal("50000")
    assert result.fee == Decimal("0.5")
    assert paths == ["/api/v3/order", "/api/v3/myTrades"]


@pytest.mark.anyio
async def test_reconcile_partial_and_unchanged_and_anomalies() -> None:
    state = {"name": "order_query_partial"}

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/api/v3/myTrades":
            return reply("my_trades")
        if state["name"] == "unknown":
            return reply("error_unknown_order", 400)
        return reply(state["name"])

    adapter, client = adapter_for(handler)
    async with client:
        partial = await adapter.reconcile_order(order(OrderState.ACKNOWLEDGED), NOW)
        assert partial.state is OrderState.PARTIALLY_FILLED
        assert partial.filled_quantity == Decimal("0.004")
        state["name"] = "order_query_new"
        same = order(OrderState.ACKNOWLEDGED)
        assert (await adapter.reconcile_order(same, NOW)) is same
        # Acknowledged order that vanished from the venue stays flagged.
        state["name"] = "unknown"
        flagged = await adapter.reconcile_order(same, NOW)
        assert flagged.state is OrderState.RECONCILIATION_REQUIRED
        # Never auto-REJECT a flagged order without acknowledgement history.
        again = await adapter.reconcile_order(flagged, NOW)
        assert again.state is OrderState.RECONCILIATION_REQUIRED
        # Only an explicit never-acknowledged caller may conclude "not placed".
        rejected = await adapter.reconcile_order(flagged, NOW, never_acknowledged=True)
        assert rejected.state is OrderState.REJECTED


@pytest.mark.anyio
async def test_reconcile_reraises_non_unknown_order_errors() -> None:
    adapter, client = adapter_for(lambda r: reply("error_timestamp", 400))
    async with client:
        with pytest.raises(BinanceApiError) as info:
            await adapter.reconcile_order(
                order(OrderState.RECONCILIATION_REQUIRED), NOW
            )
    assert info.value.code == -1021


@pytest.mark.anyio
async def test_cancel_open_order() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return reply("order_cancel")

    adapter, client = adapter_for(handler)
    async with client:
        result = await adapter.cancel_spot_order(order(OrderState.ACKNOWLEDGED), NOW)
        with pytest.raises(ValueError, match="Cannot cancel"):
            await adapter.cancel_spot_order(order(), NOW)
    assert result.state is OrderState.CANCELLED
    assert seen[0].method == "DELETE"
    assert dict(seen[0].url.params)["origClientOrderId"] == "hcs-cl-001"
    assert len(seen) == 1


@pytest.mark.anyio
async def test_cancel_with_unexpected_status_falls_back_to_reconcile() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.method == "DELETE":
            return reply("order_query_new")
        return reply("order_query_new")

    adapter, client = adapter_for(handler)
    start = order(OrderState.ACKNOWLEDGED)
    async with client:
        assert (await adapter.cancel_spot_order(start, NOW)) is start


@pytest.mark.anyio
async def test_cancel_timeout_is_ambiguous_error() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("slow", request=request)

    adapter, client = adapter_for(handler)
    async with client:
        with pytest.raises(BinanceAmbiguousExecutionError):
            await adapter.cancel_spot_order(order(OrderState.ACKNOWLEDGED), NOW)


@pytest.mark.anyio
async def test_open_orders_balances_fills_price_and_rules() -> None:
    routes = {
        "/api/v3/openOrders": "open_orders",
        "/api/v3/account": "account",
        "/api/v3/myTrades": "my_trades",
        "/api/v3/ticker/price": "ticker_price",
        "/api/v3/exchangeInfo": "exchange_info_btcusdt",
    }

    def handler(request: httpx2.Request) -> httpx2.Response:
        return reply(routes[request.url.path])

    adapter, client = adapter_for(handler)
    async with client:
        orders = await adapter.open_orders("BTC/USDT")
        assert orders[0].client_order_id == "hcs-cl-001"
        assert orders[0].price == Decimal("50000")
        assert len(await adapter.open_orders()) == 1
        balances = {b.asset: b for b in await adapter.balances()}
        assert balances["USDT"].locked == Decimal("250.5")
        account = await adapter.get_account()
        assert account.can_trade and not account.can_withdraw
        assert account.permissions == ("SPOT",)
        fills = await adapter.fills("BTC/USDT", "28")
        assert fills[0].commission_asset == "BTC" and fills[0].trade_id == "28457"
        assert await adapter.get_price("BTC/USDT") == Decimal("60000")
        rules = await adapter.symbol_rules("BTC/USDT")
    assert (rules.tick_size, rules.step_size, rules.min_notional) == (
        Decimal("0.01"),
        Decimal("0.00001"),
        Decimal("5"),
    )


# -- rate limiting -------------------------------------------------------------


@pytest.mark.anyio
async def test_429_backs_off_then_succeeds_with_fresh_signature() -> None:
    calls: list[httpx2.Request] = []
    sleeps: list[float] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        if len(calls) == 1:
            return reply("error_too_many_requests", 429, headers={"Retry-After": "2"})
        if len(calls) == 2:
            return reply("error_too_many_requests", 429)
        return reply("account")

    adapter, client = adapter_for(handler, sleeps)
    async with client:
        await adapter.get_account()
    assert sleeps == [2.0, 1.0]
    assert len(calls) == 3
    for request in calls:
        payload, signature = signed_query(request)
        mac = hmac.new(API_SECRET.encode(), payload.encode(), hashlib.sha256)
        assert signature == mac.hexdigest()


@pytest.mark.anyio
async def test_429_exhausts_retry_budget_and_418_is_never_retried() -> None:
    calls = 0
    sleeps: list[float] = []
    status = {"code": 429}

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        return httpx2.Response(
            status["code"],
            json=fx("error_too_many_requests"),
            headers={"Retry-After": "999"},
        )

    adapter, client = adapter_for(handler, sleeps, max_retries=2)
    async with client:
        with pytest.raises(BinanceRateLimited) as limited:
            await adapter.get_account()
        assert calls == 3 and not limited.value.banned
        assert sleeps == [30.0, 30.0]  # capped
        calls = 0
        status["code"] = 418
        with pytest.raises(BinanceRateLimited) as banned:
            await adapter.get_account()
    assert calls == 1 and banned.value.banned and banned.value.retry_after == 999.0


@pytest.mark.anyio
async def test_malformed_retry_after_falls_back_to_exponential_backoff() -> None:
    sleeps: list[float] = []
    calls = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return reply(
                "error_too_many_requests", 429, headers={"Retry-After": "soon"}
            )
        return reply("account")

    adapter, client = adapter_for(handler, sleeps)
    async with client:
        await adapter.get_account()
    assert sleeps == [0.5, 1.0]


@pytest.mark.anyio
async def test_http_error_without_code_and_unknown_status_handling() -> None:
    adapter, client = adapter_for(lambda r: httpx2.Response(400, json={"x": 1}))
    async with client:
        with pytest.raises(BinanceApiError):
            await adapter.get_account()
    unknown = {**fx("order_new_full"), "status": "WEIRD"}
    adapter2, client2 = adapter_for(lambda r: httpx2.Response(200, json=unknown))
    async with client2:
        result = await adapter2.submit_spot_order(order(), NOW)
    assert result.state is OrderState.RECONCILIATION_REQUIRED
