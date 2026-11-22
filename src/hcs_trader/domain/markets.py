"""Static market registry: venue symbols, precision, minimum sizes and status.

The registry is data, not policy. It answers "what is this market and what may be
done with it". Execution paths additionally require ``MarketStatus.LIVE_ALLOWED``
and the deterministic risk policy; a registry entry never authorises anything on
its own. Values were read from the public Kraken ``AssetPairs`` endpoint on
2026-11-22 and must be re-verified before they are relied on for new venues.
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from hcs_trader.domain.assets import AssetId, MarketId


class MarketStatus(StrEnum):
    READ_ONLY = "read_only"
    PAPER = "paper"
    LIVE_ALLOWED = "live_allowed"


class UnknownMarket(ValueError):
    """The market symbol or identifier is not in the registry."""


class OrderShapeError(ValueError):
    """Quantity or price violates registry precision or minimum size."""


@dataclass(frozen=True, slots=True)
class MarketSpec:
    symbol: str
    venue: str
    base: AssetId
    quote: AssetId
    venue_pair: str
    response_keys: frozenset[str]
    price_decimals: int
    quantity_decimals: int
    min_quantity: Decimal
    min_notional: Decimal
    status: MarketStatus
    note: str = ""

    @property
    def market_id(self) -> MarketId:
        return MarketId(self.venue, self.base, self.quote)

    @property
    def allows_paper(self) -> bool:
        return self.status in (MarketStatus.PAPER, MarketStatus.LIVE_ALLOWED)

    @property
    def allows_live(self) -> bool:
        return self.status is MarketStatus.LIVE_ALLOWED


@dataclass(frozen=True, slots=True)
class AssetSpec:
    asset_id: AssetId
    symbol: str
    status: MarketStatus
    note: str = ""


def _decimal_places(value: Decimal) -> int:
    """Significant decimal places, computed without rounding or context limits."""
    _sign, digits, exponent = value.as_tuple()
    if not isinstance(exponent, int):
        raise OrderShapeError("Amount must be finite")
    digit_list = list(digits)
    while digit_list and digit_list[-1] == 0 and exponent < 0:
        digit_list.pop()
        exponent += 1
    return max(0, -exponent)


def validate_order_shape(
    spec: MarketSpec, quantity: Decimal, price: Decimal | None = None
) -> None:
    """Reject non-finite, over-precise or below-minimum amounts. Never rounds."""
    if not isinstance(quantity, Decimal) or not quantity.is_finite() or quantity <= 0:
        raise OrderShapeError("Quantity must be a positive finite Decimal")
    if _decimal_places(quantity) > spec.quantity_decimals:
        raise OrderShapeError(
            f"Quantity exceeds {spec.quantity_decimals} decimals for {spec.symbol}"
        )
    if quantity < spec.min_quantity:
        raise OrderShapeError(
            f"Quantity is below the {spec.min_quantity} minimum for {spec.symbol}"
        )
    if price is None:
        return
    if not isinstance(price, Decimal) or not price.is_finite() or price <= 0:
        raise OrderShapeError("Price must be a positive finite Decimal")
    if _decimal_places(price) > spec.price_decimals:
        raise OrderShapeError(
            f"Price exceeds {spec.price_decimals} decimals for {spec.symbol}"
        )
    if quantity * price < spec.min_notional:
        raise OrderShapeError(
            f"Notional is below the {spec.min_notional} minimum for {spec.symbol}"
        )


class MarketRegistry:
    def __init__(
        self, markets: tuple[MarketSpec, ...], assets: tuple[AssetSpec, ...]
    ) -> None:
        symbols = [m.symbol for m in markets]
        if len(set(symbols)) != len(symbols):
            raise ValueError("Duplicate market symbol")
        self._markets = {m.symbol: m for m in markets}
        self._by_id = {m.market_id: m for m in markets}
        if len(self._by_id) != len(markets):
            raise ValueError("Duplicate market identifier")
        self._assets = assets

    @property
    def default(self) -> MarketSpec:
        return self._markets[DEFAULT_MARKET_SYMBOL]

    def markets(self) -> tuple[MarketSpec, ...]:
        return tuple(self._markets.values())

    def assets(self) -> tuple[AssetSpec, ...]:
        return self._assets

    def resolve(self, symbol: str | None) -> MarketSpec:
        """Return the spec for a ``BASE/QUOTE`` symbol; None selects the default."""
        if symbol is None:
            return self.default
        try:
            return self._markets[symbol]
        except KeyError:
            raise UnknownMarket("Market is not in the registry") from None

    def by_market_id(self, market: MarketId) -> MarketSpec:
        try:
            return self._by_id[market]
        except KeyError:
            raise UnknownMarket("Market is not in the registry") from None

    def quote_symbols(self, base_symbol: str) -> tuple[str, ...]:
        return tuple(
            m.symbol.split("/")[1]
            for m in self._markets.values()
            if m.symbol.split("/")[0] == base_symbol
        )

    def is_live_allowed(self, symbol: str) -> bool:
        spec = self._markets.get(symbol)
        return spec is not None and spec.allows_live

    def live_allowed_symbols(self) -> frozenset[str]:
        return frozenset(m.symbol for m in self._markets.values() if m.allows_live)


_USD = AssetId.parse("fiat:iso4217/currency:USD")
_EUR = AssetId.parse("fiat:iso4217/currency:EUR")
_BTC = AssetId.parse("bitcoin:mainnet/native:BTC")
_ETH = AssetId.parse("ethereum:1/native:ETH")
_SOL = AssetId.parse("solana:mainnet-beta/native:SOL")
_LTC = AssetId.parse("litecoin:mainnet/native:LTC")
_DGB = AssetId.parse("digibyte:mainnet/native:DGB")

_READ_ONLY_NOTE = (
    "Read-only: quotes and candles only. Paper execution and live submission "
    "support BTC/USD only."
)


def _kraken(
    base_symbol: str,
    base: AssetId,
    quote_symbol: str,
    quote: AssetId,
    pair: str,
    response_key: str,
    price_decimals: int,
    min_quantity: str,
    min_notional: str,
    status: MarketStatus = MarketStatus.READ_ONLY,
    note: str = _READ_ONLY_NOTE,
) -> MarketSpec:
    # Kraken answers with names that differ from the request pair (for example
    # XXBTZUSD for XBTUSD); newer pairs echo the request name, in which case
    # response_key equals pair. Only the registered response key is accepted.
    return MarketSpec(
        symbol=f"{base_symbol}/{quote_symbol}",
        venue="kraken",
        base=base,
        quote=quote,
        venue_pair=pair,
        response_keys=frozenset({response_key}),
        price_decimals=price_decimals,
        quantity_decimals=8,
        min_quantity=Decimal(min_quantity),
        min_notional=Decimal(min_notional),
        status=status,
        note=note,
    )


DEFAULT_MARKET_SYMBOL = "BTC/USD"

_LIVE_NOTE = (
    "Only market eligible for the Kraken spot canary; live mode is off by default "
    "and still requires the risk policy, local authorisation and a fresh quote."
)

MARKET_REGISTRY = MarketRegistry(
    markets=(
        _kraken(
            "BTC",
            _BTC,
            "USD",
            _USD,
            "XBTUSD",
            "XXBTZUSD",
            1,
            "0.00006",
            "0.5",
            MarketStatus.LIVE_ALLOWED,
            _LIVE_NOTE,
        ),
        _kraken("BTC", _BTC, "EUR", _EUR, "XBTEUR", "XXBTZEUR", 1, "0.00006", "0.45"),
        _kraken("ETH", _ETH, "USD", _USD, "ETHUSD", "XETHZUSD", 2, "0.002", "0.5"),
        _kraken("ETH", _ETH, "EUR", _EUR, "ETHEUR", "XETHZEUR", 2, "0.002", "0.45"),
        _kraken("SOL", _SOL, "USD", _USD, "SOLUSD", "SOLUSD", 2, "0.05", "0.5"),
        _kraken("SOL", _SOL, "EUR", _EUR, "SOLEUR", "SOLEUR", 2, "0.05", "0.45"),
        _kraken("LTC", _LTC, "USD", _USD, "LTCUSD", "XLTCZUSD", 2, "0.08", "0.5"),
        _kraken("LTC", _LTC, "EUR", _EUR, "LTCEUR", "XLTCZEUR", 2, "0.08", "0.45"),
    ),
    assets=(
        AssetSpec(_BTC, "BTC", MarketStatus.LIVE_ALLOWED, "BTC/USD canary only"),
        AssetSpec(_ETH, "ETH", MarketStatus.READ_ONLY),
        AssetSpec(_SOL, "SOL", MarketStatus.READ_ONLY),
        AssetSpec(_LTC, "LTC", MarketStatus.READ_ONLY),
        AssetSpec(
            _DGB,
            "DGB",
            MarketStatus.READ_ONLY,
            "No Kraken pair. Low-liquidity asset; an aggregator source is planned "
            "for Wave 2 and is not available yet.",
        ),
    ),
)

DEFAULT_MARKET_ID = MARKET_REGISTRY.default.market_id
