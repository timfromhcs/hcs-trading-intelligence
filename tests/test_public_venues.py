"""Offline contract tests for the keyless public market-data adapter."""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx2
import pytest

from hcs_trader.data.provider import InvalidQuote, QuoteRateLimited
from hcs_trader.data.public_venues import (
    COINBASE,
    VenueConfig,
    coinbase_provider,
)
from hcs_trader.domain.assets import AssetId, MarketId
from hcs_trader.domain.markets import MARKET_REGISTRY

MARKET = MARKET_REGISTRY.resolve("BTC/USD").market_id
NOW = datetime.now(UTC).replace(microsecond=0)


def _ticker() -> dict[str, object]:
    return {
        "price": "101.5",
        "bid": "101.0",
        "ask": "102.0",
        "time": (NOW - timedelta(seconds=2)).isoformat(),
        "trade_id": 1,
        "size": "1",
        "volume": "100",
    }


def _client(
    handler: Callable[[httpx2.Request], httpx2.Response],
) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


def test_venue_config_rejects_unlisted_host() -> None:
    with pytest.raises(ValueError, match="allowlist"):
        VenueConfig(
            name="bad",
            base_url="https://evil.example",
            allowed_hosts=frozenset({next(iter(COINBASE.allowed_hosts))}),
            source="bad",
            quote_assets=frozenset({"USD"}),
            base_assets=frozenset({"BTC"}),
        )


def test_coinbase_quote_is_normalized() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.host == "api.exchange.coinbase.com"
        assert request.url.path == "/products/BTC-USD/ticker"
        return httpx2.Response(200, json=_ticker())

    async def exercise() -> None:
        async with _client(handler) as client:
            quote = await coinbase_provider(client).get_quote(MARKET)
        assert quote.source == "coinbase-exchange-public"
        assert quote.bid == Decimal("101.0")
        assert quote.ask == Decimal("102.0")
        assert quote.bid < quote.ask
        assert quote.market == MARKET

    asyncio.run(exercise())


def test_coinbase_rate_limit_maps_to_provider_error() -> None:
    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(429, json={"message": "slow down"})

    async def exercise() -> None:
        async with _client(handler) as client:
            with pytest.raises(QuoteRateLimited):
                await coinbase_provider(client).get_quote(MARKET)

    asyncio.run(exercise())


def test_crossed_ticker_is_rejected() -> None:
    payload = _ticker()
    payload["bid"] = "103"

    async def exercise() -> None:
        async with _client(lambda _: httpx2.Response(200, json=payload)) as client:
            with pytest.raises(InvalidQuote):
                await coinbase_provider(client).get_quote(MARKET)

    asyncio.run(exercise())


def test_unregistered_asset_is_rejected_before_request() -> None:
    dgb = MarketId(
        "kraken",
        AssetId.parse("digibyte:mainnet/native:DGB"),
        AssetId.parse("fiat:iso4217/currency:USD"),
    )

    def handler(_: httpx2.Request) -> httpx2.Response:
        raise AssertionError("No request may be sent for an unallowlisted base")

    async def exercise() -> None:
        async with _client(handler) as client:
            with pytest.raises(InvalidQuote):
                await coinbase_provider(client).get_quote(dgb)

    asyncio.run(exercise())


def test_candles_drop_open_bucket_and_keep_closed_rows() -> None:
    def opened(minutes: int) -> datetime:
        return (NOW - timedelta(minutes=minutes)).replace(second=0, microsecond=0)

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/products/BTC-USD/candles"
        assert request.url.params["granularity"] == "60"
        rows = [
            [ts.timestamp(), 99, 103, 100, 102, 1.5]
            for ts in (opened(3), opened(2), opened(1))
        ] + [
            [
                (datetime.now(UTC) + timedelta(seconds=30)).timestamp(),
                100,
                104,
                102,
                103,
                0.5,
            ]
        ]
        return httpx2.Response(200, json=rows)

    async def exercise() -> None:
        async with _client(handler) as client:
            candles = await coinbase_provider(client).get_ohlcv(MARKET, 1)
        assert len(candles) == 3
        assert candles[-1].close == Decimal("102")
        assert candles == sorted(candles, key=lambda candle: candle.opened_at)

    asyncio.run(exercise())


def test_oversized_response_is_rejected() -> None:
    def handler(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=b"x" * 300_000)

    async def exercise() -> None:
        async with _client(handler) as client:
            with pytest.raises(InvalidQuote):
                await coinbase_provider(client).get_quote(MARKET)

    asyncio.run(exercise())
