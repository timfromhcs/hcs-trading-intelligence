"""Market registry, multi-market Kraken adapter and live-submission safety."""

import base64
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient
from hypothesis import given
from hypothesis import strategies as st

from hcs_trader.api import create_app
from hcs_trader.data.kraken import (
    DEPTH_URL,
    OHLC_URL,
    SOURCE,
    KrakenQuoteProvider,
)
from hcs_trader.data.provider import InvalidQuote
from hcs_trader.domain.assets import MarketId
from hcs_trader.domain.execution import SpotOrder, SpotOrderType, SpotSide
from hcs_trader.domain.lifecycle import OrderState
from hcs_trader.domain.market_data import Candle, Quote
from hcs_trader.domain.markets import (
    DEFAULT_MARKET_ID,
    MARKET_REGISTRY,
    MarketSpec,
    MarketStatus,
    OrderShapeError,
    UnknownMarket,
    validate_order_shape,
)
from hcs_trader.kraken_spot import KrakenSpotAdapter
from hcs_trader.risk import RiskEngine, RiskPolicy, RiskRejected
from hcs_trader.spot_execution import SUPPORTED_MARKETS, KrakenSpotExecution
from hcs_trader.storage import AuditStore

_SECRET = base64.b64encode(b"01234567890123456789012345678901").decode("ascii")
_BOOK = {
    "asks": [["1717.02000", "4.696", 1795362627]],
    "bids": [["1717.01000", "4.854", 1795362627]],
}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_registry_seed_and_default() -> None:
    symbols = {m.symbol for m in MARKET_REGISTRY.markets()}
    assert symbols == {
        f"{base}/{quote}"
        for base in ("BTC", "ETH", "SOL", "LTC")
        for quote in ("USD", "EUR")
    }
    assert MARKET_REGISTRY.default.market_id == DEFAULT_MARKET_ID
    assert MARKET_REGISTRY.resolve(None).symbol == "BTC/USD"
    assert MARKET_REGISTRY.quote_symbols("ETH") == ("USD", "EUR")
    assert MARKET_REGISTRY.quote_symbols("DGB") == ()
    dgb = next(a for a in MARKET_REGISTRY.assets() if a.symbol == "DGB")
    assert dgb.status is MarketStatus.READ_ONLY
    for bad in ("DGB/USD", "btc/usd", "BTC/USDT", ""):
        with pytest.raises(UnknownMarket):
            MARKET_REGISTRY.resolve(bad)


def test_only_btc_usd_is_live_allowed() -> None:
    assert MARKET_REGISTRY.live_allowed_symbols() == {"BTC/USD"}
    assert SUPPORTED_MARKETS == {"BTC/USD"}
    assert RiskPolicy().allowed_markets == {"BTC/USD"}
    for spec in MARKET_REGISTRY.markets():
        assert spec.allows_live is (spec.symbol == "BTC/USD")
        assert not spec.allows_paper or spec.allows_live


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("symbol", "pair", "key"),
    [
        ("BTC/USD", "XBTUSD", "XXBTZUSD"),
        ("ETH/USD", "ETHUSD", "XETHZUSD"),
        ("ETH/EUR", "ETHEUR", "XETHZEUR"),
        ("LTC/USD", "LTCUSD", "XLTCZUSD"),
        ("SOL/EUR", "SOLEUR", "SOLEUR"),
    ],
)
async def test_quote_uses_market_pair_and_response_key(
    symbol: str, pair: str, key: str
) -> None:
    def depth(request: httpx2.Request) -> httpx2.Response:
        assert request.url.copy_with(query=None) == httpx2.URL(DEPTH_URL)
        assert dict(request.url.params) == {"pair": pair, "count": "1"}
        return httpx2.Response(200, json={"error": [], "result": {key: _BOOK}})

    market = MARKET_REGISTRY.resolve(symbol).market_id
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(depth)) as client:
        quote = await KrakenQuoteProvider(client).get_quote(market)
    assert quote.market == market and quote.source == SOURCE


@pytest.mark.anyio
@pytest.mark.parametrize(
    "result",
    [
        {"XXBTZUSD": _BOOK},  # another market's key
        {"XETHZUSD": _BOOK, "XXBTZUSD": _BOOK},  # ambiguous
        {},
    ],
)
async def test_quote_rejects_unregistered_or_ambiguous_response_keys(
    result: dict[str, Any],
) -> None:
    def depth(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"error": [], "result": result})

    market = MARKET_REGISTRY.resolve("ETH/USD").market_id
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(depth)) as client:
        with pytest.raises(InvalidQuote):
            await KrakenQuoteProvider(client).get_quote(market)


