from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hcs_trader.api import create_app
from hcs_trader.data.kraken import BTC_USD, SOURCE
from hcs_trader.domain.market_data import Candle
from hcs_trader.indicators import calculate_indicators
from hcs_trader.storage import AuditStore

_START = datetime(2020, 1, 1, tzinfo=UTC)
_HOUR = timedelta(hours=1)


def series(values: list[int]) -> list[Candle]:
    return [
        Candle(
            BTC_USD,
            SOURCE,
            60,
            _START + index * _HOUR,
            _START + (index + 1) * _HOUR,
            Decimal(price),
            Decimal(price + 1),
            Decimal(price - 1),
            Decimal(price),
            Decimal(2),
            1,
        )
        for index, price in enumerate(values)
    ]


def test_indicator_warm_up_and_one_direction_series() -> None:
    assert calculate_indicators([]).closed_at is None
    rising = series(list(range(10, 45)))
    assert calculate_indicators(rising[:14]).rsi14 is None
    assert calculate_indicators(rising[:15]).rsi14 == 100
    assert calculate_indicators(rising[:19]).sma20 is None
    assert calculate_indicators(rising[:20]).volatility20 is None
    assert calculate_indicators(rising[:33]).macd is None
    result = calculate_indicators(rising)
    assert result.closed_at == rising[-1].closed_at
    assert result.sma20 == result.ema20 == Decimal("34.5")
    assert result.macd == result.macd_signal == Decimal(7)
    assert result.macd_histogram == 0
    assert result.atr14 == 2
    assert result.volume_sma20 == 2
    assert result.volatility20 is not None and result.volatility20 > 0
    assert calculate_indicators(series(list(range(40, 20, -1)))).rsi14 == 0


def test_flat_prices_and_data_gaps() -> None:
    candles = series([10] * 40)
    result = calculate_indicators(candles)
    assert result.rsi14 == 50
    assert result.bollinger_lower == result.bollinger_upper == 10
    assert result.volatility20 == 0
    gap = calculate_indicators(candles[:24] + candles[30:])
    assert gap.contiguous_candles == 10
    assert gap.ema20 is None and gap.rsi14 is None
    with pytest.raises(ValueError, match="non-overlapping"):
        calculate_indicators([candles[0], candles[0]])
    with pytest.raises(ValueError, match="one source"):
        calculate_indicators([candles[0], replace(candles[1], source="other")])


def test_indicator_api_reads_closed_history_offline(tmp_path: Path) -> None:
    store = AuditStore(tmp_path / "indicators.sqlite")
    store.initialize()
    store.record_candles(series(list(range(10, 45))))
    with TestClient(create_app(store), base_url="http://127.0.0.1") as api:
        result = api.get("/api/v1/market/indicators").json()
        assert result["rsi14"] == "100"
        assert result["source"] == SOURCE
        assert result["interval_minutes"] == 60
        assert datetime.fromisoformat(result["opened_at"]) == _START
        assert Decimal(result["ema20"]) == Decimal("34.5")
        assert result["contiguous_candles"] == 35
        assert api.get("/api/v1/market/indicators?interval=5").json()["rsi14"] is None
        assert api.get("/api/v1/market/indicators?interval=7").status_code == 422
