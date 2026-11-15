"""Market data provider contract and normalized failures."""

from typing import Protocol

from hcs_trader.domain.assets import MarketId
from hcs_trader.domain.market_data import Quote


class QuoteUnavailable(Exception):
    """The provider could not supply the requested quote."""


class QuoteRateLimited(QuoteUnavailable):
    """The provider requested a pause in polling."""


class InvalidQuote(QuoteUnavailable):
    """The provider response did not meet the quote contract."""


class QuoteProvider(Protocol):
    async def get_quote(self, market: MarketId) -> Quote: ...
