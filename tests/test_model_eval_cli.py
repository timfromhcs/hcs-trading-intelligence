"""CLI wiring for the offline model-boundary eval harness."""

import json
import sys

import pytest

from hcs_trader import __main__ as launcher
from hcs_trader.agents import eval as model_eval


def test_eval_model_command_reports_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["hcs-trader", "eval-model"])
    launcher.main()
    output = json.loads(capsys.readouterr().out)
    assert output["passed"] is True
    assert output["ratio"] == 1.0
    assert {result["category"] for result in output["results"]} == {
        "schema",
        "citation",
        "injection",
    }


def test_eval_model_command_exits_nonzero_when_gate_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def blocked(_report: model_eval.EvalReport) -> None:
        raise model_eval.EvalNotReady("gate failed")

    monkeypatch.setattr(model_eval, "assert_release_ready", blocked)
    monkeypatch.setattr(sys, "argv", ["hcs-trader", "eval-model"])
    with pytest.raises(SystemExit) as exit_info:
        launcher.main()
    assert exit_info.value.code == 1
    assert "gate failed" in capsys.readouterr().err
