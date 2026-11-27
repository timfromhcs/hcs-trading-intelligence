"""Keyless public market-data adapters behind the shared provider contract.

Only hostnames that are explicitly listed in a venue configuration are ever
requested. No adapter here can trade, sign, or access an authenticated endpoint.
A venue stays unavailable until its verification record exists in
``docs/DATA_SOURCES.md``; Binance is deliberately not enabled by this module.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit

import httpx2
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from hcs_trader.data.provider import (
    InvalidQuote,
    ProviderHealth,
    ProviderStatus,
    QuoteRateLimited,
    QuoteUnavailable,
)
from hcs_trader.domain.assets import MarketId
from hcs_trader.domain.market_data import Candle, Quote
from hcs_trader.domain.markets import MARKET_REGISTRY, MarketSpec, UnknownMarket

_MAX_RESPONSE_BYTES = 262_144
_TIMEOUT_SECONDS = 5
_COINBASE_HOST = "api.exchange.coinbase.com"
_COINBASE_SOURCE = "coinbase-exchange-public"
_COINBASE_QUOTE_ASSETS = frozenset({"USD", "EUR"})
_COINBASE_BASES = frozenset({"BTC", "ETH", "SOL", "LTC"})
_GRANULARITIES = frozenset({60, 300, 900, 3600, 21_600, 86_400})


@dataclass(frozen=True, slots=True)
class VenueConfig:
    """Static, reviewed connection policy for one public venue."""

    name: str
    base_url: str
    allowed_hosts: frozenset[str]
    source: str
    quote_assets: frozenset[str]
    base_assets: frozenset[str]

    def __post_init__(self) -> None:
        parts = urlsplit(self.base_url)
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError("A public venue must use an absolute https URL")
        if parts.hostname not in self.allowed_hosts:
            raise ValueError("Venue host is not in the allowlist")
        if parts.port not in (None, 443) or parts.username or parts.password:
            raise ValueError("Venue URL has an invalid authority")


COINBASE = VenueConfig(
    name="coinbase",
    base_url="https://api.exchange.coinbase.com",
    allowed_hosts=frozenset({_COINBASE_HOST}),
    source=_COINBASE_SOURCE,
    quote_assets=_COINBASE_QUOTE_ASSETS,
    base_assets=_COINBASE_BASES,
)


class _Ticker(BaseModel):
    model_config = ConfigDict(extra="ignore")

    price: str
    bid: str
    ask: str
    time: datetime


class _Time(BaseModel):
    model_config = ConfigDict(extra="ignore")

    iso: datetime


class _Candles(BaseModel):
    root: list[list[float]] = Field(max_length=1000)


class PublicRestProvider:
    """Read-only adapter for a reviewed public REST venue."""

    def __init__(self, client: httpx2.AsyncClient, config: VenueConfig) -> None:
        self._client = client
        self._config = config

    def _spec(self, market: MarketId) -> MarketSpec:
        try:
            spec = MARKET_REGISTRY.by_market_id(market)
        except UnknownMarket:
            raise InvalidQuote("Market is not registered") from None
        if (
            spec.base.reference not in self._config.base_assets
            or spec.quote.reference not in self._config.quote_assets
        ):
            raise InvalidQuote("Asset is not allowlisted for this venue")
        return spec

    def _url(self, path: str) -> str:
        if ".." in path or not path.startswith("/"):
            raise InvalidQuote("Invalid venue path")
        url = f"{self._config.base_url}{path}"
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or parts.hostname not in self._config.allowed_hosts
            or parts.port not in (None, 443)
            or parts.username
            or parts.password
        ):
            raise InvalidQuote("Venue URL failed the host allowlist")
        return url

    async def _read_json(self, path: str, params: dict[str, str]) -> Any:
        url = self._url(path)
        try:
            async with (
                asyncio.timeout(_TIMEOUT_SECONDS),
                self._client.stream(
                    "GET", url, params=params, follow_redirects=False
                ) as response,
            ):
                if response.status_code == 429:
                    raise QuoteRateLimited(f"{self._config.name} rate limit")
                response.raise_for_status()
                payload = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(payload) + len(chunk) > _MAX_RESPONSE_BYTES:
                        raise InvalidQuote("Venue response exceeds the size limit")
                    payload.extend(chunk)
        except (httpx2.HTTPError, TimeoutError) as error:
            raise QuoteUnavailable("Venue request failed") from error
        try:
            return json.loads(payload)
        except (ValueError, UnicodeDecodeError) as error:
            raise InvalidQuote("Venue response was not valid JSON") from error

    async def get_quote(self, market: MarketId) -> Quote:
        spec = self._spec(market)
        pair = f"{spec.base.reference}-{spec.quote.reference}"
        received_at = datetime.now(UTC)
        raw = await self._read_json(f"/products/{pair}/ticker", {})
        try:
            ticker = _Ticker.model_validate(raw)
            bid = Decimal(ticker.bid)
            ask = Decimal(ticker.ask)
            price = Decimal(ticker.price)
        except (ValidationError, InvalidOperation, ValueError) as error:
            raise InvalidQuote("Invalid venue ticker") from error
        if (
            not all(value.is_finite() and value > 0 for value in (bid, ask, price))
            or bid >= ask
        ):
            raise InvalidQuote("Venue ticker prices are invalid")
        observed = ticker.time
        if observed.tzinfo is None:
            raise InvalidQuote("Venue ticker timestamp is not timezone-aware")
        observed = observed.astimezone(UTC)
        if observed > received_at + timedelta(seconds=2):
            raise InvalidQuote("Venue ticker timestamp is in the future")
        return Quote(
            market, self._config.source, bid, ask, observed, observed, received_at
        )

    async def get_ohlcv(
        self,
        market: MarketId,
        interval_minutes: int,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> Sequence[Candle]:
        spec = self._spec(market)
        granularity = interval_minutes * 60
        if granularity not in _GRANULARITIES:
            raise ValueError("Candle interval is not supported by this venue")
        pair = f"{spec.base.reference}-{spec.quote.reference}"
        raw = await self._read_json(
            f"/products/{pair}/candles", {"granularity": str(granularity)}
        )
        received_at = datetime.now(UTC)
        try:
            rows = _Candles.model_validate({"root": raw}, strict=True).root
        except ValidationError as error:
            raise InvalidQuote("Invalid venue candle response") from error
        candles: list[Candle] = []
        step = timedelta(minutes=interval_minutes)
        for row in rows:
            if len(row) != 6:
                raise InvalidQuote("Venue candle row has an unexpected shape")
            opened, low, high, opening, close, volume = row
            try:
                opened_at = datetime.fromtimestamp(opened, UTC)
            except (OverflowError, OSError, ValueError) as error:
                raise InvalidQuote("Venue candle timestamp is invalid") from error
            if opened_at + step > received_at:
                continue  # the venue still labels the live bucket as a candle
            if start is not None and opened_at < start:
                continue
            if end is not None and opened_at >= end:
                continue
            try:
                values = tuple(
                    Decimal(str(value)) for value in (opening, high, low, close, volume)
                )
            except InvalidOperation as error:
                raise InvalidQuote("Invalid venue candle values") from error
            if not all(value.is_finite() for value in values) or values[4] < 0:
                raise InvalidQuote("Non-finite venue candle values")
            candles.append(
                Candle(
                    market,
                    self._config.source,
                    interval_minutes,
                    opened_at,
                    received_at,
                    values[0],
                    values[1],
                    values[2],
                    values[3],
                    values[4],
                    0,
                )
            )
        candles.sort(key=lambda candle: candle.opened_at)
        if len({candle.opened_at for candle in candles}) != len(candles):
            raise InvalidQuote("Venue returned duplicate candles")
        return candles

    async def stream_quotes(self, markets: Sequence[MarketId]) -> AsyncIterator[Quote]:
        raise QuoteUnavailable("This read-only REST adapter does not stream quotes")
        yield  # pragma: no cover - keeps this an async generator

    async def health(self) -> ProviderHealth:
        received_at = datetime.now(UTC)
        raw = await self._read_json("/time", {})
        try:
            observed = _Time.model_validate(raw).iso.astimezone(UTC)
        except (ValidationError, ValueError) as error:
            raise InvalidQuote("Invalid venue time response") from error
        if abs((received_at - observed).total_seconds()) > 30:
            return ProviderHealth(
                self._config.name,
                ProviderStatus.DEGRADED,
                last_success=received_at,
                quota_note="Venue clock drift",
            )
        return ProviderHealth(
            self._config.name, ProviderStatus.HEALTHY, last_success=received_at
        )


def coinbase_provider(client: httpx2.AsyncClient) -> PublicRestProvider:
    """Construct the verified keyless Coinbase Exchange public adapter."""
    return PublicRestProvider(client, COINBASE)
