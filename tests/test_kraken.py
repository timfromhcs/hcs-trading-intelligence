from datetime import UTC, datetime
from decimal import Decimal

import httpx2
import pytest

from hcs_trader.data.kraken import BTC_USD, DEPTH_URL, KrakenQuoteProvider
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
