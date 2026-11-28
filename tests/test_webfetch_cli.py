"""CLI wiring for registered, unverified-by-default web sources."""

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


def test_webfetch_cli_rejects_unknown_source(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["hcs-trader", "webfetch", "no-such-source"])
    with pytest.raises(SystemExit) as exit_info:
        launcher.main()
    assert exit_info.value.code == 2
    assert "not registered" in capsys.readouterr().err