@pytest.mark.anyio
async def test_candles_resolve_key_from_response() -> None:
    def ohlc(request: httpx2.Request) -> httpx2.Response:
        assert request.url.copy_with(query=None) == httpx2.URL(OHLC_URL)
        assert dict(request.url.params) == {"pair": "LTCUSD", "interval": "60"}
        row = [3600, "60", "61", "59", "60.5", "60", "2.5", 7]
        return httpx2.Response(
            200,
            json={
                "error": [],
                "result": {"XLTCZUSD": [row, [7200, *row[1:]]], "last": 7200},
            },
        )

    market = MARKET_REGISTRY.resolve("LTC/USD").market_id
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(ohlc)) as client:
        candles = await KrakenQuoteProvider(client).get_candles(market, 60)
    assert len(candles) == 1 and candles[0].market == market


@pytest.mark.anyio
async def test_unregistered_market_makes_no_request() -> None:
    def unexpected(request: httpx2.Request) -> httpx2.Response:
        raise AssertionError(f"Unexpected request to {request.url}")

    dgb_asset = next(a for a in MARKET_REGISTRY.assets() if a.symbol == "DGB")
    dgb = MarketId("kraken", dgb_asset.asset_id, DEFAULT_MARKET_ID.quote)
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(unexpected)) as client:
        with pytest.raises(ValueError, match="allowlisted"):
            await KrakenQuoteProvider(client).get_quote(dgb)
        with pytest.raises(ValueError, match="allowlisted"):
            await KrakenQuoteProvider(client).get_candles(dgb, 60)


def _order(market: str) -> SpotOrder:
    now = datetime.now(UTC)
    return SpotOrder(
        client_order_id="mkt-1",
        order_id="ord-mkt-1",
        venue="kraken",
        market=market,
        side=SpotSide.BUY,
        order_type=SpotOrderType.LIMIT,
        quantity=Decimal("0.5"),
        limit_price=Decimal("100.00"),
        state=OrderState.PREVIEWED,
        risk_decision_id="x",
        created_at=now,
        updated_at=now,
    )


def _quote(market: MarketId) -> Quote:
    now = datetime.now(UTC)
    return Quote(market, SOURCE, Decimal("99.9"), Decimal("100"), now, now, now)


@pytest.mark.anyio
@pytest.mark.parametrize("symbol", ["ETH/USD", "SOL/USD", "LTC/EUR", "DGB/USD"])
async def test_live_submission_rejected_for_non_live_allowed_markets(
    tmp_path: Path, symbol: str
) -> None:
    store = AuditStore(tmp_path / "markets.db")
    store.initialize()
    calls: list[str] = []

    def venue(request: httpx2.Request) -> httpx2.Response:
        calls.append(request.url.path)
        return httpx2.Response(200, json={"error": [], "result": {}})

    # DGB has no registry market, so its quote carries the default identity.
    market_id = (
        DEFAULT_MARKET_ID
        if symbol == "DGB/USD"
        else MARKET_REGISTRY.resolve(symbol).market_id
    )
    # The operator widens the allowlist: the registry must still reject.
    policy = RiskPolicy(
        live_mode_enabled=True,
        allowed_markets=frozenset({"BTC/USD", symbol}),
    )
    quote = _quote(market_id)
    quality = quote.quality_at(
        datetime.now(UTC),
        max_age=timedelta(seconds=30),
        max_clock_skew=timedelta(seconds=2),
    )
    verdict = RiskEngine(policy).evaluate_order(
        _order(symbol), quote, quality, 0, "v1.0.0", datetime.now(UTC)
    )
    assert not verdict.allowed
    assert "not live_allowed" in (verdict.reason or "")

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(venue)) as client:
        execution = KrakenSpotExecution(
            store, KrakenSpotAdapter("k", _SECRET, client), RiskEngine(policy)
        )
        with pytest.raises(RiskRejected):
            await execution.submit(
                _order(symbol),
                quote,
                candidate_id="none",
                strategy_version="v1.0.0",
            )
    assert calls == []
    assert store.list_open_spot_orders("kraken") == []


def test_shape_check_rejects_registry_read_only_even_with_btc_quote() -> None:
    eth = MARKET_REGISTRY.resolve("ETH/USD").market_id
    with pytest.raises(RiskRejected):
        KrakenSpotExecution._check_shape(_order("ETH/USD"), _quote(eth))
    with pytest.raises(RiskRejected):
        KrakenSpotExecution._check_shape(_order("ETH/USD"), _quote(DEFAULT_MARKET_ID))


@pytest.mark.anyio
async def test_btc_usd_rejects_request_pair_name_as_response_key() -> None:
    def depth(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"error": [], "result": {"XBTUSD": _BOOK}})

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(depth)) as client:
        with pytest.raises(InvalidQuote):
            await KrakenQuoteProvider(client).get_quote(DEFAULT_MARKET_ID)


