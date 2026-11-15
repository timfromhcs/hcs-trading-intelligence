"""Read-only status endpoints for the loopback application."""

import csv
import json
import logging
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from io import StringIO
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi import Path as PathParameter
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from hcs_trader.backtest import BacktestConfig, BacktestResult, run_backtest
from hcs_trader.data import ecb, fred
from hcs_trader.data.kraken import BTC_USD, CANDLE_INTERVALS, SOURCE
from hcs_trader.data.quotes import QuoteService
from hcs_trader.domain.macro import MacroObservation
from hcs_trader.domain.market_data import Candle, DataQuality
from hcs_trader.domain.news import NewsItem
from hcs_trader.indicators import Indicators, calculate_indicators
from hcs_trader.storage import AuditStore
from hcs_trader.walk_forward import WalkForwardConfig, WalkForwardResult, walk_forward

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


class CandleRecord(BaseModel):
    source: str
    base_asset: str
    quote_asset: str
    interval_minutes: int
    opened_at: datetime
    closed_at: datetime
    received_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    trade_count: int


class ReportPayload(BaseModel):
    schema_version: Literal[1] = 1
    report: BacktestResult | WalkForwardResult


class StoredReport(ReportPayload):
    id: str
    recorded_at: datetime


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

    def read_candles(interval: int, limit: int) -> list[Candle]:
        if interval not in CANDLE_INTERVALS:
            raise HTTPException(422, "Unsupported candle interval")
        try:
            return store.candle_history(BTC_USD, SOURCE, interval, limit=limit)
        except sqlite3.Error as error:
            raise HTTPException(503, "Candle storage unavailable") from error

    @app.get("/api/v1/market/candles")
    def candle_history(
        interval: int = 60, limit: Annotated[int, Query(ge=1, le=1000)] = 720
    ) -> list[CandleRecord]:
        return [
            CandleRecord(
                source=candle.source,
                base_asset=str(candle.market.base),
                quote_asset=str(candle.market.quote),
                interval_minutes=candle.interval_minutes,
                opened_at=candle.opened_at,
                closed_at=candle.closed_at,
                received_at=candle.received_at,
                open=candle.open,
                high=candle.high,
                low=candle.low,
                close=candle.close,
                volume=candle.volume,
                trade_count=candle.trade_count,
            )
            for candle in read_candles(interval, limit)
        ]

    @app.get("/api/v1/market/indicators")
    def indicators(interval: int = 60) -> Indicators:
        return calculate_indicators(read_candles(interval, 720))

    def save_report(
        result: BacktestResult | WalkForwardResult, response: Response
    ) -> None:
        payload = ReportPayload(report=result).model_dump_json()
        report_id = sha256(payload.encode("utf-8")).hexdigest()
        try:
            store.record_backtest(report_id, payload)
        except sqlite3.Error as error:
            raise HTTPException(503, "Backtest storage unavailable") from error
        response.headers["Location"] = f"/api/v1/backtests/{report_id}"

    @app.post("/api/v1/backtests")
    def backtest(
        config: BacktestConfig,
        response: Response,
        interval: int = 60,
        limit: Annotated[int, Query(ge=1, le=1000)] = 720,
    ) -> BacktestResult:
        try:
            result = run_backtest(read_candles(interval, limit), config)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        save_report(result, response)
        return result

    @app.post("/api/v1/backtests/walk-forward")
    def walk_forward_backtest(
        config: WalkForwardConfig,
        response: Response,
        interval: int = 60,
        limit: Annotated[int, Query(ge=1, le=1000)] = 720,
    ) -> WalkForwardResult:
        try:
            result = walk_forward(read_candles(interval, limit), config)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        save_report(result, response)
        return result

    @app.get("/api/v1/backtests/{report_id}")
    def get_report(
        report_id: Annotated[str, PathParameter(pattern=r"^[a-f0-9]{64}$")],
    ) -> StoredReport:
        try:
            record = store.backtest_report(report_id)
        except (sqlite3.Error, ValueError) as error:
            raise HTTPException(503, "Backtest storage unavailable") from error
        if record is None:
            raise HTTPException(404, "Backtest report not found")
        if sha256(record.payload.encode("utf-8")).hexdigest() != report_id:
            raise HTTPException(503, "Backtest report integrity check failed")
        try:
            payload = ReportPayload.model_validate_json(record.payload)
        except ValidationError as error:
            raise HTTPException(503, "Backtest report format is unsupported") from error
        return StoredReport(
            id=report_id, recorded_at=record.recorded_at, report=payload.report
        )

    @app.get(
        "/api/v1/market/candles.csv",
        response_class=Response,
        responses={200: {"content": {"text/csv": {"schema": {"type": "string"}}}}},
    )
    def candle_csv(
        interval: int = 60, limit: Annotated[int, Query(ge=1, le=1000)] = 720
    ) -> Response:
        candles = candle_history(interval, limit)
        output = StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=list(CandleRecord.model_fields))
        writer.writeheader()
        writer.writerows(candle.model_dump(mode="json") for candle in candles)
        filename = f"btc-usd-{interval}m.csv"
        return Response(
            output.getvalue(),
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/api/v1/news/events")
    def news_events(limit: Annotated[int, Query(ge=1, le=100)] = 25) -> list[NewsItem]:
        try:
            return store.news(limit=limit)
        except sqlite3.Error as error:
            raise HTTPException(503, "News storage unavailable") from error

    @app.get("/api/v1/macro/series")
    def macro_series(
        limit: Annotated[int, Query(ge=1, le=1000)] = 250,
        source: Literal["ecb", "fred"] = "ecb",
    ) -> list[MacroObservation]:
        provider = ecb if source == "ecb" else fred
        try:
            return store.macro_history(provider.SOURCE, provider.SERIES, limit=limit)
        except sqlite3.Error as error:
            raise HTTPException(503, "Macro storage unavailable") from error

    if ui_assets is not None and (ui_assets / "index.html").is_file():
        app.mount("/", StaticFiles(directory=ui_assets, html=True), name="ui")

    return app
