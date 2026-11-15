from datetime import UTC, datetime
from decimal import Decimal

import httpx2
import pytest

from hcs_trader.data.kraken import BTC_USD, DEPTH_URL, OHLC_URL, KrakenQuoteProvider
from hcs_trader.data.provider import InvalidQuote, QuoteRateLimited, QuoteUnavailable
from hcs_trader.domain.assets import MarketId

# Synthetic fixture based on Kraken's Spot REST Get Order Book example.
_BOOK = {
    "asks": [["30384.10000", "2.059", 1688671659]],
    "bids": [["30297.00000", "1.115", 1688671636]],
}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_depth_request_and_quote_provenance() -> None:
    def depth(request: httpx2.Request) -> httpx2.Response:
        assert request.method == "GET"
        assert request.url.copy_with(query=None) == httpx2.URL(DEPTH_URL)
        assert dict(request.url.params) == {"pair": "XBTUSD", "count": "1"}
        return httpx2.Response(200, json={"error": [], "result": {"XXBTZUSD": _BOOK}})

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(depth)) as client:
        quote = await KrakenQuoteProvider(client).get_quote(BTC_USD)
    assert quote.market == BTC_USD
    assert quote.source == "kraken-spot"
    assert quote.bid == Decimal("30297.00000")
    assert quote.ask == Decimal("30384.10000")
    assert quote.bid_updated_at == datetime.fromtimestamp(1688671636, UTC)
    assert quote.ask_updated_at == datetime.fromtimestamp(1688671659, UTC)
    assert quote.received_at >= quote.ask_updated_at


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status", "body", "failure"),
    [
        (429, "", QuoteRateLimited),
        (503, "", QuoteUnavailable),
        (302, "", QuoteUnavailable),
        (200, "not JSON", InvalidQuote),
        (200, " " * 16_385, InvalidQuote),
        (200, '{"error":["EAPI:Rate limit exceeded"]}', QuoteRateLimited),
        (200, '{"error":["EGeneral:Internal error"]}', QuoteUnavailable),
        (200, '{"error":[],"result":{}}', InvalidQuote),
        (200, '{"error":[],"result":{"XXBTZUSD":{"asks":[],"bids":[]}}}', InvalidQuote),
        (
            200,
            '{"error":[],"result":{"XXBTZUSD":'
            '{"asks":[["1","1",1]],"bids":[["2","1",1]]}}}',
            InvalidQuote,
        ),
    ],
)
async def test_depth_failures_are_normalized(
    status: int, body: str, failure: type[QuoteUnavailable]
) -> None:
    def respond(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            status, content=body, headers={"location": "https://example.invalid"}
        )

    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(respond), follow_redirects=True
    ) as client:
        with pytest.raises(failure) as caught:
            await KrakenQuoteProvider(client).get_quote(BTC_USD)
        assert type(caught.value) is failure


@pytest.mark.anyio
async def test_unknown_market_does_not_make_a_request() -> None:
    def unexpected(request: httpx2.Request) -> httpx2.Response:
        raise AssertionError(f"Unexpected request to {request.url}")

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(unexpected)) as client:
        with pytest.raises(ValueError, match="allowlisted"):
            await KrakenQuoteProvider(client).get_quote(
                MarketId("unverified", BTC_USD.base, BTC_USD.quote)
            )


@pytest.mark.anyio
async def test_ohlc_returns_exact_closed_candles_and_preserves_gaps() -> None:
    def respond(request: httpx2.Request) -> httpx2.Response:
        assert request.url.copy_with(query=None) == httpx2.URL(OHLC_URL)
        assert dict(request.url.params) == {"pair": "XBTUSD", "interval": "1"}
        return httpx2.Response(
            200,
            json={
                "error": [],
                "result": {
                    "XXBTZUSD": [
                        [1688671200, "10", "11", "9", "10.0010", "10", "0.12345678", 1],
                        [1688671320, "10", "12", "9", "11", "10", "0", 0],
                        [1688671380, "11", "12", "8", "8", "10", "10", 1],
                    ],
                    "last": 1688671320,
                },
            },
        )

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as client:
        candles = await KrakenQuoteProvider(client).get_candles(BTC_USD, 1)
    assert len(candles) == 2
    assert candles[0].market == BTC_USD
    assert candles[0].source == "kraken-spot"
    assert str(candles[0].close) == "10.0010"
    assert candles[0].volume == Decimal("0.12345678")
    assert candles[0].closed_at == datetime.fromtimestamp(1688671260, UTC)
    assert candles[1].opened_at == datetime.fromtimestamp(1688671320, UTC)
    assert candles[1].trade_count == 0


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("first_timestamp", "second_timestamp", "high", "volume", "trades"),
    [
        (1688671200, 1688671200, "12", "1", 1),
        (1688671260, 1688671200, "12", "1", 1),
        (1688671201, 1688671260, "12", "1", 1),
        (1688671200, 1688671260, "9", "1", 1),
        (1688671200, 1688671260, "12", "-1", 1),
        (1688671200, 1688671260, "12", "1", -1),
        (1688671200, 1688671260, "Infinity", "1", 1),
        (4102444800, 4102444860, "12", "1", 1),
    ],
)
async def test_ohlc_rejects_invalid_closed_candle_batches(
    first_timestamp: int, second_timestamp: int, high: str, volume: str, trades: int
) -> None:
    def respond(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={
                "error": [],
                "result": {
                    "XXBTZUSD": [
                        [first_timestamp, "10", high, "8", "10", "10", volume, trades],
                        [second_timestamp, "10", "12", "8", "10", "10", "1", 1],
                    ],
                    "last": first_timestamp,
                },
            },
        )

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as client:
        with pytest.raises(InvalidQuote):
            await KrakenQuoteProvider(client).get_candles(BTC_USD, 1)


@pytest.mark.anyio
async def test_ohlc_accepts_full_window_plus_provisional_candle() -> None:
    def respond(request: httpx2.Request) -> httpx2.Response:
        start = 1688671200
        rows = [
            [start + index * 60, "10", "12", "8", "10", "10", "1", 1]
            for index in range(721)
        ]
        return httpx2.Response(
            200,
            json={"error": [], "result": {"XXBTZUSD": rows, "last": start + 719 * 60}},
        )

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as client:
        candles = await KrakenQuoteProvider(client).get_candles(BTC_USD, 1)
    assert len(candles) == 720
