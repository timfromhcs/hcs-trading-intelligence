"""Local simulation using verified public quotes and the paper ledger."""

import asyncio

from hcs_trader.data.kraken import BTC_USD
from hcs_trader.data.quotes import QuoteService
from hcs_trader.domain.market_data import DataQuality
from hcs_trader.domain.paper import (
    PaperFill,
    PaperOrder,
    PaperRejected,
    simulated_terms,
)
from hcs_trader.storage import AuditStore


class PaperExecution:
    def __init__(self, store: AuditStore, quotes: QuoteService) -> None:
        self._store = store
        self._quotes = quotes

    async def submit(self, order: PaperOrder) -> PaperFill:
        existing = await asyncio.to_thread(self._store.existing_paper_fill, order)
        if existing is not None:
            return existing

        snapshot = await self._quotes.snapshot()
        quote = snapshot.quote
        if (
            snapshot.quality is not DataQuality.FRESH
            or quote is None
            or quote.market != BTC_USD
        ):
            raise PaperRejected("Paper execution requires a fresh BTC/USD quote")
        price, fee = simulated_terms(order, quote)
        return await asyncio.to_thread(
            self._store.record_paper_fill, order, price, fee, quote=quote
        )
