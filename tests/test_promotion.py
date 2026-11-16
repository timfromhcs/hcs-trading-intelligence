import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from mcp import Client
from starlette.testclient import TestClient

from hcs_trader import __main__ as launcher
from hcs_trader.api import create_app
from hcs_trader.backtest import BacktestConfig, BacktestResult, SimulatedFill
from hcs_trader.data.kraken import BTC_USD, SOURCE
from hcs_trader.domain.market_data import Candle
from hcs_trader.mcp_server import create_mcp_server
from hcs_trader.promotion import (
    PROMOTION_LADDER,
    PromotionStage,
    StrategyCandidate,
    compare_champion_challenger,
    evaluate_promotion_gate,
    execute_shadow_run,
    get_latest_shadow_run,
    list_shadow_runs_for_candidate,
    next_promotion_stage,
    promote_strategy_candidate,
    run_forward_shadow_observation,
)
from hcs_trader.storage import AuditStore
from hcs_trader.walk_forward import (
    Evaluation,
    PeriodPair,
    WalkForwardConfig,
    WalkForwardResult,
)


def _make_backtest(
    strategy: str = "test",
    net_return: Decimal = Decimal("0.05"),
    max_drawdown: Decimal = Decimal("0.10"),
    trade_count: int = 5,
) -> BacktestResult:
    now = datetime.now(UTC)
    fills = tuple(
        SimulatedFill(
            side="buy" if i % 2 == 0 else "sell",
            signal_at=now,
            filled_at=now,
            requested_quantity=Decimal("1"),
            quantity=Decimal("1"),
            price=Decimal("100"),
            fee=Decimal("0.16"),
            partial=False,
        )
        for i in range(trade_count)
    )
    return BacktestResult(
        strategy=strategy,
        dataset_sha256="dummy-sha",
        candle_count=100,
        dataset_opened_at=now,
        dataset_closed_at=now,
        config=BacktestConfig(),
        ending_cash=Decimal("10000"),
        ending_quantity=Decimal("0"),
        ending_equity=Decimal("10000") * (Decimal("1") + net_return),
        net_return=net_return,
        max_drawdown=max_drawdown,
        total_fees=Decimal("0.80"),
        fills=fills,
    )


def test_promotion_ladder_ordering() -> None:
    assert PROMOTION_LADDER[0] == PromotionStage.RESEARCH
    assert PROMOTION_LADDER[-1] == PromotionStage.LIVE_BOUNDED
    assert next_promotion_stage(PromotionStage.RESEARCH) == PromotionStage.UNIT_TESTED
    assert next_promotion_stage(PromotionStage.LIVE_BOUNDED) is None


def test_gate_rejects_skipping_stages() -> None:
    candidate = StrategyCandidate(
        candidate_id="cand-1",
        name="RSI Momentum",
        version="1.0.0",
        stage=PromotionStage.RESEARCH,
        dataset_hash="abc",
    )
    result = evaluate_promotion_gate(candidate, PromotionStage.BACKTESTED)
    assert result.passed is False
    assert "Invalid transition" in result.reasons[0]


def test_unit_tested_gate_validates_parameters() -> None:
    bad_cand = StrategyCandidate(
        candidate_id="cand-2",
        name="Bad Params",
        version="1.0.0",
        stage=PromotionStage.RESEARCH,
        parameters={"fast": 20, "slow": 10},  # fast >= slow is invalid
        dataset_hash="abc",
    )
    bad_res = evaluate_promotion_gate(bad_cand, PromotionStage.UNIT_TESTED)
    assert bad_res.passed is False

    good_cand = StrategyCandidate(
        candidate_id="cand-3",
        name="Good Params",
        version="1.0.0",
        stage=PromotionStage.RESEARCH,
        parameters={"fast": 10, "slow": 20},
        dataset_hash="abc",
    )
    good_res = evaluate_promotion_gate(good_cand, PromotionStage.UNIT_TESTED)
    assert good_res.passed is True


