"""Binance Spot TESTNET adapter: spot-only, testnet-only, fail-closed.

Verified against the Binance Spot Test Network docs (REST base
https://testnet.binance.vision/api, X-MBX-APIKEY header, HMAC-SHA256 hex
signature over the query string, optional Ed25519 base64 signature,
newClientOrderId, recvWindow <= 60000).

Safety properties enforced in code:
* The profile only accepts the two documented testnet hosts. Mainnet,
  look-alike and credential-bearing URLs raise BinanceMainnetForbidden.
* Redirects are never followed, so a response cannot steer traffic elsewhere.
* Only an explicit allowlist of spot endpoints can be called. No withdrawal,
  transfer, margin or sapi endpoint exists in this module.
* A timeout, 5xx, -1007 or duplicate client id after sending is never retried
  blindly: the order moves to RECONCILIATION_REQUIRED and is queried by client id.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import time
import urllib.parse
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

import httpx2
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from hcs_trader.domain.execution import SpotOrder, SpotOrderType
from hcs_trader.domain.lifecycle import OrderState
from hcs_trader.spot_port import (
    SpotAmbiguousExecutionError,
    SpotExecutionError,
    SpotOrderRejected,
)

VENUE = "binance-testnet"
DEFAULT_BASE_URL = "https://testnet.binance.vision/api"
TESTNET_HOSTS = frozenset({"testnet.binance.vision", "api1.testnet.binance.vision"})

BINANCE_PAIR_MAP = {
    "BTC/USDT": "BTCUSDT",
    "ETH/USDT": "ETHUSDT",
    "LTC/USDT": "LTCUSDT",
    "BNB/USDT": "BNBUSDT",
}

# (method, path) pairs this module may ever call. Nothing else is reachable.
ALLOWED_ENDPOINTS = frozenset(
    {
        ("GET", "/v3/time"),
        ("GET", "/v3/exchangeInfo"),
        ("GET", "/v3/ticker/price"),
        ("GET", "/v3/account"),
        ("GET", "/v3/order"),
        ("POST", "/v3/order"),
        ("DELETE", "/v3/order"),
        ("GET", "/v3/openOrders"),
        ("GET", "/v3/myTrades"),
    }
)
_PUBLIC_PATHS = frozenset({"/v3/time", "/v3/exchangeInfo", "/v3/ticker/price"})

CODE_UNKNOWN_ORDER = -2013
CODE_BACKEND_TIMEOUT = -1007
CODE_EXECUTION_UNKNOWN = -1006
CODE_NEW_ORDER_REJECTED = -2010
MAX_BACKOFF_SECONDS = 30.0


class BinanceTestnetError(SpotExecutionError):
    """Base error for the Binance testnet adapter."""


class BinanceMainnetForbidden(BinanceTestnetError, ValueError):
    """A non-testnet target was supplied. This adapter can never reach mainnet."""


class BinanceOrderRejected(BinanceTestnetError, SpotOrderRejected):
    """The request was rejected locally or by the venue."""


class BinanceApiError(BinanceOrderRejected):
    """The venue returned a structured error ({"code": ..., "msg": ...})."""

    def __init__(self, code: int, message: str, status: int) -> None:
        super().__init__(f"Binance testnet error {code}: {message} (HTTP {status})")
        self.code = code
        self.message = message
        self.status = status

    @property
    def is_duplicate_client_id(self) -> bool:
        return (
            self.code == CODE_NEW_ORDER_REJECTED and "duplicate" in self.message.lower()
        )


class BinanceAmbiguousExecutionError(BinanceTestnetError, SpotAmbiguousExecutionError):
    """Timeout, 5xx or unparseable response: outcome unknown, reconcile first."""


class BinanceRateLimited(BinanceTestnetError):
    """HTTP 429/418 persisted after the retry budget (or retries not allowed)."""

    def __init__(self, message: str, retry_after: float | None, banned: bool) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        self.banned = banned


def _host_of(url: str) -> str:
    return urllib.parse.urlsplit(url).netloc.lower()


@dataclass(frozen=True, slots=True)
class BinanceTestnetProfile:
    """Validated testnet REST base URL. Construction fails for anything else."""

    base_url: str = DEFAULT_BASE_URL

    def __post_init__(self) -> None:
        parts = urllib.parse.urlsplit(self.base_url)
        if (
            parts.scheme != "https"
            or parts.netloc != (parts.hostname or "")
            or parts.hostname not in TESTNET_HOSTS
            or parts.path.rstrip("/") != "/api"
            or parts.query
            or parts.fragment
        ):
            raise BinanceMainnetForbidden(
                "Binance adapter is testnet-only: base URL must be https://"
                "testnet.binance.vision/api or https://api1.testnet.binance.vision/api"
            )

    def url(self, path: str) -> str:
        return f"{self.base_url.rstrip('/')}{path}"


class RequestSigner(Protocol):
    def sign(self, payload: str) -> str: ...


class HmacSha256Signer:
    """HMAC-SHA256 hex signature (Binance HMAC API keys)."""

    def __init__(self, secret: str) -> None:
        if not secret:
            raise ValueError("API secret must not be empty")
        self._secret = secret.encode()

    def sign(self, payload: str) -> str:
        return hmac.new(self._secret, payload.encode(), hashlib.sha256).hexdigest()

    def __repr__(self) -> str:
        return "HmacSha256Signer(<redacted>)"


class Ed25519Signer:
    """Ed25519 base64 signature (Binance Ed25519 API keys)."""

    def __init__(self, private_key: Ed25519PrivateKey) -> None:
        self._key = private_key

    @classmethod
    def from_pem(cls, pem: bytes, password: bytes | None = None) -> "Ed25519Signer":
        key = load_pem_private_key(pem, password=password)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError("PEM does not contain an Ed25519 private key")
        return cls(key)

    def sign(self, payload: str) -> str:
        return base64.b64encode(self._key.sign(payload.encode("ascii"))).decode()

    def __repr__(self) -> str:
        return "Ed25519Signer(<redacted>)"


@dataclass(frozen=True, slots=True)
class BinanceFill:
    trade_id: str
    price: Decimal
    qty: Decimal
    commission: Decimal
    commission_asset: str


@dataclass(frozen=True, slots=True)
class BinanceOrderInfo:
    symbol: str
    venue_order_id: str
    client_order_id: str
    status: str
    side: str
    order_type: str
    price: Decimal
    orig_qty: Decimal
    executed_qty: Decimal
    cumulative_quote_qty: Decimal
    fills: tuple[BinanceFill, ...] = ()


@dataclass(frozen=True, slots=True)
class BinanceBalance:
    asset: str
    free: Decimal
    locked: Decimal


@dataclass(frozen=True, slots=True)
class BinanceAccount:
    can_trade: bool
    can_withdraw: bool
    permissions: tuple[str, ...]
    balances: tuple[BinanceBalance, ...]


@dataclass(frozen=True, slots=True)
class SymbolRules:
    symbol: str
    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal
    min_notional: Decimal


def _dec(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError("Malformed decimal in venue response") from error
    if not result.is_finite():
        raise ValueError("Non-finite decimal in venue response")
    return result


def _fmt(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _parse_fill(raw: dict[str, Any]) -> BinanceFill:
    return BinanceFill(
        trade_id=str(raw.get("tradeId", raw.get("id", ""))),
        price=_dec(raw["price"]),
        qty=_dec(raw["qty"]),
        commission=_dec(raw["commission"]),
        commission_asset=str(raw["commissionAsset"]),
    )


def _parse_order(raw: dict[str, Any]) -> BinanceOrderInfo:
    return BinanceOrderInfo(
        symbol=str(raw["symbol"]),
        venue_order_id=str(raw["orderId"]),
        client_order_id=str(raw.get("clientOrderId", raw.get("origClientOrderId", ""))),
        status=str(raw["status"]),
        side=str(raw["side"]),
        order_type=str(raw["type"]),
        price=_dec(raw["price"]),
        orig_qty=_dec(raw["origQty"]),
        executed_qty=_dec(raw["executedQty"]),
        cumulative_quote_qty=_dec(raw["cummulativeQuoteQty"]),
        fills=tuple(_parse_fill(f) for f in raw.get("fills", [])),
    )


def fee_in_quote(
    fills: tuple[BinanceFill, ...], base_asset: str, quote_asset: str
) -> Decimal | None:
    """Total commission in quote units, or None if any fee asset is third-party."""
    if not fills:
        return None
    total = Decimal(0)
    for fill in fills:
        if fill.commission_asset == quote_asset:
            total += fill.commission
        elif fill.commission_asset == base_asset:
            total += fill.commission * fill.price
        else:
            return None
    return total


def _symbol(market: str) -> str:
    symbol = BINANCE_PAIR_MAP.get(market)
    if symbol is None:
        raise BinanceOrderRejected(
            f"Market '{market}' has no mapped Binance testnet spot symbol"
        )
    return symbol


def _retry_after(response: httpx2.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


class BinanceSpotTestnetAdapter:
    """Spot-only Binance TESTNET adapter implementing SpotExchangePort."""

    def __init__(
        self,
        api_key: str,
        signer: RequestSigner,
        http_client: httpx2.AsyncClient,
        profile: BinanceTestnetProfile | None = None,
        *,
        recv_window_ms: int = 5000,
        max_retries: int = 3,
        request_timeout: float = 10.0,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not api_key:
            raise ValueError("API key must not be empty")
        if not 0 < recv_window_ms <= 60000:
            raise ValueError("recvWindow must be in (0, 60000]")
        self.api_key = api_key
        self.signer = signer
        self.client = http_client
        self.profile = profile or BinanceTestnetProfile()
        self.recv_window_ms = recv_window_ms
        self.max_retries = max_retries
        self.request_timeout = request_timeout
        self._clock = clock
        self._sleep = sleep

    def __repr__(self) -> str:
        return f"BinanceSpotTestnetAdapter(base_url={self.profile.base_url!r})"

    # -- transport ---------------------------------------------------------

    def _build_url(self, path: str, params: dict[str, str], signed: bool) -> str:
        query_params = dict(params)
        if signed:
            query_params["recvWindow"] = str(self.recv_window_ms)
            query_params["timestamp"] = str(int(self._clock() * 1000))
        query = urllib.parse.urlencode(query_params)
        if signed:
            signature = urllib.parse.quote(self.signer.sign(query), safe="")
            query = f"{query}&signature={signature}"
        url = self.profile.url(path)
        return f"{url}?{query}" if query else url

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, str] | None = None,
        *,
        retry_on_rate_limit: bool,
    ) -> Any:
        if (method, path) not in ALLOWED_ENDPOINTS:
            raise BinanceOrderRejected(f"Endpoint {method} {path} is not allowlisted")
        signed = path not in _PUBLIC_PATHS
        headers = {"X-MBX-APIKEY": self.api_key} if signed else {}
        attempt = 0
        while True:
            url = self._build_url(path, params or {}, signed)
            if _host_of(url) not in TESTNET_HOSTS:  # pragma: no cover - defensive
                raise BinanceMainnetForbidden("Refusing non-testnet request target")
            try:
                response = await self.client.request(
                    method,
                    url,
                    headers=headers,
                    timeout=self.request_timeout,
                    follow_redirects=False,
                )
            except (httpx2.TimeoutException, httpx2.TransportError) as err:
                raise BinanceAmbiguousExecutionError(
                    f"Network failure on {method} {path}: {type(err).__name__}"
                ) from err

            status = response.status_code
            if 300 <= status < 400:
                raise BinanceAmbiguousExecutionError(
                    f"Unexpected redirect (HTTP {status}) on {path}; not followed"
                )
            if status in (418, 429):
                retry_after = _retry_after(response)
                banned = status == 418
                if not banned and retry_on_rate_limit and attempt < self.max_retries:
                    delay = retry_after if retry_after is not None else 0.5 * 2**attempt
                    await self._sleep(min(delay, MAX_BACKOFF_SECONDS))
                    attempt += 1
                    continue
                raise BinanceRateLimited(
                    f"Binance testnet rate limited (HTTP {status}) on {path}",
                    retry_after,
                    banned,
                )
            if status >= 500:
                raise BinanceAmbiguousExecutionError(
                    f"Binance testnet 5xx on {path}: HTTP {status}"
                )
            try:
                payload = json.loads(response.text)
            except ValueError as err:
                raise BinanceAmbiguousExecutionError(
                    f"Unparseable response on {path}"
                ) from err
            if isinstance(payload, dict) and isinstance(payload.get("code"), int):
                code = payload["code"]
                if code < 0:
                    raise BinanceApiError(code, str(payload.get("msg", "")), status)
            if status >= 400:
                raise BinanceApiError(0, "HTTP error without code", status)
            return payload

    # -- public / account reads -------------------------------------------

    async def server_time(self) -> int:
        payload = await self._request("GET", "/v3/time", retry_on_rate_limit=True)
        return int(payload["serverTime"])

    async def get_price(self, market: str) -> Decimal:
        payload = await self._request(
            "GET",
            "/v3/ticker/price",
            {"symbol": _symbol(market)},
            retry_on_rate_limit=True,
        )
        return _dec(payload["price"])

    async def symbol_rules(self, market: str) -> SymbolRules:
        symbol = _symbol(market)
        payload = await self._request(
            "GET", "/v3/exchangeInfo", {"symbol": symbol}, retry_on_rate_limit=True
        )
        info = payload["symbols"][0]
        filters = {f["filterType"]: f for f in info["filters"]}
        notional = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL") or {}
        return SymbolRules(
            symbol=symbol,
            tick_size=_dec(filters["PRICE_FILTER"]["tickSize"]),
            step_size=_dec(filters["LOT_SIZE"]["stepSize"]),
            min_qty=_dec(filters["LOT_SIZE"]["minQty"]),
            min_notional=_dec(notional.get("minNotional", "0")),
        )

    async def get_account(self) -> BinanceAccount:
        payload = await self._request("GET", "/v3/account", retry_on_rate_limit=True)
        return BinanceAccount(
            can_trade=bool(payload.get("canTrade")),
            can_withdraw=bool(payload.get("canWithdraw")),
            permissions=tuple(str(p) for p in payload.get("permissions", [])),
            balances=tuple(
                BinanceBalance(
                    asset=str(b["asset"]),
                    free=_dec(b["free"]),
                    locked=_dec(b["locked"]),
                )
                for b in payload.get("balances", [])
            ),
        )

    async def balances(self) -> tuple[BinanceBalance, ...]:
        return (await self.get_account()).balances

    async def open_orders(self, market: str | None = None) -> list[BinanceOrderInfo]:
        params = {"symbol": _symbol(market)} if market else {}
        payload = await self._request(
            "GET", "/v3/openOrders", params, retry_on_rate_limit=True
        )
        return [_parse_order(item) for item in payload]

    async def query_order_by_client_id(
        self, market: str, client_order_id: str
    ) -> BinanceOrderInfo | None:
        """Return the order, or None when the venue reports it does not exist."""
        try:
            payload = await self._request(
                "GET",
                "/v3/order",
                {"symbol": _symbol(market), "origClientOrderId": client_order_id},
                retry_on_rate_limit=True,
            )
        except BinanceApiError as error:
            if error.code == CODE_UNKNOWN_ORDER:
                return None
            raise
        return _parse_order(payload)

    async def fills(
        self, market: str, venue_order_id: str | None = None
    ) -> list[BinanceFill]:
        params = {"symbol": _symbol(market), "limit": "1000"}
        if venue_order_id:
            params["orderId"] = venue_order_id
        payload = await self._request(
            "GET", "/v3/myTrades", params, retry_on_rate_limit=True
        )
        return [_parse_fill(item) for item in payload]

    # -- order lifecycle (SpotExchangePort) --------------------------------

    def _check_venue(self, order: SpotOrder) -> None:
        if order.venue != VENUE:
            raise BinanceOrderRejected(
                f"Order venue '{order.venue}' is not '{VENUE}'; refusing to route"
            )

    def _build_order_params(self, order: SpotOrder) -> dict[str, str]:
        self._check_venue(order)
        params = {
            "symbol": _symbol(order.market),
            "side": order.side.value.upper(),
            "type": order.order_type.value.upper(),
            "quantity": _fmt(order.quantity),
            "newClientOrderId": order.client_order_id,
            "newOrderRespType": "FULL",
        }
        if order.order_type is SpotOrderType.LIMIT:
            if order.limit_price is None:
                raise BinanceOrderRejected("LIMIT order requires a limit price")
            params["timeInForce"] = "GTC"
            params["price"] = _fmt(order.limit_price)
        return params

    async def submit_spot_order(self, order: SpotOrder, at: datetime) -> SpotOrder:
        params = self._build_order_params(order)
        submitted = order.transition_to(OrderState.SUBMITTED, at=at)
        try:
            payload = await self._request(
                "POST", "/v3/order", params, retry_on_rate_limit=False
            )
        except (BinanceAmbiguousExecutionError, BinanceRateLimited):
            return submitted.transition_to(OrderState.RECONCILIATION_REQUIRED, at=at)
        except BinanceApiError as error:
            if error.is_duplicate_client_id or error.code in (
                CODE_BACKEND_TIMEOUT,
                CODE_EXECUTION_UNKNOWN,
            ):
                return submitted.transition_to(
                    OrderState.RECONCILIATION_REQUIRED, at=at
                )
            return submitted.transition_to(OrderState.REJECTED, at=at)
        try:
            info = _parse_order(payload)
        except (KeyError, TypeError, ValueError, AttributeError):
            # The POST was sent; a malformed 200 means the outcome is unknown.
            return submitted.transition_to(OrderState.RECONCILIATION_REQUIRED, at=at)
        return self._advance(submitted, info, at, fills=info.fills)

    async def submit_and_reconcile(self, order: SpotOrder, at: datetime) -> SpotOrder:
        """Submit once; on ambiguity query by client id (never resubmit blindly)."""
        result = await self.submit_spot_order(order, at)
        if result.state is OrderState.RECONCILIATION_REQUIRED:
            return await self.reconcile_order(result, at, never_acknowledged=True)
        return result

    async def cancel_spot_order(self, order: SpotOrder, at: datetime) -> SpotOrder:
        if order.state not in (
            OrderState.ACKNOWLEDGED,
            OrderState.PARTIALLY_FILLED,
            OrderState.RECONCILIATION_REQUIRED,
        ):
            raise ValueError(f"Cannot cancel an order in state {order.state}")
        self._check_venue(order)
        payload = await self._request(
            "DELETE",
            "/v3/order",
            {
                "symbol": _symbol(order.market),
                "origClientOrderId": order.client_order_id,
            },
            retry_on_rate_limit=False,
        )
        info = _parse_order(payload)
        if info.status != "CANCELED":
            return await self.reconcile_order(order, at)
        return self._advance(order, info, at, fills=())

    async def reconcile_order(
        self, order: SpotOrder, at: datetime, *, never_acknowledged: bool = False
    ) -> SpotOrder:
        """Query venue truth by client id and advance the local order.

        ``never_acknowledged`` must be True only when the caller knows the order
        has never been seen acknowledged (for example straight after an
        ambiguous submit). Without it, a flagged order that the venue no longer
        knows stays RECONCILIATION_REQUIRED for operator review.
        """
        self._check_venue(order)
        info = await self.query_order_by_client_id(order.market, order.client_order_id)
        if info is None:
            # Unknown to the venue. Only an order we never saw acknowledged can
            # be treated as not placed; anything else stays flagged.
            if order.state is OrderState.SUBMITTED or (
                order.state is OrderState.RECONCILIATION_REQUIRED and never_acknowledged
            ):
                return order.transition_to(OrderState.REJECTED, at=at)
            return self._to_reconciliation_required(order, at)
        fills: tuple[BinanceFill, ...] = ()
        if info.executed_qty > 0:
            fills = tuple(await self.fills(order.market, info.venue_order_id))
        return self._advance(order, info, at, fills=fills)

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _to_reconciliation_required(order: SpotOrder, at: datetime) -> SpotOrder:
        if order.state is OrderState.RECONCILIATION_REQUIRED:
            return order
        return order.transition_to(OrderState.RECONCILIATION_REQUIRED, at=at)

    def _advance(
        self,
        order: SpotOrder,
        info: BinanceOrderInfo,
        at: datetime,
        *,
        fills: tuple[BinanceFill, ...],
    ) -> SpotOrder:
        targets = {
            "NEW": OrderState.ACKNOWLEDGED,
            "PENDING_NEW": OrderState.ACKNOWLEDGED,
            "PENDING_CANCEL": OrderState.ACKNOWLEDGED,
            "PARTIALLY_FILLED": OrderState.PARTIALLY_FILLED,
            "FILLED": OrderState.FILLED,
            "CANCELED": OrderState.CANCELLED,
            "EXPIRED": OrderState.CANCELLED,
            "EXPIRED_IN_MATCH": OrderState.CANCELLED,
            "REJECTED": OrderState.REJECTED,
        }
        target = targets.get(info.status)
        if target is None:
            return self._to_reconciliation_required(order, at)
        if target is order.state and target is not OrderState.PARTIALLY_FILLED:
            return order
        base, _, quote = order.market.partition("/")
        executed = info.executed_qty if info.executed_qty > 0 else None
        average = (
            info.cumulative_quote_qty / info.executed_qty
            if info.executed_qty > 0
            else None
        )
        try:
            return order.transition_to(
                target,
                at=at,
                venue_order_id=info.venue_order_id,
                filled_quantity=executed,
                average_price=average,
                fee=fee_in_quote(fills, base, quote),
            )
        except ValueError:
            return self._to_reconciliation_required(order, at)
