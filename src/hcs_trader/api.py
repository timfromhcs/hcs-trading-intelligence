"""Read-only status endpoints for the loopback application."""

import json
import logging
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.trustedhost import TrustedHostMiddleware

from hcs_trader.data.kraken import BTC_USD, SOURCE
from hcs_trader.data.quotes import QuoteService
from hcs_trader.domain.market_data import DataQuality
from hcs_trader.storage import AuditStore

_logger = logging.getLogger(__name__)
LOOPBACK_HOST = "127.0.0.1"
LOCAL_PORT = 8765


class Mode(StrEnum):
    OFFLINE = "OFFLINE"
    DATA_ONLY = "DATA_ONLY"
    PAPER = "PAPER"
    LIVE = "LIVE"


class Status(BaseModel):
    mode: Mode = Mode.PAPER
    execution_available: bool = False
    network_enabled: bool = False


class Health(BaseModel):
    status: str


class PublicConfig(BaseModel):
    host: str = LOOPBACK_HOST
    port: int = LOCAL_PORT
    default_mode: Mode = Mode.PAPER


class Capabilities(BaseModel):
    api: bool = True
    market_data: bool = False
    portfolio: bool = False
    paper_orders: bool = False
    live_orders: bool = False
    mcp: bool = False


class QuoteResponse(BaseModel):
    source: str
    base_asset: str
    quote_asset: str
    quality: DataQuality
    provider_issue: str | None
    bid: Decimal | None = None
    ask: Decimal | None = None
    bid_updated_at: datetime | None = None
    ask_updated_at: datetime | None = None
    received_at: datetime | None = None


class QuoteRecord(BaseModel):
    source: str
    base_asset: str
    quote_asset: str
    bid: Decimal
    ask: Decimal
    bid_updated_at: datetime
    ask_updated_at: datetime
    received_at: datetime


def create_app(
    store: AuditStore,
    ui_assets: Path | None = None,
    *,
    quote_service: QuoteService | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        store.initialize()
        if quote_service is not None:
            for quote in store.quote_history(BTC_USD, limit=1):
                quote_service.restore(quote)
        event = store.record_startup()
        # Only allowlisted fields from the local event enter operational logs.
        _logger.info(
            "%s",
            json.dumps(
                {"event": event.kind, "id": event.event_id, "at": event.recorded_at}
            ),
        )
        yield

    app = FastAPI(
        title="HCS Trading Intelligence",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[LOOPBACK_HOST])

    @app.get("/api/v1/health")
    def health() -> Health:
        try:
            store.latest_event()
        except sqlite3.Error as error:
            raise HTTPException(503, "Audit storage unavailable") from error
        return Health(status="ok")

    @app.get("/api/v1/system/status")
    def status() -> Status:
        if quote_service is not None:
            return Status(mode=Mode.DATA_ONLY, network_enabled=True)
        return Status()

    @app.get("/api/v1/system/config")
    def config() -> PublicConfig:
        return PublicConfig()

    @app.get("/api/v1/system/capabilities")
    def capabilities() -> Capabilities:
        return Capabilities(market_data=quote_service is not None)

    @app.get("/api/v1/market/quotes")
    async def market_quote(response: Response) -> QuoteResponse:
        if quote_service is None:
            raise HTTPException(503, "Public market data is disabled")
        response.headers["Cache-Control"] = "no-store"
        try:
            snapshot = await quote_service.snapshot()
        except sqlite3.Error as error:
            raise HTTPException(503, "Quote storage unavailable") from error
        quote = snapshot.quote
        return QuoteResponse(
            source=SOURCE,
            base_asset=str(BTC_USD.base),
            quote_asset=str(BTC_USD.quote),
            quality=snapshot.quality,
            provider_issue=snapshot.provider_issue,
            bid=quote.bid if quote else None,
            ask=quote.ask if quote else None,
            bid_updated_at=quote.bid_updated_at if quote else None,
            ask_updated_at=quote.ask_updated_at if quote else None,
            received_at=quote.received_at if quote else None,
        )

    @app.get("/api/v1/market/quotes/history")
    def quote_history(
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> list[QuoteRecord]:
        try:
            quotes = store.quote_history(BTC_USD, limit=limit)
        except sqlite3.Error as error:
            raise HTTPException(503, "Quote storage unavailable") from error
        return [
            QuoteRecord(
                source=quote.source,
                base_asset=str(quote.market.base),
                quote_asset=str(quote.market.quote),
                bid=quote.bid,
                ask=quote.ask,
                bid_updated_at=quote.bid_updated_at,
                ask_updated_at=quote.ask_updated_at,
                received_at=quote.received_at,
            )
            for quote in quotes
        ]

    if ui_assets is not None and (ui_assets / "index.html").is_file():
        app.mount("/", StaticFiles(directory=ui_assets, html=True), name="ui")

    return app
