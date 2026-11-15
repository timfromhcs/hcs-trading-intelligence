import asyncio
import json
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hcs_trader.__main__ import main, market_research, watch_research
from hcs_trader.api import create_app
from hcs_trader.indicators import Indicators
from hcs_trader.local_research import ResearchReport
from hcs_trader.promotion import PromotionStage, StrategyCandidate
from hcs_trader.storage import AuditStore


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


def test_research_history_persists_and_matches_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "audit.sqlite"
    monkeypatch.setattr("hcs_trader.__main__.local_store", lambda: AuditStore(path))
    for interval in ("60", "30"):
        monkeypatch.setattr(
            "sys.argv", ["hcs-trader", "research-brief", "--interval", interval]
        )
        main()
        assert json.loads(capsys.readouterr().out)["status"] == "model_disabled"
    monkeypatch.setattr("sys.argv", ["hcs-trader", "research-history", "--limit", "1"])
    main()
    history = json.loads(capsys.readouterr().out)
    (run,) = history["runs"]
    assert run["interval_minutes"] == 30
    assert run["duration_ms"] >= 0 and run["evidence"]["rsi14"] is None
    (stats,) = history["statistics"]
    assert stats["count"] == 2 and stats["status"] == "model_disabled"
    with TestClient(create_app(AuditStore(path)), base_url="http://127.0.0.1") as api:
        response = api.get("/api/v1/research/runs?limit=1")
        assert response.json() == history
        assert response.headers["Cache-Control"] == "no-store"


def test_scheduled_research_backs_off_and_restart_preserves_due_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "audit.sqlite"
    monkeypatch.setattr("hcs_trader.__main__.local_store", lambda: AuditStore(path))

    async def unavailable(*args: object, **kwargs: object) -> ResearchReport:
        return ResearchReport("unavailable", Indicators(0), model="test")

    monkeypatch.setattr("hcs_trader.__main__._configured_research", unavailable)
    started = datetime.now(UTC)
    report = asyncio.run(market_research(60, 9931, None, path, path, schedule=(60, 2)))
    assert report.status == "unavailable"
    store = AuditStore(path)
    due, failures = store.research_schedule()
    assert due is not None and due >= started + timedelta(seconds=480)
    assert failures == 3 and store.research_history().statistics[0].count == 1

    async def unexpected_run(failures: int) -> ResearchReport:
        pytest.fail(f"Schedule started early with failure count {failures}")

    async def restart() -> None:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(watch_research(store, unexpected_run), timeout=0.05)

    asyncio.run(restart())
    assert store.research_schedule() == (due, 3)
    with store.research_worker_lock():
        with pytest.raises(ValueError, match="already running"):
            with AuditStore(path).research_worker_lock():
                pytest.fail("Two workers acquired the same lock")
    with AuditStore(path).research_worker_lock():
        assert store.research_schedule() == (due, 3)


def test_strategy_cli_list_and_show(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "audit.sqlite"
    store = AuditStore(path)
    store.initialize()
    now = datetime.now(UTC)
    cand = StrategyCandidate(
        candidate_id="cand-cli-01",
        name="SMA Test",
        version="1.0.0",
        stage=PromotionStage.RESEARCH,
        parameters={"fast": 10, "slow": 30},
        dataset_hash="hash123",
        created_at=now,
        updated_at=now,
    )
    store.save_strategy_candidate(cand)
    monkeypatch.setattr("hcs_trader.__main__.local_store", lambda: store)

    # 1. list all
    monkeypatch.setattr("sys.argv", ["hcs-trader", "strategy-list"])
    main()
    listed = json.loads(capsys.readouterr().out)
    assert len(listed) == 1
    assert listed[0]["candidate_id"] == "cand-cli-01"

    # 2. list with stage filter matching
    monkeypatch.setattr(
        "sys.argv", ["hcs-trader", "strategy-list", "--stage", "RESEARCH"]
    )
    main()
    assert len(json.loads(capsys.readouterr().out)) == 1

    # 3. list with stage filter non-matching
    monkeypatch.setattr(
        "sys.argv", ["hcs-trader", "strategy-list", "--stage", "LIVE_BOUNDED"]
    )
    main()
    assert len(json.loads(capsys.readouterr().out)) == 0

    # 4. show existing
    monkeypatch.setattr("sys.argv", ["hcs-trader", "strategy-show", "cand-cli-01"])
    main()
    shown = json.loads(capsys.readouterr().out)
    assert shown["candidate_id"] == "cand-cli-01"
    assert shown["name"] == "SMA Test"
    assert shown["has_canary_approval"] is False

    # 5. show non-existent
    monkeypatch.setattr("sys.argv", ["hcs-trader", "strategy-show", "non-existent"])
    with pytest.raises(SystemExit) as exit_info:
        main()
    assert exit_info.value.code == 1
    assert "not found" in capsys.readouterr().err

    # 6. promote cand-cli-01 to UNIT_TESTED
    monkeypatch.setattr("sys.argv", ["hcs-trader", "strategy-promote", "cand-cli-01"])
    main()
    promoted_out = capsys.readouterr().out
    assert (
        "Successfully promoted candidate 'cand-cli-01' to UNIT_TESTED." in promoted_out
    )
    updated = store.get_strategy_candidate("cand-cli-01")
    assert updated is not None and updated.stage == PromotionStage.UNIT_TESTED

    # 7. promote cand-cli-01 further fails without backtest report
    monkeypatch.setattr("sys.argv", ["hcs-trader", "strategy-promote", "cand-cli-01"])
    with pytest.raises(SystemExit) as exit_info:
        main()
    assert exit_info.value.code == 1
    assert "Candidate needs a saved backtest report ID" in capsys.readouterr().err

    # 8. promote non-existent candidate fails
    monkeypatch.setattr("sys.argv", ["hcs-trader", "strategy-promote", "non-existent"])
    with pytest.raises(SystemExit) as exit_info:
        main()
    assert exit_info.value.code == 1
    assert "not found" in capsys.readouterr().err


def test_new_research_watch_runs_immediately_and_persists_next_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = AuditStore(tmp_path / "audit.sqlite")
    monkeypatch.setattr("hcs_trader.__main__.local_store", lambda: store)

    async def exercise() -> None:
        finished = asyncio.Event()

        async def scheduled(failures: int) -> ResearchReport:
            report = await market_research(
                60, 9931, None, None, None, schedule=(60, failures)
            )
            finished.set()
            return report

        task = asyncio.create_task(watch_research(store, scheduled))
        try:
            await asyncio.wait_for(finished.wait(), timeout=2)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(exercise())
    due, failures = store.research_schedule()
    assert due is not None and due > datetime.now(UTC)
    assert failures == 0
    assert len(store.research_history().runs) == 1
