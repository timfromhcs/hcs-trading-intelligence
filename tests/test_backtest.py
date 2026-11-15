import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter

from hcs_trader import __main__ as launcher
from hcs_trader.api import create_app
from hcs_trader.backtest import BacktestConfig, run_backtest
from hcs_trader.data.kraken import BTC_USD, SOURCE
from hcs_trader.domain.market_data import Candle
from hcs_trader.holdout import RESULT_JSON, freeze_candidate, score_holdout
from hcs_trader.storage import AuditStore
from hcs_trader.walk_forward import PeriodPair, WalkForwardConfig, walk_forward


def fixture_candles() -> list[Candle]:
    start = datetime(2020, 1, 1, tzinfo=UTC)
    result = []
    for index, value in enumerate((100, 110, 120, 130, 90, 80)):
        opened = start + timedelta(hours=index)
        price = Decimal(value)
        result.append(
            Candle(
                BTC_USD,
                SOURCE,
                60,
                opened,
                opened + timedelta(hours=1),
                price,
                price + 1,
                price - 1,
                price,
                Decimal(2),
                1,
            )
        )
    return result


def test_costs_partial_fills_and_next_open_without_lookahead() -> None:
    candles = fixture_candles()
    config = BacktestConfig(
        fast_period=2, slow_period=3, volume_fraction=Decimal("0.1")
    )
    result = run_backtest(candles, config)
    buy, sell = result.fills
    assert buy.side == "buy" and sell.side == "sell"
    assert buy.signal_at == candles[2].closed_at
    assert buy.filled_at == candles[3].opened_at
    assert buy.partial and buy.quantity == Decimal("0.2")
    assert buy.price == Decimal("130.13")
    assert buy.fee == Decimal("0.065065")
    assert sell.filled_at == candles[5].opened_at
    assert sell.price == Decimal("79.92")
    assert result.ending_quantity == 0
    assert result.ending_cash == result.ending_equity == Decimal("9989.852975")
    assert result.max_drawdown > 0
    assert result == run_backtest(candles, config)

    changed = candles[:3] + [
        replace(candles[3], close=Decimal(129), volume=Decimal(200))
    ]
    tampered_future = run_backtest(changed + candles[4:], config)
    assert tampered_future.fills[0] == buy
    assert tampered_future.dataset_sha256 != result.dataset_sha256


def test_backtest_rejects_gaps_and_enforces_minimums() -> None:
    candles = fixture_candles()
    config = BacktestConfig(fast_period=2, slow_period=3, min_notional=Decimal("5000"))
    assert run_backtest(candles, config).fills == ()
    with pytest.raises(ValueError, match="warm-up"):
        run_backtest(candles[:3], config)
    with pytest.raises(ValueError, match="contiguous"):
        run_backtest(candles[:2] + candles[3:], config)


def test_offline_backtest_route_validates_inputs(tmp_path: Path) -> None:
    store = AuditStore(tmp_path / "backtest.sqlite")
    store.initialize()
    store.record_candles(fixture_candles())
    with TestClient(create_app(store), base_url="http://127.0.0.1") as api:
        config = {"fast_period": 2, "slow_period": 3, "volume_fraction": "0.1"}
        report = api.post("/api/v1/backtests", json=config)
        assert report.status_code == 200
        assert report.json()["strategy"] == "sma-trend-1"
        assert len(report.json()["fills"]) == 2
        location = report.headers["location"]
        stored = api.get(location).json()
        assert stored["report"] == report.json()
        repeated = api.post("/api/v1/backtests", json=config)
        assert repeated.headers["location"] == location
        assert api.get(location).json()["recorded_at"] == stored["recorded_at"]
        assert api.get("/api/v1/backtests/" + "0" * 64).status_code == 404
        assert api.post("/api/v1/backtests?limit=2", json=config).status_code == 422
        assert (
            api.post("/api/v1/backtests", json={**config, "fee_bps": "-1"}).status_code
            == 422
        )


