"""Read-only status endpoints for the loopback application."""

import asyncio
import csv
import ipaddress
import json
import logging
import os
import re
import secrets
import sqlite3
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from io import StringIO
from pathlib import Path
from typing import Annotated, Any, Literal, cast
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi import Path as PathParameter
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from hcs_trader.application import (
    ApplicationError,
    ApplicationService,
    ChainCapability,
    Forbidden,
    InvalidRequest,
    NotFound,
    Rejected,
)
from hcs_trader.application.service import (
    AssetInfo,
    MarketInfo,
    OrderBookSnapshot,
    OrderPreview,
    Position,
    StrategyMetrics,
    Unavailable,
    WalletBalanceView,
    WalletTransactions,
    spot_order_dict,
)
from hcs_trader.backtest import BacktestConfig, BacktestResult
from hcs_trader.data import ecb, fred
from hcs_trader.data.kraken import SOURCE
from hcs_trader.data.provider import QuoteUnavailable
from hcs_trader.data.quotes import QuoteService
from hcs_trader.data.runtime import IngestionService
from hcs_trader.data.source_registry import load_source_registry
from hcs_trader.data.source_status import DataSourcesReport
from hcs_trader.domain.execution import SpotOrder, SpotOrderType, SpotSide
from hcs_trader.domain.lifecycle import OrderState
from hcs_trader.domain.macro import MacroObservation
from hcs_trader.domain.market_data import Candle, DataQuality
from hcs_trader.domain.markets import DEFAULT_MARKET_ID
from hcs_trader.domain.news import NewsHit, NewsItem
from hcs_trader.domain.paper import (
    PaperBalance,
    PaperFill,
    PaperOrder,
    PaperRejected,
    PaperSide,
)
from hcs_trader.domain.wallet import WalletSnapshot
from hcs_trader.indicators import Indicators
from hcs_trader.kraken_spot import KrakenAmbiguousExecutionError, KrakenOrderRejected
from hcs_trader.local_research import ResearchHistory
from hcs_trader.logging_config import correlation_id
from hcs_trader.loopback import HostPolicy, fetch_site_allowed
from hcs_trader.network_policy import NetworkPolicy, SystemMode
from hcs_trader.paper_execution import PaperExecution
from hcs_trader.promotion import (
    CandidateNotFound,
    PromotionRejected,
    PromotionStage,
    ReportPayload,
    StrategyCandidate,
    execute_shadow_run,
    get_candidate_backtest_report,
    get_latest_shadow_run,
    list_shadow_runs_for_candidate,
    promote_strategy_candidate,
)
from hcs_trader.risk import RiskRejected
from hcs_trader.skills import list_skills
from hcs_trader.spot_execution import KrakenSpotExecution
from hcs_trader.storage import AuditStore, ExecutionPause
from hcs_trader.walk_forward import WalkForwardConfig, WalkForwardResult
from hcs_trader.webfetch import SafeFetcher, SafeFetchError, SourceDisabled

_logger = logging.getLogger(__name__)
LOOPBACK_HOST = "127.0.0.1"
LOCAL_PORT = 8765


Mode = SystemMode

API_TOKEN_ENV = "HCS_API_TOKEN"  # noqa: S105 - env var name
MAX_BODY_BYTES = 1_048_576
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (
        b"content-security-policy",
        b"default-src 'self'; script-src 'self'; style-src 'self'; "
        b"img-src 'self' data:; font-src 'self'; connect-src 'self'; "
        b"object-src 'none'; base-uri 'none'; form-action 'self'; "
        b"frame-ancestors 'none'",
    ),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-frame-options", b"DENY"),
)


def generate_api_token() -> str:
    """Random per-run token; the operator shows or stores it locally."""
    return secrets.token_urlsafe(32)


class _BodyTooLarge(Exception):
    pass


