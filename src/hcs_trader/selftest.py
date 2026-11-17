"""Offline self-test for installed wheels and frozen binaries.

The self-test uses only a temporary data directory, loopback sockets and local
subprocesses. It never reads the user's data directory, never contacts an
external host and never enables execution.
"""

import asyncio
import json
import os
import sqlite3
import subprocess  # noqa: S404
import sys
import threading
from collections.abc import Callable
from http.client import HTTPConnection
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep

import uvicorn
from mcp import Client, StdioServerParameters

from hcs_trader.api import LOOPBACK_HOST, Mode, Status, create_app
from hcs_trader.mcp_server import CapabilityTier, tool_tier
from hcs_trader.storage import AuditStore

TRIP_EXIT_CODE = 77
_FORBIDDEN_TOOL_MARKERS = ("place_live", "execute", "withdraw", "sign", "transfer")


def self_command() -> list[str]:
    """Command prefix that re-runs this program, frozen or from source."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, "-m", "hcs_trader"]


def trip_and_die(data_dir: Path) -> None:
    """Persist the kill switch, then terminate without any cleanup."""
    store = AuditStore(data_dir / "audit.sqlite")
    store.initialize()
    store.set_execution_pause(True)
    os._exit(TRIP_EXIT_CODE)


def _check_migration(data_dir: Path) -> None:
    store = AuditStore(data_dir / "audit.sqlite")
    store.initialize()
    store.initialize()  # migrations must be idempotent
    with sqlite3.connect(store.path) as connection:
        (version,) = connection.execute("PRAGMA user_version").fetchone()
    if version < 1:
        raise RuntimeError("Database was not migrated")
    if store.execution_pause().paused:
        raise RuntimeError("A fresh database must not start paused")


def _check_paper_default(data_dir: Path) -> None:
    status = Status()
    if (
        status.mode is not Mode.PAPER
        or status.execution_available
        or status.network_enabled
    ):
        raise RuntimeError("Default capabilities are not PAPER and offline")
    # The app built with its defaults must report the same through its API.
    served = json.loads(_serve_get(data_dir, "/api/v1/system/status"))
    if (
        served.get("mode") != "PAPER"
        or served.get("execution_available") is not False
        or served.get("network_enabled") is not False
    ):
        raise RuntimeError(f"Served capabilities are not PAPER and offline: {served}")


def _check_kill_switch_survives_kill(data_dir: Path) -> None:
    child = subprocess.run(  # noqa: S603
        [*self_command(), "selftest", "--trip-and-die", str(data_dir)],
        capture_output=True,
        timeout=60,
        check=False,
    )
    if child.returncode != TRIP_EXIT_CODE:
        raise RuntimeError(
            f"Trip process ended with {child.returncode}: "
            f"{child.stderr.decode(errors='replace')[-300:]}"
        )
    if not AuditStore(data_dir / "audit.sqlite").execution_pause().paused:
        raise RuntimeError("Kill switch was lost after the process was killed")


def _serve_get(data_dir: Path, path: str) -> bytes:
    """Serve the real app on an ephemeral loopback port and GET one path."""
    if LOOPBACK_HOST != "127.0.0.1":
        raise RuntimeError("Service host is not loopback")
    app = create_app(AuditStore(data_dir / "audit.sqlite"), data_dir / "no-ui")
    config = uvicorn.Config(
        app,
        host=LOOPBACK_HOST,
        port=0,
        proxy_headers=False,
        access_log=False,
        log_level="warning",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=lambda: asyncio.run(server.serve()), daemon=True)
    thread.start()
    try:
        deadline = monotonic() + 20
        while not server.started and monotonic() < deadline and thread.is_alive():
            sleep(0.05)
        if not server.started:
            raise RuntimeError("Loopback server did not start")
        sockets = [s for srv in server.servers for s in srv.sockets]
        if not sockets or any(s.getsockname()[0] != "127.0.0.1" for s in sockets):
            raise RuntimeError("Server socket is not bound to 127.0.0.1 only")
        port = sockets[0].getsockname()[1]
        connection = HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            body = response.read()
            if response.status != 200:
                raise RuntimeError(f"HTTP {response.status} for {path}")
            return body
        finally:
            connection.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def _check_loopback_bind(data_dir: Path) -> None:
    if json.loads(_serve_get(data_dir, "/api/v1/health")) != {"status": "ok"}:
        raise RuntimeError("Health endpoint did not report ok")


async def _list_mcp_tools(data_dir: Path) -> list[str]:
    command = self_command()
    transport = StdioServerParameters(
        command=command[0],
        args=[*command[1:], "mcp-stdio", "--data-dir", str(data_dir)],
    )
    async with Client(transport, read_timeout_seconds=30) as client:
        return [tool.name for tool in (await client.list_tools()).tools]


def _check_mcp_stdio(data_dir: Path) -> None:
    names = asyncio.run(_list_mcp_tools(data_dir))
    if "risk.get_status" not in names:
        raise RuntimeError("MCP server did not list its read tools")
    # Exact allowlist: every listed tool must be a declared one, and none may be
    # a LIVE_TRADE tool, whatever its name.
    undeclared: list[str] = []
    live: list[str] = []
    for name in names:
        try:
            tier = tool_tier(name)
        except KeyError:
            undeclared.append(name)
            continue
        if tier is CapabilityTier.LIVE_TRADE:
            live.append(name)
    if undeclared or live:
        raise RuntimeError(
            f"MCP server lists undeclared tools {undeclared} or live tools {live}"
        )
    unsafe = [n for n in names if any(m in n for m in _FORBIDDEN_TOOL_MARKERS)]
    if unsafe:
        raise RuntimeError(f"MCP server exposes side-effect tools: {unsafe}")


def run_selftest() -> list[tuple[str, bool, str]]:
    """Run every check in order; a failure never hides later results."""
    results: list[tuple[str, bool, str]] = []
    with TemporaryDirectory(prefix="hcs-selftest-") as temporary:
        data_dir = Path(temporary) / "data dir"
        data_dir.mkdir()
        checks: list[tuple[str, Callable[[Path], None]]] = [
            ("database_migrates", _check_migration),
            ("paper_default", _check_paper_default),
            ("kill_switch_survives_kill", _check_kill_switch_survives_kill),
            ("loopback_bind", _check_loopback_bind),
            ("mcp_stdio_list_tools", _check_mcp_stdio),
        ]
        for name, check in checks:
            try:
                check(data_dir)
            except Exception as error:  # noqa: BLE001
                results.append((name, False, f"{type(error).__name__}: {error}"))
            else:
                results.append((name, True, "ok"))
    return results


def main() -> int:
    results = run_selftest()
    for name, passed, detail in results:
        print(json.dumps({"check": name, "passed": passed, "detail": detail}))
    return 0 if all(passed for _, passed, _ in results) else 1
