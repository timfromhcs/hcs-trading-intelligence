from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from hcs_trader.backtest import BacktestConfig, analyze_backtest, run_backtest
from hcs_trader.data.kraken import BTC_USD, SOURCE
from hcs_trader.domain.market_data import Candle


def trend_candles(prices: list[int]) -> list[Candle]:
    start = datetime(2020, 1, 1, tzinfo=UTC)
    out = []
    for index, value in enumerate(prices):
        opened = start + timedelta(hours=index)
        price = Decimal(value)
        out.append(
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
                Decimal(1000),
                1,
            )
        )
    return out


_WAVE = [100 + (i * 7) % 23 + i // 2 for i in range(120)]


def test_analysis_reports_extended_metrics_and_matches_plain_run() -> None:
    candles = trend_candles(_WAVE)
    config = BacktestConfig(fast_period=3, slow_period=8)
    analysis = analyze_backtest(candles, config, bootstrap_iterations=50)
    assert analysis.result == run_backtest(candles, config)
    metrics = analysis.metrics
    assert metrics.periods == len(candles) - config.slow_period
    assert metrics.trade_count > 0
    assert 0 <= metrics.exposure <= 1 and metrics.turnover > 0
    assert metrics.total_fees == analysis.result.total_fees
    assert metrics.total_slippage > 0
    assert metrics.worst_period_return <= 0
    assert metrics.excess_return == (
        analysis.result.net_return - analysis.benchmark.net_return
    )
    assert metrics.expectancy is not None and metrics.win_rate is not None
    assert metrics.sharpe is not None and metrics.sortino is not None
    boot = analysis.bootstrap
    assert boot is not None and boot.p05 <= boot.median <= boot.p95
    assert 0 <= boot.loss_probability <= 1
    assert analyze_backtest(candles, config, bootstrap_iterations=50) == analysis


def test_analysis_with_no_trades_has_none_metrics() -> None:
    candles = trend_candles([100, 110, 120, 130, 90, 80])
    config = BacktestConfig(fast_period=2, slow_period=3, min_notional=Decimal("5e11"))
    analysis = analyze_backtest(candles, config)
    assert analysis.metrics.trade_count == 0
    assert analysis.metrics.expectancy is None and analysis.metrics.win_rate is None
    assert analysis.metrics.exposure == 0
    assert analysis.metrics.sharpe is None and analysis.bootstrap is None


def test_stop_loss_fills_at_gap_open_not_stop_price() -> None:
    prices = [100] * 4 + [101, 102, 103, 104, 50, 49, 48, 47]
    candles = trend_candles(prices)
    config = BacktestConfig(
        fast_period=2,
        slow_period=3,
        slippage_bps=Decimal(0),
        spread_bps=Decimal(0),
        fee_bps=Decimal(0),
        allocation=Decimal("0.5"),
    )
    stopped = analyze_backtest(candles, config, stop_loss_pct=Decimal("0.10"))
    sells = [f for f in stopped.result.fills if f.side == "sell"]
    assert sells and sells[0].filled_at == candles[8].opened_at
    assert sells[0].price == Decimal(50)  # the gap open, far below the stop
    with pytest.raises(ValueError, match="stop_loss_pct"):
        analyze_backtest(candles, config, stop_loss_pct=Decimal(1))
    with pytest.raises(ValueError, match="bootstrap_iterations"):
        analyze_backtest(candles, config, bootstrap_iterations=10_001)


def test_intrabar_stop_fills_at_stop_price() -> None:
    prices = [100] * 4 + [101, 102, 103, 104]
    candles = trend_candles(prices)
    crash = candles[-1]
    candles.append(
        Candle(
            BTC_USD,
            SOURCE,
            60,
            crash.closed_at,
            crash.closed_at + timedelta(hours=1),
            Decimal(104),
            Decimal(105),
            Decimal(60),
            Decimal(70),
            Decimal(1000),
            1,
        )
    )
    config = BacktestConfig(
        fast_period=2,
        slow_period=3,
        slippage_bps=Decimal(0),
        spread_bps=Decimal(0),
        fee_bps=Decimal(0),
    )
    analysis = analyze_backtest(candles, config, stop_loss_pct=Decimal("0.10"))
    sell = [f for f in analysis.result.fills if f.side == "sell"][0]
    entry = [f for f in analysis.result.fills if f.side == "buy"][0].price
    assert sell.price == entry * Decimal("0.9")