class _Hardening:
    """ASGI middleware: loopback peer, correlation ID, token, origin, size, rate."""

    def __init__(
        self,
        app: Any,
        *,
        token: str | None,
        max_body: int,
        rate_limit: int,
        window: float,
        hosts: HostPolicy | None = None,
    ) -> None:
        self.app = app
        self.hosts = hosts or HostPolicy()
        self.token = token
        self.max_body = max_body
        self.rate_limit = rate_limit
        self.window = window
        self._hits: dict[str, deque[float]] = {}

    @staticmethod
    async def _reject(
        send: Any,
        status: int,
        detail: str,
        rid: str,
        extra: list[tuple[bytes, bytes]] | None = None,
    ) -> None:
        body = json.dumps({"detail": detail}).encode()
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            (b"x-request-id", rid.encode()),
            *SECURITY_HEADERS,
            *(extra or []),
        ]
        await send(
            {"type": "http.response.start", "status": status, "headers": headers}
        )
        await send({"type": "http.response.body", "body": body})

    def _limited(self, key: str) -> bool:
        now = time.monotonic()
        hits = self._hits.setdefault(key, deque())
        while hits and now - hits[0] > self.window:
            hits.popleft()
        if len(hits) >= self.rate_limit:
            return True
        hits.append(now)
        return False

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in scope["headers"]
        }
        supplied = headers.get("x-request-id", "")
        rid = supplied if _REQUEST_ID.match(supplied) else uuid4().hex
        reset = correlation_id.set(rid)
        try:
            await self._handle(scope, receive, send, headers, rid)
        finally:
            correlation_id.reset(reset)

    async def _handle(
        self, scope: Any, receive: Any, send: Any, headers: dict[str, str], rid: str
    ) -> None:
        client = scope.get("client")
        if client:
            try:
                if not ipaddress.ip_address(client[0]).is_loopback:
                    await self._reject(send, 403, "Loopback clients only", rid)
                    return
            except ValueError:
                pass  # non-IP peer (in-process test transport)
        if not self.hosts.host_allowed(headers.get("host")):
            await self._reject(send, 400, "Invalid host header", rid)
            return
        mutating = scope["method"] not in SAFE_METHODS
        if mutating:
            origin = headers.get("origin")
            if not fetch_site_allowed(headers.get("sec-fetch-site")) or (
                origin is not None and not self.hosts.origin_allowed(origin)
            ):
                await self._reject(send, 403, "Local origin required", rid)
                return
            if self.token is not None:
                presented = headers.get("x-hcs-token", "")
                auth = headers.get("authorization", "")
                if auth.lower().startswith("bearer "):
                    presented = auth[7:].strip()
                if not secrets.compare_digest(presented.encode(), self.token.encode()):
                    await self._reject(
                        send,
                        401,
                        "API token required",
                        rid,
                        [(b"www-authenticate", b"Bearer")],
                    )
                    return
            declared = headers.get("content-length")
            if declared is not None and (
                not declared.isdigit() or int(declared) > self.max_body
            ):
                await self._reject(send, 413, "Request body too large", rid)
                return
            has_body = declared not in (None, "0") or "transfer-encoding" in headers
            ctype = headers.get("content-type", "").split(";")[0].strip().lower()
            if has_body and ctype != "application/json":
                await self._reject(send, 415, "application/json required", rid)
                return
            key = client[0] if client else "local"
            if self._limited(key):
                await self._reject(
                    send, 429, "Rate limit exceeded", rid, [(b"retry-after", b"60")]
                )
                return

        received = 0

        async def limited_receive() -> Any:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body:
                    raise _BodyTooLarge
            return message

        async def tagged_send(message: Any) -> None:
            if message["type"] == "http.response.start":
                present = {k.lower() for k, _ in message["headers"]}
                message["headers"] = [
                    *message["headers"],
                    (b"x-request-id", rid.encode()),
                    *(h for h in SECURITY_HEADERS if h[0] not in present),
                ]
            await send(message)

        try:
            await self.app(scope, limited_receive if mutating else receive, tagged_send)
        except _BodyTooLarge:
            await self._reject(send, 413, "Request body too large", rid)


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
    portfolio: bool = True
    paper_orders: bool = False
    live_orders: bool = False
    mcp: bool = True
    wallet_snapshots: bool = True


class QuoteResponse(BaseModel):
    market: str | None = None
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


class DataSourcesResponse(DataSourcesReport):
    ingestion_enabled: bool
    ingestion_mode: SystemMode | None = None


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


class StoredReport(ReportPayload):
    id: str
    recorded_at: datetime


class AuditEventResponse(BaseModel):
    event_id: str
    recorded_at: str
    kind: str


class SkillResponse(BaseModel):
    name: str
    version: str
    purpose: str
    capability_tier: str
    required_tools: list[str]


class CreateCandidateRequest(BaseModel):
    name: str
    version: str
    parameters: dict[str, Any] = {}
    dataset_hash: str


class PromoteCandidateRequest(BaseModel):
    target_stage: PromotionStage


class ShadowRunResponse(BaseModel):
    run_id: str
    report: BacktestResult


