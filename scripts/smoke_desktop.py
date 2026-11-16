"""Smoke-test a native desktop build with a separate data directory."""

import argparse
import json
import os
import re
import shutil
import subprocess  # noqa: S404
from collections.abc import Iterator
from contextlib import contextmanager
from http.client import HTTPConnection
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep

_NAME = "HCS-Trading-Intelligence"


@contextmanager
def _temporary_root(*, wait_for_extracted_files: bool) -> Iterator[Path]:
    temporary = TemporaryDirectory(prefix="hcs-desktop-")
    try:
        yield Path(temporary.name)
    finally:
        deadline = monotonic() + 10
        while True:
            try:
                temporary.cleanup()
                break
            except PermissionError:
                # Windows may release the child's DLL mappings after taskkill returns.
                if not wait_for_extracted_files or monotonic() >= deadline:
                    raise
                sleep(0.25)


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
    if "/api/v1/health" not in json.loads(_read("/openapi.json")).get("paths", {}):
        raise RuntimeError("Desktop OpenAPI schema is unavailable")
    html = _read("/").decode("utf-8")
    script = re.search(r'src="(/assets/[^"]+\.js)"', html)
    if (
        "HCS Trading Intelligence" not in html
        or script is None
        or not _read(script.group(1))
    ):
        raise RuntimeError("Desktop dashboard assets are unavailable")


def _selftest(executable: Path) -> None:
    """Run the frozen binary's own offline selftest; any failed check is fatal."""
    result = subprocess.run(  # noqa: S603
        [str(executable), "selftest"],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    print(result.stdout, end="")
    if result.returncode != 0:
        print(result.stderr)
        raise RuntimeError("Frozen selftest failed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--onefile", action="store_true")
    args = parser.parse_args()
    suffix = ".exe" if os.name == "nt" else ""
    layout = Path("dist") / ("desktop-single" if args.onefile else "desktop")
    program_dir = layout if args.onefile else layout / _NAME
    executable = (program_dir / (_NAME + suffix)).resolve()
    _selftest(executable)
    with _temporary_root(wait_for_extracted_files=args.onefile) as root:
        data = root / "Données héhé 数据" / "app data"
        log = root / "desktop.log"
        with log.open("wb") as output:
            process = subprocess.Popen(  # noqa: S603
                [str(executable), "serve", "--data-dir", str(data)],
                cwd=root,
                env={
                    **os.environ,
                    "TEMP": str(root),
                    "TMP": str(root),
                    "TMPDIR": str(root),
                },
                stdout=output,
                stderr=subprocess.STDOUT,
            )
        try:
            _verify(process, data)
        except RuntimeError as error:
            raise RuntimeError(f"{error}\n{log.read_text(errors='replace')}") from error
        finally:
            if process.poll() is None:
                if args.onefile and os.name == "nt":
                    # The Windows bootloader runs a child containing the Python runtime.
                    taskkill = shutil.which("taskkill")
                    if taskkill is None:
                        raise RuntimeError("Windows taskkill is unavailable")
                    subprocess.run(  # noqa: S603
                        [taskkill, "/PID", str(process.pid), "/T", "/F"],
                        check=True,
                        stdout=subprocess.DEVNULL,
                    )
                else:
                    process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
    print(f"Verified {executable.name}, the dashboard and the paper-mode API.")


if __name__ == "__main__":
    main()
