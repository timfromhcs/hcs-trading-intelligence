"""CLI wiring for the shared cost model preview."""

import json
import sys

import pytest

from hcs_trader import __main__ as launcher


def test_costs_cli_preview(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["hcs-trader", "costs", "--reference-price", "100", "--side", "buy"],
    )
    launcher.main()
    body = json.loads(capsys.readouterr().out)
    assert body["authorization"] == "NONE"
    assert float(body["execution_price"]) > 100


def test_costs_cli_rejects_bad_fee(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hcs-trader",
            "costs",
            "--reference-price",
            "100",
            "--side",
            "buy",
            "--fee-bps",
            "999",
        ],
    )
    with pytest.raises(SystemExit) as exit_info:
        launcher.main()
    assert exit_info.value.code == 2
    assert capsys.readouterr().err