def test_backtested_gate_evaluates_drawdown_and_trades() -> None:
    cand = StrategyCandidate(
        candidate_id="cand-4",
        name="Backtest Cand",
        version="1.0.0",
        stage=PromotionStage.UNIT_TESTED,
        parameters={"fast": 10, "slow": 20},
        dataset_hash="abc",
    )
    # Missing backtest
    assert evaluate_promotion_gate(cand, PromotionStage.BACKTESTED).passed is False

    # Zero trades
    zero_trades = _make_backtest(trade_count=0)
    assert (
        evaluate_promotion_gate(
            cand, PromotionStage.BACKTESTED, backtest=zero_trades
        ).passed
        is False
    )

    # Extreme drawdown > 25%
    high_dd = _make_backtest(max_drawdown=Decimal("0.35"), trade_count=10)
    assert (
        evaluate_promotion_gate(
            cand, PromotionStage.BACKTESTED, backtest=high_dd
        ).passed
        is False
    )

    # Valid backtest
    valid_bt = _make_backtest(
        net_return=Decimal("0.12"), max_drawdown=Decimal("0.10"), trade_count=10
    )
    assert (
        evaluate_promotion_gate(
            cand, PromotionStage.BACKTESTED, backtest=valid_bt
        ).passed
        is True
    )


def test_walk_forward_gate() -> None:
    cand = StrategyCandidate(
        candidate_id="cand-wf",
        name="WF Cand",
        version="1.0.0",
        stage=PromotionStage.BACKTESTED,
        dataset_hash="abc",
    )
    # Missing walk forward
    assert (
        evaluate_promotion_gate(cand, PromotionStage.WALK_FORWARD_PASSED).passed
        is False
    )

    # Empty folds
    now = datetime.now(UTC)
    bt = _make_backtest()
    eval_item = Evaluation(
        started_at=now,
        ended_at=now,
        evaluated_candles=50,
        selected=PeriodPair(5, 10),
        selection_dataset_sha256="sha-wf",
        training_return=Decimal("0.05"),
        strategy=bt,
        benchmark=bt,
    )
    empty_wf = WalkForwardResult(
        config=WalkForwardConfig(),
        folds=(),
        holdout=eval_item,
    )
    assert (
        evaluate_promotion_gate(
            cand, PromotionStage.WALK_FORWARD_PASSED, walk_forward=empty_wf
        ).passed
        is False
    )

    # Valid walk forward
    valid_wf = WalkForwardResult(
        config=WalkForwardConfig(),
        folds=(eval_item,),
        holdout=eval_item,
    )
    assert (
        evaluate_promotion_gate(
            cand, PromotionStage.WALK_FORWARD_PASSED, walk_forward=valid_wf
        ).passed
        is True
    )


def test_paper_shadow_and_approved_gates() -> None:
    cand = StrategyCandidate(
        candidate_id="cand-5",
        name="Paper Cand",
        version="1.0.0",
        stage=PromotionStage.WALK_FORWARD_PASSED,
        dataset_hash="abc",
    )
    assert evaluate_promotion_gate(cand, PromotionStage.PAPER_SHADOW).passed is False

    # PAPER_APPROVED requires >= 5 shadow fills
    shadow_cand = StrategyCandidate(
        candidate_id="cand-5",
        name="Paper Cand",
        version="1.0.0",
        stage=PromotionStage.PAPER_SHADOW,
        dataset_hash="abc",
    )
    assert (
        evaluate_promotion_gate(
            shadow_cand, PromotionStage.PAPER_APPROVED, shadow_fills=3
        ).passed
        is False
    )
    assert (
        evaluate_promotion_gate(
            shadow_cand, PromotionStage.PAPER_APPROVED, shadow_fills=5
        ).passed
        is True
    )


def test_live_canary_requires_operator_enablement() -> None:
    cand = StrategyCandidate(
        candidate_id="cand-6",
        name="Live Cand",
        version="1.0.0",
        stage=PromotionStage.PAPER_APPROVED,
        dataset_hash="abc",
    )
    # Default is not live enabled -> rejected
    assert (
        evaluate_promotion_gate(
            cand, PromotionStage.LIVE_CANARY, is_live_enabled=False
        ).passed
        is False
    )
    # With operator live enabled -> passed
    assert (
        evaluate_promotion_gate(
            cand, PromotionStage.LIVE_CANARY, is_live_enabled=True
        ).passed
        is True
    )


def test_champion_challenger_comparison() -> None:
    champ = _make_backtest(
        strategy="champ",
        net_return=Decimal("0.10"),
        max_drawdown=Decimal("0.15"),
        trade_count=15,
    )
    # Challenger with higher return and lower drawdown
    better_chall = _make_backtest(
        strategy="better",
        net_return=Decimal("0.18"),
        max_drawdown=Decimal("0.11"),
        trade_count=18,
    )
    verdict1 = compare_champion_challenger("c1", champ, "c2", better_chall)
    assert verdict1.challenger_wins is True

    # Challenger with lower return
    worse_return = _make_backtest(
        strategy="worse",
        net_return=Decimal("0.05"),
        max_drawdown=Decimal("0.10"),
        trade_count=15,
    )
    verdict2 = compare_champion_challenger("c1", champ, "c3", worse_return)
    assert verdict2.challenger_wins is False