class PaperPortfolio(BaseModel):
    mode: Literal["PAPER"] = "PAPER"
    cash_currency: Literal["USD"] = "USD"
    position_currency: Literal["BTC"] = "BTC"
    balance: PaperBalance


class PaperAccount(PaperPortfolio):
    fills: list[PaperFill]


class OrderPreviewRequest(BaseModel):
    side: PaperSide
    quantity: Decimal
    idempotency_key: str | None = None


class SystemCapabilities(Capabilities):
    chains: list[ChainCapability]


def _http_error(error: ApplicationError) -> HTTPException:
    if isinstance(error, InvalidRequest):
        return HTTPException(422, str(error))
    if isinstance(error, NotFound):
        return HTTPException(404, str(error))
    if isinstance(error, Forbidden):
        return HTTPException(403, str(error))
    if isinstance(error, Rejected):
        return HTTPException(409, str(error))
    return HTTPException(503, str(error))


class SpotOrderSubmission(BaseModel):
    side: SpotSide
    quantity: Decimal
    limit_price: Decimal
    candidate_id: str
    strategy_version: str
    client_order_id: str | None = None


def create_app(
    store: AuditStore,
    ui_assets: Path | None = None,
    *,
    quote_service: QuoteService | None = None,
    paper_mode: bool = False,
    kraken_reconciliation: Callable[[], Awaitable[object]] | None = None,
    spot_execution: KrakenSpotExecution | None = None,
    api_token: str | None = None,
    network_policy: NetworkPolicy | None = None,
    ingestion: IngestionService | None = None,
    max_body_bytes: int = MAX_BODY_BYTES,
    write_rate_limit: int = 60,
    rate_window_seconds: float = 60.0,
    bound_port: int | None = None,
    extra_allowed_hosts: tuple[str, ...] = (),
    webfetch: SafeFetcher | None = None,
) -> FastAPI:
    token = api_token if api_token is not None else os.environ.get(API_TOKEN_ENV)
    if token is not None and len(token) < 16:
        raise ValueError("API token must be at least 16 characters")
    if (
        network_policy is not None
        and network_policy.mode is SystemMode.LIVE
        and spot_execution is None
    ):
        raise ValueError("LIVE mode requires configured spot execution")
    paper = (
        PaperExecution(store, quote_service) if paper_mode and quote_service else None
    )

    service = ApplicationService(store, paper=paper)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        store.initialize()
        if quote_service is not None:
            for quote in store.quote_history(DEFAULT_MARKET_ID, limit=1):
                quote_service.restore(quote)
        if quote_service is not None:
            try:
                quote_service.restore_state(store.load_ingest_state("kraken"))
            except sqlite3.Error:
                pass  # status persistence is best effort; quotes still refresh
        event = store.record_startup()
        # Only allowlisted fields from the local event enter operational logs.
        _logger.info(
            "%s",
            json.dumps(
                {"event": event.kind, "id": event.event_id, "at": event.recorded_at}
            ),
        )
        if kraken_reconciliation is not None:
            try:
                async with asyncio.timeout(30):
                    await kraken_reconciliation()
                incomplete = any(
                    order.state
                    in {OrderState.SUBMITTED, OrderState.RECONCILIATION_REQUIRED}
                    for order in store.list_open_spot_orders("kraken")
                )
            except TimeoutError:
                incomplete = True
            store.record_audit_event(
                "spot.recovery.incomplete" if incomplete else "spot.recovery.completed"
            )
        if ingestion is not None:
            ingestion.start()
        try:
            yield
        finally:
            if ingestion is not None:
                await ingestion.stop()
            if webfetch is not None:
                await webfetch.aclose()

    app = FastAPI(
        title="HCS Trading Intelligence",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.add_middleware(
        _Hardening,
        hosts=HostPolicy(bound_port, extra_allowed_hosts),
        token=token,
        max_body=max_body_bytes,
        rate_limit=write_rate_limit,
        window=rate_window_seconds,
    )

    @app.get("/api/v1/health")
    def health() -> Health:
        try:
            store.latest_event()
        except sqlite3.Error as error:
            raise HTTPException(503, "Audit storage unavailable") from error
        return Health(status="ok")

    @app.get("/api/v1/system/status")
    def status() -> Status:
        if network_policy is not None:
            return Status(
                mode=network_policy.mode,
                network_enabled=network_policy.mode is not SystemMode.OFFLINE,
                execution_available=(
                    network_policy.mode in {SystemMode.PAPER, SystemMode.LIVE}
                    and not execution_pause().paused
                ),
            )
        if spot_execution is not None:
            return Status(
                mode=Mode.LIVE,
                network_enabled=True,
                execution_available=not execution_pause().paused,
            )
        if quote_service is not None:
            return Status(
                mode=Mode.PAPER if paper is not None else Mode.DATA_ONLY,
                network_enabled=True,
                execution_available=paper is not None and not execution_pause().paused,
            )
        if kraken_reconciliation is not None:
            return Status(mode=Mode.DATA_ONLY, network_enabled=True)
        return Status()

    def _verify_local_origin(request: Request, action_name: str) -> None:
        origin = request.headers.get("origin")
        if request.headers.get("sec-fetch-site") == "cross-site" or (
            origin is not None and origin != f"http://{LOOPBACK_HOST}:{LOCAL_PORT}"
        ):
            raise HTTPException(403, f"{action_name} require a local origin")

    @app.get("/api/v1/risk/status")
    def execution_pause() -> ExecutionPause:
        try:
            return store.execution_pause()
        except (sqlite3.Error, ValueError) as error:
            raise HTTPException(503, "Execution pause state unavailable") from error

    @app.post("/api/v1/risk/pause")
    @app.post("/api/v1/system/pause")
    def trigger_pause(request: Request) -> ExecutionPause:
        _verify_local_origin(request, "Risk controls")
        try:
            return service.pause_execution()
        except ApplicationError as error:
            raise _http_error(error) from error

    @app.post("/api/v1/risk/resume")
    @app.post("/api/v1/system/resume")
    def clear_pause(request: Request) -> ExecutionPause:
        _verify_local_origin(request, "Risk controls")
        try:
            return service.resume_execution()
        except ApplicationError as error:
            raise _http_error(error) from error

    @app.get("/api/v1/risk/policy")
    def risk_policy() -> dict[str, Any]:
        try:
            return service.risk_policy_summary()
        except ApplicationError as error:
            raise _http_error(error) from error

    @app.get("/api/v1/system/config")
    def config() -> PublicConfig:
        return PublicConfig()

    @app.get("/api/v1/system/capabilities")
    def capabilities() -> SystemCapabilities:
        return SystemCapabilities(
            market_data=quote_service is not None,
            paper_orders=paper is not None,
            live_orders=spot_execution is not None,
            chains=service.chain_capabilities(),
        )

    @app.get("/api/v1/system/chains")
    def chain_capabilities() -> list[ChainCapability]:
        return service.chain_capabilities()

    @app.get("/api/v1/data/sources")
    def data_sources(response: Response) -> DataSourcesResponse:
        response.headers["Cache-Control"] = "no-store"
        try:
            report = service.data_sources()
        except Unavailable as error:
            raise HTTPException(503, "Source status storage unavailable") from error
        return DataSourcesResponse(
            generated_at=report.generated_at,
            sources=report.sources,
            ingestion_enabled=ingestion is not None,
            ingestion_mode=ingestion.network_policy.mode if ingestion else None,
        )

    @app.get("/api/v1/webfetch/sources")
    def registered_web_sources(response: Response) -> list[dict[str, Any]]:
        """List registered external sources with their verification state."""
        response.headers["Cache-Control"] = "no-store"
        registry = load_source_registry()
        return [
            {
                "id": spec.id,
                "url": spec.url,
                "format": spec.format,
                "language": spec.language,
                "api_key_required": spec.api_key_required,
                "enabled": spec.usable,
                "terms_url": spec.terms_url,
                "attribution": spec.attribution,
                "requests_per_minute": spec.requests_per_minute,
                "verification_result": spec.verification.result,
            }
            for spec in registry.all()
        ]

    @app.get("/api/v1/webfetch/{source_id}")
    async def fetch_registered_source(
        source_id: Annotated[str, PathParameter(min_length=1, max_length=64)],
        response: Response,
    ) -> dict[str, Any]:
        """Fetch a registered source; URLs and paths are never caller-supplied."""
        response.headers["Cache-Control"] = "no-store"
        if webfetch is None:
            raise HTTPException(503, "Web fetch is not configured")
        try:
            document = await webfetch.fetch(source_id)
        except SourceDisabled as error:
            raise HTTPException(403, str(error)) from error
        except SafeFetchError as error:
            raise HTTPException(422, str(error)) from error
        # Only metadata and marked text leave the fetch boundary; raw HTML is
        # never returned to a client or model.
        return {
            **webfetch.document_envelope(document),
            "url": document.url,
            "status_code": document.status_code,
            "content_type": document.content_type,
            "fetched_at": document.fetched_at.isoformat(),
            "from_cache": document.from_cache,
        }

    @app.get("/api/v1/market/quotes")
    async def market_quote(
        response: Response,
        market: Annotated[str | None, Query(max_length=16)] = None,
    ) -> QuoteResponse:
        response.headers["Cache-Control"] = "no-store"
        try:
            spec = service.resolve_market(market)
        except ApplicationError as error:
            raise _http_error(error) from error
        if quote_service is None:
            raise HTTPException(503, "Public market data is disabled")
        if spec.market_id != quote_service.market:
            # Only the configured market has a live refresh; others are archive reads.
            try:
                archived = service.latest_quote(spec.symbol)
            except ApplicationError as error:
                raise _http_error(error) from error
            return QuoteResponse(
                market=spec.symbol,
                source=archived.source if archived else SOURCE,
                base_asset=str(spec.base),
                quote_asset=str(spec.quote),
                quality=(
                    service.quote_quality(archived) if archived else DataQuality.MISSING
                ),
                provider_issue="archived_only",
                bid=archived.bid if archived else None,
                ask=archived.ask if archived else None,
                bid_updated_at=archived.bid_updated_at if archived else None,
                ask_updated_at=archived.ask_updated_at if archived else None,
                received_at=archived.received_at if archived else None,
            )
        try:
            snapshot = await quote_service.snapshot()
        except sqlite3.Error as error:
            raise HTTPException(503, "Quote storage unavailable") from error
        quote = snapshot.quote
        return QuoteResponse(
            market=spec.symbol,
            source=SOURCE,
            base_asset=str(spec.base),
            quote_asset=str(spec.quote),
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
        market: Annotated[str | None, Query(max_length=16)] = None,
    ) -> list[QuoteRecord]:
        try:
            quotes = service.quote_history(limit, market)
        except ApplicationError as error:
            raise _http_error(error) from error
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

    def read_candles(
        interval: int, limit: int, market: str | None = None
    ) -> list[Candle]:
        try:
            return service.candles(interval, limit, market)
        except ApplicationError as error:
            raise _http_error(error) from error

    @app.get("/api/v1/market/candles")
    def candle_history(
        interval: int = 60,
        limit: Annotated[int, Query(ge=1, le=1000)] = 720,
        market: Annotated[str | None, Query(max_length=16)] = None,
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
            for candle in read_candles(interval, limit, market)
        ]

    @app.get("/api/v1/market/indicators")
    def indicators(
        interval: int = 60,
        market: Annotated[str | None, Query(max_length=16)] = None,
    ) -> Indicators:
        try:
            return service.indicators(interval, market)
        except ApplicationError as error:
            raise _http_error(error) from error

    @app.post("/api/v1/backtests")
    def backtest(
        config: BacktestConfig,
        response: Response,
        interval: int = 60,
        limit: Annotated[int, Query(ge=1, le=1000)] = 720,
        holdout_candles: Annotated[int, Query(ge=0, le=999)] = 0,
    ) -> BacktestResult:
        try:
            run = service.run_backtest(
                config,
                interval=interval,
                limit=limit,
                holdout_candles=holdout_candles,
            )
        except ApplicationError as error:
            raise _http_error(error) from error
        response.headers["Location"] = f"/api/v1/backtests/{run.report_id}"
        return cast(BacktestResult, run.result)

    @app.post("/api/v1/backtests/walk-forward")
    def walk_forward_backtest(
        config: WalkForwardConfig,
        response: Response,
        interval: int = 60,
        limit: Annotated[int, Query(ge=1, le=1000)] = 720,
    ) -> WalkForwardResult:
        try:
            run = service.run_walk_forward(config, interval=interval, limit=limit)
        except ApplicationError as error:
            raise _http_error(error) from error
        response.headers["Location"] = f"/api/v1/backtests/{run.report_id}"
        return cast(WalkForwardResult, run.result)

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

    @app.get("/api/v1/portfolio/summary")
    def portfolio_summary() -> PaperPortfolio:
        try:
            return PaperPortfolio(balance=store.paper_balance())
        except (sqlite3.Error, ValidationError) as error:
            raise HTTPException(503, "Paper ledger unavailable") from error

    @app.get("/api/v1/paper/fills")
    def paper_fills(
        limit: Annotated[int, Query(ge=1, le=100)] = 25,
    ) -> list[PaperFill]:
        try:
            return store.paper_fills(limit=limit)
        except (sqlite3.Error, ValidationError) as error:
            raise HTTPException(503, "Paper ledger unavailable") from error

    @app.get("/api/v1/paper/account")
    def paper_account() -> PaperAccount:
        fills = paper_fills(limit=10)
        balance = fills[0].balance if fills else PaperBalance()
        return PaperAccount(balance=balance, fills=fills)

    @app.post("/api/v1/orders/paper")
    async def paper_order(order: PaperOrder, request: Request) -> PaperFill:
        if paper is None:
            raise HTTPException(403, "Paper execution is disabled")
        _verify_local_origin(request, "Paper orders")
        try:
            return await service.place_paper_order(order)
        except ApplicationError as error:
            raise _http_error(error) from error

    @app.get("/api/v1/wallets")
    def wallet_snapshots(
        response: Response, limit: Annotated[int, Query(ge=1, le=100)] = 25
    ) -> list[WalletSnapshot]:
        response.headers["Cache-Control"] = "no-store"
        try:
            return store.wallet_snapshots(limit=limit)
        except (sqlite3.Error, ValidationError) as error:
            raise HTTPException(503, "Wallet snapshot storage unavailable") from error

    @app.get("/api/v1/orders/spot")
    def list_spot_orders(
        response: Response,
        venue: Annotated[str, Query(min_length=1, max_length=32)] = "kraken",
    ) -> list[dict[str, Any]]:
        response.headers["Cache-Control"] = "no-store"
        try:
            orders = store.list_open_spot_orders(venue)
            return [spot_order_dict(o) for o in orders]
        except (sqlite3.Error, ValidationError) as error:
            raise HTTPException(503, "Spot order storage unavailable") from error

    @app.get("/api/v1/orders/spot/{client_order_id}")
    def get_spot_order(client_order_id: str, response: Response) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        try:
            o = store.get_spot_order(client_order_id)
            if o is None:
                raise HTTPException(404, f"Spot order '{client_order_id}' not found")
            return spot_order_dict(o)
        except HTTPException:
            raise
        except (sqlite3.Error, ValidationError) as error:
            raise HTTPException(503, "Spot order storage unavailable") from error

    @app.post("/api/v1/orders/spot")
    @app.post("/api/v1/orders/live")
    async def submit_spot_order_endpoint(
        submission: SpotOrderSubmission, request: Request
    ) -> dict[str, Any]:
        if spot_execution is None:
            raise HTTPException(403, "Live spot execution is disabled")
        _verify_local_origin(request, "Spot orders")
        if quote_service is None:
            raise HTTPException(
                503, "Market quote service unavailable for spot execution"
            )
        try:
            snap = await quote_service.snapshot()
        except (QuoteUnavailable, sqlite3.Error) as error:
            raise HTTPException(
                503, "Market quote unavailable for spot execution"
            ) from error
        if snap.quality is not DataQuality.FRESH or snap.quote is None:
            raise HTTPException(503, "Spot execution requires a fresh market quote")
        now = datetime.now(UTC)
        order = SpotOrder(
            client_order_id=submission.client_order_id or uuid4().hex,
            order_id=uuid4().hex,
            venue="kraken",
            market="BTC/USD",
            side=submission.side,
            order_type=SpotOrderType.LIMIT,
            quantity=submission.quantity,
            limit_price=submission.limit_price,
            state=OrderState.PREVIEWED,
            risk_decision_id="none",
            created_at=now,
            updated_at=now,
        )
        try:
            observed = await spot_execution.submit(
                order,
                snap.quote,
                candidate_id=submission.candidate_id,
                strategy_version=submission.strategy_version,
            )
            return spot_order_dict(observed)
        except RiskRejected as error:
            raise HTTPException(409, str(error)) from error
        except KrakenOrderRejected as error:
            raise HTTPException(400, str(error)) from error
        except KrakenAmbiguousExecutionError as error:
            raise HTTPException(504, str(error)) from error
        except (sqlite3.Error, ValidationError) as error:
            raise HTTPException(503, "Spot order storage unavailable") from error

    @app.get("/api/v1/research/runs")
    def research_runs(
        response: Response, limit: Annotated[int, Query(ge=1, le=100)] = 25
    ) -> ResearchHistory:
        response.headers["Cache-Control"] = "no-store"
        try:
            return store.research_history(limit)
        except (sqlite3.Error, ValidationError) as error:
            raise HTTPException(503, "Research history unavailable") from error

    @app.get("/api/v1/audit/events")
    def list_audit_events(
        response: Response,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> list[AuditEventResponse]:
        response.headers["Cache-Control"] = "no-store"
        try:
            events = store.list_audit_events(limit=limit)
            return [
                AuditEventResponse(
                    event_id=e.event_id,
                    recorded_at=e.recorded_at,
                    kind=e.kind,
                )
                for e in events
            ]
        except (sqlite3.Error, ValidationError) as error:
            raise HTTPException(503, "Audit event storage unavailable") from error

    @app.get("/api/v1/skills")
    def list_available_skills(response: Response) -> list[SkillResponse]:
        response.headers["Cache-Control"] = "no-store"
        return [
            SkillResponse(
                name=s.name,
                version=s.version,
                purpose=s.purpose,
                capability_tier=s.capability_tier,
                required_tools=s.required_tools,
            )
            for s in list_skills()
        ]

    @app.get("/api/v1/strategies/candidates")
    def list_candidates(
        response: Response,
        stage: PromotionStage | None = None,
    ) -> list[StrategyCandidate]:
        response.headers["Cache-Control"] = "no-store"
        try:
            return store.list_strategy_candidates(stage=stage)
        except (sqlite3.Error, ValidationError) as error:
            raise HTTPException(
                503, "Strategy candidate storage unavailable"
            ) from error

    @app.post("/api/v1/strategies/candidates")
    def create_candidate(
        req: CreateCandidateRequest, request: Request
    ) -> StrategyCandidate:
        _verify_local_origin(request, "Create candidate")
        try:
            return service.create_candidate(
                req.name, req.version, req.parameters, req.dataset_hash
            )
        except ApplicationError as error:
            raise _http_error(error) from error

    def candidate_backtest(candidate: StrategyCandidate) -> BacktestResult:
        try:
            return get_candidate_backtest_report(store, candidate)
        except PromotionRejected as error:
            raise HTTPException(409, str(error)) from error

    def latest_shadow(candidate_id: str) -> ShadowRunResponse:
        try:
            run_id, report = get_latest_shadow_run(store, candidate_id)
            return ShadowRunResponse(run_id=run_id, report=report)
        except PromotionRejected as error:
            raise HTTPException(404, str(error)) from error
        except (sqlite3.Error, ValueError) as error:
            raise HTTPException(503, "Paper shadow run unavailable") from error

    @app.get("/api/v1/strategies/candidates/{candidate_id}/shadow-run")
    def get_shadow_run(candidate_id: str) -> ShadowRunResponse:
        return latest_shadow(candidate_id)

    @app.post("/api/v1/strategies/candidates/{candidate_id}/shadow-run")
    def run_shadow(candidate_id: str, request: Request) -> ShadowRunResponse:
        _verify_local_origin(request, "Run paper shadow simulation")
        try:
            run_id, result = execute_shadow_run(store, candidate_id)
            return ShadowRunResponse(run_id=run_id, report=result)
        except CandidateNotFound as error:
            raise HTTPException(404, str(error)) from error
        except (PaperRejected, PromotionRejected, ValueError) as error:
            raise HTTPException(409, str(error)) from error
        except sqlite3.Error as error:
            raise HTTPException(503, "Paper shadow storage unavailable") from error

    @app.get("/api/v1/strategies/candidates/{candidate_id}/shadow-runs")
    def list_shadow_runs_endpoint(
        candidate_id: str,
        limit: Annotated[int, Query(ge=1, le=100)] = 25,
    ) -> list[ShadowRunResponse]:
        try:
            records = list_shadow_runs_for_candidate(store, candidate_id, limit=limit)
            return [
                ShadowRunResponse(run_id=run_id, report=report)
                for run_id, _, report in records
            ]
        except (sqlite3.Error, ValidationError) as error:
            raise HTTPException(503, "Paper shadow history unavailable") from error

    @app.post("/api/v1/strategies/candidates/{candidate_id}/promote")
    def promote_candidate_endpoint(
        candidate_id: str,
        req: PromoteCandidateRequest,
        request: Request,
    ) -> dict[str, Any]:
        _verify_local_origin(request, "Promote candidate")
        try:
            updated, verdict = promote_strategy_candidate(
                store, candidate_id, req.target_stage
            )
        except CandidateNotFound as error:
            raise HTTPException(404, str(error)) from error
        except PromotionRejected as error:
            raise HTTPException(409, str(error)) from error
        except (sqlite3.Error, ValidationError) as error:
            raise HTTPException(503, "Storage error during promotion") from error
        return {
            "candidate": updated.model_dump(mode="json"),
            "gate_result": {
                "candidate_id": verdict.candidate_id,
                "from_stage": verdict.from_stage.value,
                "to_stage": verdict.to_stage.value,
                "passed": verdict.passed,
                "reasons": verdict.reasons,
                "evaluated_at": verdict.evaluated_at.isoformat(),
            },
        }

    @app.get("/api/v1/strategies/candidates/{candidate_id}/canary-approval")
    def get_candidate_canary_approval(
        candidate_id: str, response: Response
    ) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        try:
            approval = store.get_spot_canary_approval(candidate_id)
            if approval is None:
                raise HTTPException(
                    404, f"No canary approval recorded for '{candidate_id}'"
                )
            return {
                "candidate_id": approval.candidate_id,
                "candidate_version": approval.candidate_version,
                "dataset_hash": approval.dataset_hash,
                "key_fingerprint": approval.key_fingerprint,
                "max_notional": str(approval.max_notional),
                "expires_at": approval.expires_at.isoformat(),
                "stage": approval.stage,
                "active": approval.active,
            }
        except HTTPException:
            raise
        except (sqlite3.Error, ValidationError) as error:
            raise HTTPException(503, "Audit storage unavailable") from error

    @app.get("/api/v1/market/assets")
    def market_assets() -> list[AssetInfo]:
        return service.list_assets()

    @app.get("/api/v1/market/markets")
    def market_registry() -> list[MarketInfo]:
        return service.list_markets()

    @app.get("/api/v1/market/orderbook")
    def market_orderbook(
        response: Response,
        market: Annotated[str | None, Query(max_length=16)] = None,
    ) -> OrderBookSnapshot:
        response.headers["Cache-Control"] = "no-store"
        try:
            return service.orderbook_snapshot(market)
        except ApplicationError as error:
            raise _http_error(error) from error

    @app.get("/api/v1/news/search")
    def news_search(
        q: Annotated[str, Query(min_length=1, max_length=100)],
        limit: Annotated[int, Query(ge=1, le=100)] = 25,
    ) -> list[NewsHit]:
        try:
            return service.search_news(q, limit)
        except ApplicationError as error:
            raise _http_error(error) from error

    @app.get("/api/v1/portfolio/positions")
    def portfolio_positions() -> list[Position]:
        try:
            return service.positions()
        except ApplicationError as error:
            raise _http_error(error) from error

    @app.get("/api/v1/wallets/{wallet_id}/balances")
    def wallet_balances(wallet_id: str, response: Response) -> list[WalletBalanceView]:
        response.headers["Cache-Control"] = "no-store"
        try:
            return service.wallet_balances(wallet_id)
        except ApplicationError as error:
            raise _http_error(error) from error

    @app.get("/api/v1/wallets/{wallet_id}/transactions")
    def wallet_transactions(wallet_id: str, response: Response) -> WalletTransactions:
        response.headers["Cache-Control"] = "no-store"
        try:
            return service.wallet_transactions(wallet_id)
        except ApplicationError as error:
            raise _http_error(error) from error

    @app.get("/api/v1/strategies")
    def list_strategies(response: Response) -> list[StrategyCandidate]:
        response.headers["Cache-Control"] = "no-store"
        try:
            return service.strategies()
        except ApplicationError as error:
            raise _http_error(error) from error

    @app.get("/api/v1/strategies/{strategy_id}/metrics")
    def strategy_metrics(strategy_id: str) -> StrategyMetrics:
        try:
            return service.strategy_metrics(strategy_id)
        except ApplicationError as error:
            raise _http_error(error) from error

    @app.post("/api/v1/orders/preview")
    async def preview_order(
        body: OrderPreviewRequest, request: Request
    ) -> OrderPreview:
        _verify_local_origin(request, "Order preview")
        quote: Any = None
        if quote_service is not None:
            try:
                quote = (await quote_service.snapshot()).quote
            except (QuoteUnavailable, sqlite3.Error):
                quote = None
        try:
            return service.preview_order(
                body.side,
                body.quantity,
                quote=quote,
                idempotency_key=body.idempotency_key,
            )
        except ApplicationError as error:
            raise _http_error(error) from error

    @app.get("/api/v1/orders/{order_id}")
    def get_order(order_id: str, response: Response) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        try:
            record = service.get_order(order_id)
        except ApplicationError as error:
            raise _http_error(error) from error
        return {"kind": record.kind, **record.payload}

    if ui_assets is not None and (ui_assets / "index.html").is_file():
        app.mount("/", StaticFiles(directory=ui_assets, html=True), name="ui")

    return app
