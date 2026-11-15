from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hcs_trader.api import create_app
from hcs_trader.backtest import BacktestConfig, run_backtest
from hcs_trader.data.kraken import BTC_USD, SOURCE
from hcs_trader.domain.market_data import Candle
from hcs_trader.storage import AuditStore


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
        assert api.post("/api/v1/backtests?limit=2", json=config).status_code == 422
        assert (
            api.post("/api/v1/backtests", json={**config, "fee_bps": "-1"}).status_code
            == 422
        )
