"""The CLI surface must not advertise signing, export or arbitrary transfer tools."""

import subprocess  # noqa: S404
import sys


def test_help_lists_only_reviewed_commands() -> None:
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "hcs_trader", "--help"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    help_text = result.stdout
    for command in (
        "serve",
        "mcp",
        "mcp-http",
        "selftest",
        "eval-model",
        "webfetch",
        "sources",
        "size",
        "live-status",
        "live-enable",
        "live-revoke",
    ):
        assert command in help_text
    for forbidden in (
        "seed-export",
        "private-key-export",
        "sign-arbitrary",
        "withdraw",
        "arbitrary-transfer",
    ):
        assert forbidden not in help_text
