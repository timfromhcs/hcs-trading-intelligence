"""Crash-recovery and selftest regression tests (no network, temp data only)."""

import json
import os
import subprocess  # noqa: S404
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from hcs_trader.domain.paper import PaperOrder, PaperRejected, PaperSide
from hcs_trader.selftest import TRIP_EXIT_CODE
from hcs_trader.storage import AuditStore

_TRIP = (
    "import os, sys; from pathlib import Path; "
    "from hcs_trader.storage import AuditStore; "
    "s = AuditStore(Path(sys.argv[1])); s.initialize(); "
    "s.set_execution_pause(True); os._exit(3)"
)


def test_kill_switch_survives_hard_process_exit(tmp_path: Path) -> None:
    path = tmp_path / "state dir" / "audit.sqlite"
    child = subprocess.run(  # noqa: S603
        [sys.executable, "-c", _TRIP, str(path)],
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert child.returncode == 3, child.stderr.decode(errors="replace")

    reopened = AuditStore(path)
    reopened.initialize()  # re-running migrations must not clear the switch
    assert reopened.execution_pause().paused is True
    with pytest.raises(PaperRejected, match="paused"):
        reopened.record_paper_fill(
            PaperOrder("after-crash-1", PaperSide.BUY, Decimal("0.01")),
            Decimal(1000),
            Decimal(1),
        )
    # Only a deliberate local resume clears it.
    reopened.set_execution_pause(False)
    assert AuditStore(path).execution_pause().paused is False


def test_selftest_child_trips_and_dies_with_marker_code(tmp_path: Path) -> None:
    child = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "hcs_trader",
            "selftest",
            "--trip-and-die",
            str(tmp_path),
        ],
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert child.returncode == TRIP_EXIT_CODE
    assert AuditStore(tmp_path / "audit.sqlite").execution_pause().paused


def test_selftest_command_passes_and_reports_every_check() -> None:
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "hcs_trader", "selftest"],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    lines = [json.loads(line) for line in result.stdout.splitlines() if line]
    assert [line["check"] for line in lines] == [
        "database_migrates",
        "paper_default",
        "kill_switch_survives_kill",
        "loopback_bind",
        "mcp_stdio_list_tools",
    ]
    assert all(line["passed"] for line in lines)
