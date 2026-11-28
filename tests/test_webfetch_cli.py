"""CLI wiring for registered, unverified-by-default web sources."""

import json
import sys

import pytest

from hcs_trader import __main__ as launcher


def test_webfetch_cli_fails_closed_for_disabled_source(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["hcs-trader", "webfetch", "ecb-sdmx"])
    with pytest.raises(SystemExit) as exit_info:
        launcher.main()
    assert exit_info.value.code == 2
    captured = capsys.readouterr()
    assert "verified and enabled" in captured.err
    assert captured.out == ""


def test_sources_cli_is_offline_and_disables_everything(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["hcs-trader", "sources"])
    launcher.main()
    rows = json.loads(capsys.readouterr().out)
    assert rows
    assert all(row["enabled"] is False for row in rows)
    assert all(row["verification_result"] == "pending" for row in rows)
    assert all("api_key" not in row for row in rows)


def test_webfetch_cli_rejects_unknown_source(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["hcs-trader", "webfetch", "no-such-source"])
    with pytest.raises(SystemExit) as exit_info:
        launcher.main()
    assert exit_info.value.code == 2
    assert "not registered" in capsys.readouterr().err
