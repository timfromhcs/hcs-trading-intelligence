"""Read-only status endpoints for the loopback application."""

import json
import logging
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from enum import StrEnum
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.trustedhost import TrustedHostMiddleware

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


def create_app(store: AuditStore, ui_assets: Path | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        store.initialize()
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
        return Status()

    @app.get("/api/v1/system/config")
    def config() -> PublicConfig:
        return PublicConfig()

    @app.get("/api/v1/system/capabilities")
    def capabilities() -> Capabilities:
        return Capabilities()

    if ui_assets is not None and (ui_assets / "index.html").is_file():
        app.mount("/", StaticFiles(directory=ui_assets, html=True), name="ui")

    return app
