"""Public Kraken Spot top-of-book and OHLCV adapter."""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

import httpx2
from pydantic import BaseModel, Field, ValidationError

from hcs_trader.data.provider import InvalidQuote, QuoteRateLimited, QuoteUnavailable
from hcs_trader.domain.assets import MarketId
from hcs_trader.domain.market_data import Candle, Quote
from hcs_trader.domain.markets import (
    DEFAULT_MARKET_ID,
    MARKET_REGISTRY,
    MarketSpec,
    UnknownMarket,
)

SOURCE = "kraken-spot"
DEPTH_URL = "https://api.kraken.com/0/public/Depth"
OHLC_URL = "https://api.kraken.com/0/public/OHLC"
CANDLE_INTERVALS = frozenset({1, 5, 15, 30, 60, 240, 1440})
# Kept for callers and tests; the registry owns the definition.
BTC_USD = DEFAULT_MARKET_ID
_MAX_RESPONSE_BYTES = 16_384


class _Book(BaseModel):
    bids: list[tuple[str, str, int]] = Field(min_length=1, max_length=1)
    asks: list[tuple[str, str, int]] = Field(min_length=1, max_length=1)


class _Depth(BaseModel):
    error: list[str]
    result: dict[str, _Book] | None = None


class _TradingStatus(BaseModel):
    status: str
    timestamp: datetime


class _SystemStatus(BaseModel):
    error: list[str]
    result: _TradingStatus | None = None


_OhlcRow = tuple[int, str, str, str, str, str, str, int]


class _Ohlc(BaseModel):
    error: list[str]
    result: dict[str, list[_OhlcRow] | int] | None = None


def _spec(market: MarketId) -> MarketSpec:
    try:
        spec = MARKET_REGISTRY.by_market_id(market)
    except UnknownMarket:
        raise ValueError("Kraken market is not allowlisted") from None
    if spec.venue != "kraken":
        raise ValueError("Kraken market is not allowlisted")
    return spec


def _resolve_key(keys: set[str], spec: MarketSpec, what: str) -> str:
    """Return the single pair key in a response, which must be a known name.

    Kraken names the result key differently from the request pair (XBTUSD is
    answered as XXBTZUSD), so the key is read from the response and checked
    against the names registered for the requested market.
    """
    if len(keys) != 1:
        raise InvalidQuote(f"Kraken did not identify the requested {what}")
    key = next(iter(keys))
    if key not in spec.response_keys:
        raise InvalidQuote(f"Kraken did not identify the requested {what}")
    return key