def test_walk_forward_keeps_test_and_holdout_out_of_selection(tmp_path: Path) -> None:
    hour = timedelta(hours=1)
    template = fixture_candles()[0]
    candles = [
        replace(
            template,
            opened_at=template.opened_at + index * hour,
            received_at=template.received_at + index * hour,
            volume=Decimal(20),
        )
        for index in range(16)
    ]
    config = WalkForwardConfig(6, 3, 4, (PeriodPair(2, 3), PeriodPair(2, 4)))
    result = walk_forward(candles, config)
    first, second = result.folds
    assert first.started_at == candles[6].opened_at
    assert first.ended_at == second.started_at == candles[9].opened_at
    assert second.ended_at == result.holdout.started_at
    assert result.holdout.evaluated_candles == 4
    assert result.holdout.selected == PeriodPair(2, 3)
    assert result.holdout.strategy.fills == ()
    (buy,) = result.holdout.benchmark.fills
    assert buy.filled_at == result.holdout.started_at

    altered = candles[:12] + [
        replace(candle, close=Decimal(101)) for candle in candles[12:]
    ]
    changed = walk_forward(altered, config)
    assert changed.folds == result.folds
    assert changed.holdout.selected == result.holdout.selected
    assert changed.holdout.training_return == result.holdout.training_return
    assert (
        changed.holdout.selection_dataset_sha256
        == result.holdout.selection_dataset_sha256
    )
    assert (
        changed.holdout.strategy.dataset_sha256
        != result.holdout.strategy.dataset_sha256
    )

    store = AuditStore(tmp_path / "history.sqlite")
    store.initialize()
    store.record_candles(candles)
    with TestClient(create_app(store), base_url="http://127.0.0.1") as api:
        response = api.post(
            "/api/v1/backtests/walk-forward",
            json={
                "training_candles": 6,
                "test_candles": 3,
                "holdout_candles": 4,
                "candidates": [{"fast": 2, "slow": 3}, {"fast": 2, "slow": 4}],
            },
        )
        assert response.status_code == 200
        assert response.json()["holdout"]["benchmark"]["strategy"] == "buy-and-hold"
        assert api.get(response.headers["location"]).json()["report"] == response.json()


def test_walk_forward_api_rejects_insufficient_history(tmp_path: Path) -> None:
    store = AuditStore(tmp_path / "walk-forward.sqlite")
    with TestClient(create_app(store), base_url="http://127.0.0.1") as api:
        assert api.post("/api/v1/backtests/walk-forward", json={}).status_code == 422


def test_backtest_reports_survive_restart_and_detect_corruption(tmp_path: Path) -> None:
    store = AuditStore(tmp_path / "report.sqlite")
    store.initialize()
    store.record_candles(fixture_candles())
    with TestClient(create_app(store), base_url="http://127.0.0.1") as api:
        response = api.post(
            "/api/v1/backtests", json={"fast_period": 2, "slow_period": 3}
        )
        location = response.headers["location"]
    with TestClient(
        create_app(AuditStore(store.path)), base_url="http://127.0.0.1"
    ) as api:
        assert api.get(location).json()["report"] == response.json()
        with closing(sqlite3.connect(store.path)) as connection, connection:
            connection.execute("UPDATE backtest_reports SET payload = '{}'")
        assert api.get(location).status_code == 503


def test_frozen_holdout_cli_enforces_commitment_and_keeps_first_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    candles = fixture_candles()
    development, holdout = candles[:4], candles[4:]
    holdout_file = tmp_path / "holdout.json"
    payload = TypeAdapter(list[Candle]).dump_json(holdout)
    commitment = sha256(payload).hexdigest()
    config_path = tmp_path / "config.json"
    config = WalkForwardConfig(4, 1, 2, (PeriodPair(2, 3),))
    config_path.write_bytes(TypeAdapter(WalkForwardConfig).dump_json(config))
    store = AuditStore(tmp_path / "research.sqlite")
    store.initialize()
    store.record_candles(development)
    monkeypatch.setattr(launcher, "local_store", lambda: AuditStore(store.path))

    launcher.freeze_research(commitment, 60, config_path)
    assert commitment in capsys.readouterr().out
    launcher.freeze_research(commitment, 60, config_path)
    capsys.readouterr()

    holdout_file.write_bytes(payload + b" ")
    with pytest.raises(ValueError, match="commitment"):
        launcher.score_research(commitment, holdout_file)
    holdout_file.write_bytes(payload)
    launcher.score_research(commitment, holdout_file)
    first = capsys.readouterr().out
    result = RESULT_JSON.validate_json(first)
    assert result.holdout_sha256 == commitment
    assert all(fill.filled_at >= holdout[0].opened_at for fill in result.strategy.fills)
    launcher.score_research(commitment, holdout_file)
    assert capsys.readouterr().out == first

    store.record_candles([holdout[0]])
    with pytest.raises(ValueError, match="different candidate"):
        launcher.freeze_research(commitment, 60, config_path)


def test_committed_overlapping_holdout_is_rejected() -> None:
    candles = fixture_candles()
    payload = TypeAdapter(list[Candle]).dump_json(candles[3:5])
    candidate = freeze_candidate(
        candles[:4],
        WalkForwardConfig(4, 1, 2, (PeriodPair(2, 3),)),
        sha256(payload).hexdigest(),
    )
    with pytest.raises(ValueError, match="end of the development"):
        score_holdout(candidate, payload)
