import json
from importlib.metadata import version

import pytest

from hcs_trader.__main__ import main


def test_default_capabilities(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.argv", ["hcs-trader"])
    main()
    assert json.loads(capsys.readouterr().out) == {
        "mode": "PAPER",
        "execution_available": False,
        "network_enabled": False,
    }


def test_version(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.argv", ["hcs-trader", "--version"])
    with pytest.raises(SystemExit) as exit_info:
        main()
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == version("hcs-trader")


def test_live_argument_is_rejected(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.argv", ["hcs-trader", "--live"])
    with pytest.raises(SystemExit) as exit_info:
        main()
    assert exit_info.value.code == 2
    assert capsys.readouterr().out == ""