@pytest.mark.anyio
async def test_execute_shadow_run_and_cli_workflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = AuditStore(tmp_path / "audit.sqlite")
    store.initialize()
    now = datetime(2024, 1, 1, 0, 0, tzinfo=UTC)
    dev_candles = [
        Candle(
            BTC_USD,
            SOURCE,
            60,
            now + timedelta(hours=i),
            now + timedelta(hours=i + 1),
            Decimal(100 + (i % 3) * 10),
            Decimal(135),
            Decimal(85),
            Decimal(100 + (i % 3) * 10),
            Decimal(10),
            5,
        )
        for i in range(10)
    ]
    store.record_candles(dev_candles)

    with TestClient(create_app(store), base_url="http://127.0.0.1") as api:
        rep = api.post(
            "/api/v1/backtests",
            json={"fast_period": 2, "slow_period": 3, "volume_fraction": "0.1"},
        )
        assert rep.is_success
        report_id = rep.headers["location"].rsplit("/", 1)[1]

    boundary = dev_candles[-1].closed_at
    forward_candles = [
        Candle(
            BTC_USD,
            SOURCE,
            60,
            boundary + timedelta(hours=i),
            boundary + timedelta(hours=i + 1),
            Decimal(100 + (i % 3) * 10),
            Decimal(135),
            Decimal(85),
            Decimal(100 + (i % 3) * 10),
            Decimal(10),
            5,
        )
        for i in range(30)
    ]
    store.record_candles(forward_candles)

    cand = StrategyCandidate(
        candidate_id="cand-shadow-test",
        name="SMA-Shadow",
        version="v1.0.0",
        stage=PromotionStage.PAPER_SHADOW,
        dataset_hash=rep.json()["dataset_sha256"],
        parameters={"fast": 2, "slow": 3, "backtest_report_id": report_id},
        created_at=boundary - timedelta(hours=1),
        updated_at=boundary,
    )
    store.save_strategy_candidate(cand)

    observed = run_forward_shadow_observation(store, cand.candidate_id)
    [(candidate_id, run_id, result)] = observed
    assert candidate_id == cand.candidate_id
    assert isinstance(result, BacktestResult)

    store = AuditStore(store.path)
    store.initialize()
    assert run_forward_shadow_observation(store, cand.candidate_id) == []
    assert execute_shadow_run(store, cand.candidate_id) == (run_id, result)
    latest_id, latest_report = get_latest_shadow_run(store, cand.candidate_id)
    assert latest_id == run_id
    assert latest_report == result

    history = list_shadow_runs_for_candidate(store, cand.candidate_id)
    assert len(history) == 1
    assert history[0][0] == run_id

    # 4. CLI strategy-shadow-run
    monkeypatch.setattr(
        "sys.argv",
        [
            "hcs-trader",
            "strategy-shadow-run",
            cand.candidate_id,
            "--data-dir",
            str(tmp_path),
        ],
    )
    launcher.main()
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["candidate_id"] == cand.candidate_id
    assert data["run_id"] == run_id

    # 5. CLI strategy-shadow-history
    monkeypatch.setattr(
        "sys.argv",
        [
            "hcs-trader",
            "strategy-shadow-history",
            cand.candidate_id,
            "--limit",
            "10",
            "--data-dir",
            str(tmp_path),
        ],
    )
    launcher.main()
    hist_out = capsys.readouterr().out
    hist_data = json.loads(hist_out)
    assert len(hist_data) == 1
    assert hist_data[0]["run_id"] == run_id
    with TestClient(create_app(store), base_url="http://127.0.0.1") as api:
        replay = api.get(f"/api/v1/strategies/candidates/{candidate_id}/shadow-run")
        assert replay.is_success, replay.text
        assert replay.json()["run_id"] == run_id
    async with Client(create_mcp_server(store.path)) as mcp:
        reply = await mcp.call_tool(
            "strategies.get_shadow_runs", {"candidate_id": candidate_id}
        )
        assert not reply.is_error
        [item] = reply.content
        assert item.type == "text"
        assert json.loads(item.text) == hist_data[0]
    assert store.paper_fills(limit=10) == []

    # 6. Now promote to PAPER_APPROVED using verified shadow replay
    updated, gate = promote_strategy_candidate(
        store, cand.candidate_id, PromotionStage.PAPER_APPROVED
    )
    assert updated.stage == PromotionStage.PAPER_APPROVED
    assert gate.passed is True
