"""Smoke-test a native desktop folder without using the default data directory."""

import json
import os
import re
import subprocess  # noqa: S404
from http.client import HTTPConnection
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep

_NAME = "HCS-Trading-Intelligence"


def _read(path: str) -> bytes:
    if not path.startswith("/") or path.startswith("//"):
        raise ValueError("Expected a local request path")
    connection = HTTPConnection("127.0.0.1", 8765, timeout=2)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError(f"Unexpected HTTP status {response.status} for {path}")
        return response.read()
    finally:
        connection.close()


def _verify(process: subprocess.Popen[bytes], data: Path) -> None:
    deadline = monotonic() + 30
    while monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Desktop process exited before serving the dashboard")
        try:
            if json.loads(_read("/api/v1/health")) == {"status": "ok"}:
                break
        except OSError:
            pass
        sleep(0.2)
    else:
        raise RuntimeError("Desktop startup timed out")
    if not (data / "audit.sqlite").is_file():
        raise RuntimeError(
            "Desktop must create its database in the selected data directory"
        )
    status = json.loads(_read("/api/v1/system/status"))
    if (
        status["mode"] != "PAPER"
        or status["execution_available"]
        or status["network_enabled"]
    ):
        raise RuntimeError("Unexpected desktop startup capabilities")
    html = _read("/").decode("utf-8")
    script = re.search(r'src="(/assets/[^"]+\.js)"', html)
    if (
        "HCS Trading Intelligence" not in html
        or script is None
        or not _read(script.group(1))
    ):
        raise RuntimeError("Desktop dashboard assets are unavailable")


def main() -> None:
    suffix = ".exe" if os.name == "nt" else ""
    executable = (Path("dist") / "desktop" / _NAME / (_NAME + suffix)).resolve()
    with TemporaryDirectory(prefix="hcs-desktop-") as temporary:
        root = Path(temporary)
        data = root / "app data"
        log = root / "desktop.log"
        with log.open("wb") as output:
            process = subprocess.Popen(  # noqa: S603
                [str(executable), "serve", "--data-dir", str(data)],
                cwd=root,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
        try:
            _verify(process, data)
        except RuntimeError as error:
            raise RuntimeError(f"{error}\n{log.read_text(errors='replace')}") from error
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
    print(f"Verified {executable.name}, the dashboard and the paper-mode API.")


if __name__ == "__main__":
    main()
