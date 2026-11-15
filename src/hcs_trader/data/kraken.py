"""Public Kraken Spot L2 top-of-book adapter."""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

import httpx2
from pydantic import BaseModel, Field, ValidationError

from hcs_trader.data.provider import InvalidQuote, QuoteRateLimited, QuoteUnavailable
from hcs_trader.domain.assets import AssetId, MarketId
from hcs_trader.domain.market_data import Quote

SOURCE = "kraken-spot"
DEPTH_URL = "https://api.kraken.com/0/public/Depth"
BTC_USD = MarketId(
    "kraken",
    AssetId.parse("bitcoin:mainnet/native:BTC"),
    AssetId.parse("fiat:iso4217/currency:USD"),
)
_MAX_RESPONSE_BYTES = 16_384


class _Book(BaseModel):
    bids: list[tuple[str, str, int]] = Field(min_length=1, max_length=1)
    asks: list[tuple[str, str, int]] = Field(min_length=1, max_length=1)


class _Depth(BaseModel):
    error: list[str]
    result: dict[str, _Book] | None = None


class KrakenQuoteProvider:
    def __init__(self, client: httpx2.AsyncClient) -> None:
        self._client = client

    async def get_quote(self, market: MarketId) -> Quote:
        if market != BTC_USD:
            raise ValueError("Kraken market is not allowlisted")

        try:
            async with asyncio.timeout(5):
                payload = await self._read_depth()
        except (httpx2.HTTPError, TimeoutError) as error:
            raise QuoteUnavailable("Kraken request failed") from error

        received_at = datetime.now(UTC)
        try:
            depth = _Depth.model_validate_json(payload, strict=True)
        except ValidationError as error:
            raise InvalidQuote("Invalid Kraken response envelope") from error
        if "EAPI:Rate limit exceeded" in depth.error or any(
            item.startswith("EService: Throttled:") for item in depth.error
        ):
            raise QuoteRateLimited("Kraken rate limit")
        if depth.error:
            raise QuoteUnavailable("Kraken returned a provider error")
        if depth.result is None or set(depth.result) != {"XXBTZUSD"}:
            raise InvalidQuote("Kraken did not identify the requested market")

        book = depth.result["XXBTZUSD"]
        try:
            bid, bid_at = _level(book.bids[0])
            ask, ask_at = _level(book.asks[0])
            return Quote(market, SOURCE, bid, ask, bid_at, ask_at, received_at)
        except (InvalidOperation, OverflowError, OSError, ValueError) as error:
            raise InvalidQuote("Invalid Kraken price level") from error

    async def _read_depth(self) -> bytearray:
        async with self._client.stream(
            "GET",
            DEPTH_URL,
            params={"pair": "XBTUSD", "count": "1"},
            follow_redirects=False,
        ) as response:
            if response.status_code == 429:
                raise QuoteRateLimited("Kraken rate limit")
            response.raise_for_status()
            payload = bytearray()
            async for chunk in response.aiter_bytes():
                if len(payload) + len(chunk) > _MAX_RESPONSE_BYTES:
                    raise InvalidQuote("Kraken response exceeds the size limit")
                payload.extend(chunk)
            return payload


def _level(level: tuple[str, str, int]) -> tuple[Decimal, datetime]:
    price, quantity, timestamp = level
    amount = Decimal(quantity)
    if not amount.is_finite() or amount <= 0 or timestamp <= 0:
        raise ValueError("Invalid price level metadata")
    return Decimal(price), datetime.fromtimestamp(timestamp, UTC)
