"""CLI wiring for the side-effect-free sizing preview."""

import json
import sys

import pytest

from hcs_trader import __main__ as launcher

_ARGS = [
    "hcs-trader",
    "size",
    "--hot-equity",
    "10000",
    "--entry-price",
    "100",
    "--invalidation-price",
    "95",
    "--risk-fraction",
    "0.0025",
    "--max-position-notional",
    "2000",
    "--available-quote",
    "5000",
]


def test_size_cli_returns_no_authorization(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", _ARGS)
    launcher.main()
    body = json.loads(capsys.readouterr().out)
    assert body["authorization"] == "NONE"
    assert float(body["quantity"]) > 0


def test_size_cli_rejects_invalid_input(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    args = list(_ARGS)
    args[args.index("95") + 1] = "100"  # invalidation == entry
    monkeypatch.setattr(sys, "argv", args)
    with pytest.raises(SystemExit) as exit_info:
        launcher.main()
    assert exit_info.value.code == 2
    assert capsys.readouterr().err