def test_live_shape_check_enforces_registry_shape() -> None:
    quote = _quote(DEFAULT_MARKET_ID)
    spec = MARKET_REGISTRY.default
    ok = _order("BTC/USD")
    KrakenSpotExecution._check_shape(ok, quote)  # baseline still passes
    for qty, price in [
        (spec.min_quantity / 2, Decimal("50000")),  # below minimum size
        (Decimal("0.00010000001"), Decimal("50000")),  # quantity precision
        (Decimal("0.0001"), Decimal("50000.12")),  # price precision (1 decimal)
    ]:
        bad = replace(ok, quantity=qty, limit_price=price)
        with pytest.raises(RiskRejected):
            KrakenSpotExecution._check_shape(bad, quote)


def test_validate_order_shape_examples() -> None:
    eth = MARKET_REGISTRY.resolve("ETH/USD")
    validate_order_shape(eth, Decimal("0.002"), Decimal("1700.12"))
    for qty, price in [
        (Decimal("0.001"), None),  # below minimum
        (Decimal("0.0020000001"), None),  # too many decimals
        (Decimal("1"), Decimal("1700.123")),  # price precision
        (Decimal("0.002"), Decimal("100")),  # notional 0.2 below the 0.5 minimum
        (Decimal("NaN"), None),
        (Decimal("-1"), None),
    ]:
        with pytest.raises(OrderShapeError):
            validate_order_shape(eth, qty, price)


_SPECS = st.sampled_from(MARKET_REGISTRY.markets())


@given(
    spec=_SPECS,
    units=st.integers(min_value=1, max_value=10**12),
    price_units=st.integers(min_value=1, max_value=10**9),
)
def test_exact_shapes_follow_minimums(
    spec: MarketSpec, units: int, price_units: int
) -> None:
    quantity = Decimal(units).scaleb(-spec.quantity_decimals)
    price = Decimal(price_units).scaleb(-spec.price_decimals)
    if quantity >= spec.min_quantity and quantity * price >= spec.min_notional:
        validate_order_shape(spec, quantity, price)
    else:
        with pytest.raises(OrderShapeError):
            validate_order_shape(spec, quantity, price)


@given(
    spec=_SPECS,
    quantity=st.decimals(allow_nan=True, allow_infinity=True, places=None),
)
def test_over_precise_or_invalid_quantity_is_rejected(
    spec: MarketSpec, quantity: Decimal
) -> None:
    exact = (
        quantity.is_finite()
        and quantity > 0
        and (Fraction(quantity) * 10**spec.quantity_decimals).denominator == 1
        and quantity >= spec.min_quantity
    )
    if exact:
        validate_order_shape(spec, quantity)
    else:
        with pytest.raises(OrderShapeError):
            validate_order_shape(spec, quantity)


# --- REST -------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> AuditStore:
    store = AuditStore(tmp_path / "api.db")
    store.initialize()
    return store


@pytest.fixture
def api(store: AuditStore) -> Any:
    with TestClient(create_app(store), base_url="http://127.0.0.1") as client:
        yield client


def test_markets_endpoint_and_assets_status(api: Any) -> None:
    markets = api.get("/api/v1/market/markets").json()
    assert [m["symbol"] for m in markets if m["live_allowed"]] == ["BTC/USD"]
    assert all(not m["paper_allowed"] for m in markets if m["symbol"] != "BTC/USD")
    assets = {a["symbol"]: a for a in api.get("/api/v1/market/assets").json()}
    assert assets["DGB"]["status"] == "read_only"
    assert assets["DGB"]["quote_assets"] == []
    assert assets["ETH"]["quote_assets"] == ["USD", "EUR"]
    assert assets["USD"]["status"] is None


def test_market_query_parameter_selects_archived_data(
    api: Any, store: AuditStore
) -> None:
    eth = MARKET_REGISTRY.resolve("ETH/USD").market_id
    now = datetime.now(UTC)
    store.record_quote(
        Quote(eth, SOURCE, Decimal("1700"), Decimal("1701"), now, now, now)
    )
    store.record_candles(
        [
            Candle(
                eth,
                SOURCE,
                60,
                now - timedelta(hours=1),
                now,
                Decimal(1),
                Decimal(2),
                Decimal(1),
                Decimal(2),
                Decimal(3),
                4,
            )
        ]
    )
    history = api.get("/api/v1/market/quotes/history", params={"market": "ETH/USD"})
    assert [q["bid"] for q in history.json()] == ["1700"]
    assert api.get("/api/v1/market/quotes/history").json() == []
    candles = api.get("/api/v1/market/candles", params={"market": "ETH/USD"}).json()
    assert len(candles) == 1 and candles[0]["base_asset"] == str(eth.base)
    assert api.get("/api/v1/market/candles").json() == []
    book = api.get("/api/v1/market/orderbook", params={"market": "ETH/USD"}).json()
    assert book["market"] == "ETH/USD" and book["quality"] == "FRESH"
    for path in ("quotes/history", "candles", "orderbook", "indicators"):
        response = api.get(f"/api/v1/market/{path}", params={"market": "DGB/USD"})
        assert response.status_code == 422
