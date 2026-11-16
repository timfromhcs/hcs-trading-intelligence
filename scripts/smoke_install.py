"""Shared smoke checks for an installed hcs-trader environment.

Runs the installed program from a data directory whose path contains spaces and
non-ASCII characters, with no usable network proxy, then checks the health
endpoint, the OpenAPI schema and an MCP stdio tool listing.
"""

import json
import os
import subprocess  # noqa: S404
import sys
from http.client import HTTPConnection
from pathlib import Path
from time import monotonic, sleep

LOCAL_PORT = 8765
UNICODE_DIRECTORY = "Données héhé 数据"
_MCP_PROBE = """
import asyncio, json, sys
from pathlib import Path
from mcp import Client, StdioServerParameters

async def main(path):
    transport = StdioServerParameters(
        command=sys.executable,
        args=["-c", "import sys; from pathlib import Path; "
              "from hcs_trader.mcp_server import create_mcp_server; "
              "create_mcp_server(Path(sys.argv[1])).run()", path],
    )
    async with Client(transport, read_timeout_seconds=20) as client:
        denied = await client.call_tool(
            "orders.place_paper",
            {"side": "buy", "quantity": "0.01", "idempotency_key": "smoke-paper"},
        )
        if not denied.is_error or not any(
            "Capability tier PAPER_TRADE is not granted" in getattr(item, "text", "")
            for item in denied.content
        ):
            raise RuntimeError("Default MCP session did not deny paper order writes")
        print(json.dumps(sorted(t.name for t in (await client.list_tools()).tools)))

asyncio.run(main(sys.argv[1]))
"""


def offline_environment(root: Path) -> dict[str, str]:
    """Environment whose proxies point at a closed port, so no request can leave."""
    dead = "http://127.0.0.1:9"
    return {
        **os.environ,
        "HTTP_PROXY": dead,
        "HTTPS_PROXY": dead,
        "ALL_PROXY": dead,
        "NO_PROXY": "127.0.0.1,localhost",
        "TEMP": str(root),
        "TMP": str(root),
        "TMPDIR": str(root),
    }


def get(path: str) -> bytes:
    if not path.startswith("/") or path.startswith("//"):
        raise ValueError("Expected a local request path")
    connection = HTTPConnection("127.0.0.1", LOCAL_PORT, timeout=2)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError(f"Unexpected HTTP status {response.status} for {path}")
        return response.read()
    finally:
        connection.close()


def check_api() -> None:
    deadline = monotonic() + 30
    while monotonic() < deadline:
        try:
            if json.loads(get("/api/v1/health")) == {"status": "ok"}:
                break
        except OSError:
            sleep(0.2)
    else:
        raise RuntimeError("Installed server did not become healthy")
    paths = json.loads(get("/openapi.json")).get("paths", {})
    if "/api/v1/health" not in paths or "/api/v1/system/status" not in paths:
        raise RuntimeError("OpenAPI schema is missing documented endpoints")
    status = json.loads(get("/api/v1/system/status"))
    if (
        status["mode"] != "PAPER"
        or status["execution_available"]
        or status["network_enabled"]
    ):
        raise RuntimeError("Unexpected startup capabilities")


def probe_installed(python: Path, cli: Path, root: Path) -> None:
    """Start the installed CLI under a Unicode/space path and verify API and MCP."""
    data = root / UNICODE_DIRECTORY / "app data"
    environment = offline_environment(root)
    log = root / "serve.log"
    with log.open("wb") as output:
        process = subprocess.Popen(  # noqa: S603
            [str(cli), "serve", "--data-dir", str(data)],
            cwd=root,
            env=environment,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
    try:
        if process.poll() is not None:
            raise RuntimeError("Installed server exited early")
        check_api()
        if not (data / "audit.sqlite").is_file():
            raise RuntimeError("Database was not created in the selected directory")
    except (RuntimeError, OSError) as error:
        raise RuntimeError(f"{error}\n{log.read_text(errors='replace')}") from error
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    output_text = subprocess.check_output(  # noqa: S603
        [str(python), "-c", _MCP_PROBE, str(data / "audit.sqlite")],
        env=environment,
        text=True,
        timeout=60,
    )
    tools = json.loads(output_text.strip().splitlines()[-1])
    if "risk.get_status" not in tools or any(
        ("place" in name and name != "orders.place_paper") or "withdraw" in name
        for name in tools
    ):
        raise RuntimeError(f"Unexpected MCP tool catalogue: {tools}")


if __name__ == "__main__":
    sys.exit("Import probe_installed from the wheel or sdist smoke test.")