class KrakenQuoteProvider:
    def __init__(self, client: httpx2.AsyncClient) -> None:
        self._client = client

    async def trading_is_online(self) -> bool:
        payload = await self._read(
            "https://api.kraken.com/0/public/SystemStatus", {}, 65_536
        )
        try:
            envelope = _SystemStatus.model_validate_json(payload, strict=True)
        except ValidationError as error:
            raise InvalidQuote("Invalid Kraken system status response") from error
        _check_errors(envelope.error)
        status = envelope.result
        if status is None or status.timestamp.utcoffset() is None:
            raise InvalidQuote("Kraken trading status or timestamp unavailable")
        age = (datetime.now(UTC) - status.timestamp).total_seconds()
        if not -2 <= age <= 30:
            raise InvalidQuote(
                "Kraken trading status timestamp is outside the freshness window"
            )
        return status.status == "online"

    async def get_quote(self, market: MarketId) -> Quote:
        spec = _spec(market)

        try:
            payload = await self._read(
                DEPTH_URL, {"pair": spec.venue_pair, "count": "1"}, _MAX_RESPONSE_BYTES
            )
            received_at = datetime.now(UTC)
            depth = _Depth.model_validate_json(payload, strict=True)
        except ValidationError as error:
            raise InvalidQuote("Invalid Kraken response envelope") from error
        _check_errors(depth.error)
        if depth.result is None:
            raise InvalidQuote("Kraken did not identify the requested market")

        book = depth.result[_resolve_key(set(depth.result), spec, "market")]
        try:
            bid, bid_at = _level(book.bids[0])
            ask, ask_at = _level(book.asks[0])
            return Quote(market, SOURCE, bid, ask, bid_at, ask_at, received_at)
        except (InvalidOperation, OverflowError, OSError, ValueError) as error:
            raise InvalidQuote("Invalid Kraken price level") from error

    async def get_candles(
        self, market: MarketId, interval_minutes: int
    ) -> list[Candle]:
        spec = _spec(market)
        if interval_minutes not in CANDLE_INTERVALS:
            raise ValueError("Kraken market or candle interval is not allowlisted")
        payload = await self._read(
            OHLC_URL,
            {"pair": spec.venue_pair, "interval": str(interval_minutes)},
            262_144,
        )
        received_at = datetime.now(UTC)
        try:
            ohlc = _Ohlc.model_validate_json(payload, strict=True)
            _check_errors(ohlc.error)
            if ohlc.result is None or "last" not in ohlc.result:
                raise InvalidQuote("Kraken did not identify the requested OHLC market")
            pair_key = _resolve_key(set(ohlc.result) - {"last"}, spec, "OHLC market")
            rows = ohlc.result[pair_key]
            if not isinstance(ohlc.result["last"], int) or not isinstance(rows, list):
                raise InvalidQuote("Invalid Kraken OHLC rows or cursor")
            # The live endpoint can return 720 committed rows plus the open interval.
            if not 1 <= len(rows) <= 721:
                raise InvalidQuote("Unexpected Kraken candle count")
            previous = 0
            for timestamp, *_ in rows:
                if timestamp <= previous or timestamp % (interval_minutes * 60):
                    raise InvalidQuote(
                        "Duplicate, unordered or misaligned Kraken candle"
                    )
                previous = timestamp
            # The final entry is provisional according to the provider contract.
            return [
                _candle(market, interval_minutes, received_at, row) for row in rows[:-1]
            ]
        except (InvalidOperation, OverflowError, OSError, ValueError) as error:
            raise InvalidQuote("Invalid Kraken OHLC response") from error

    async def _read(self, url: str, params: dict[str, str], limit: int) -> bytearray:
        try:
            async with (
                asyncio.timeout(5),
                self._client.stream(
                    "GET", url, params=params, follow_redirects=False
                ) as response,
            ):
                if response.status_code == 429:
                    raise QuoteRateLimited("Kraken rate limit")
                response.raise_for_status()
                payload = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(payload) + len(chunk) > limit:
                        raise InvalidQuote("Kraken response exceeds the size limit")
                    payload.extend(chunk)
                return payload
        except (httpx2.HTTPError, TimeoutError) as error:
            raise QuoteUnavailable("Kraken request failed") from error


def _check_errors(errors: list[str]) -> None:
    if "EAPI:Rate limit exceeded" in errors or any(
        item.startswith("EService: Throttled:") for item in errors
    ):
        raise QuoteRateLimited("Kraken rate limit")
    if errors:
        raise QuoteUnavailable("Kraken returned a provider error")


def _candle(
    market: MarketId, interval: int, received_at: datetime, row: _OhlcRow
) -> Candle:
    timestamp, opening, high, low, close, _vwap, volume, trades = row
    return Candle(
        market,
        SOURCE,
        interval,
        datetime.fromtimestamp(timestamp, UTC),
        received_at,
        Decimal(opening),
        Decimal(high),
        Decimal(low),
        Decimal(close),
        Decimal(volume),
        trades,
    )


def _level(level: tuple[str, str, int]) -> tuple[Decimal, datetime]:
    price, quantity, timestamp = level
    amount = Decimal(quantity)
    if not amount.is_finite() or amount <= 0 or timestamp <= 0:
        raise ValueError("Invalid price level metadata")
    return Decimal(price), datetime.fromtimestamp(timestamp, UTC)
