from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hcs_trader import __main__ as launcher
from hcs_trader.api import create_app
from hcs_trader.data.kraken import BTC_USD, SOURCE, KrakenQuoteProvider
from hcs_trader.domain.assets import MarketId
from hcs_trader.domain.market_data import Candle
from hcs_trader.storage import AuditStore


def test_import_is_idempotent_and_candles_are_readable_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    opened = datetime(2026, 1, 1, tzinfo=UTC)
    first = Candle(
        BTC_USD,
        SOURCE,
        60,
        opened,
        opened + timedelta(hours=3),
        Decimal("10.000"),
        Decimal("12"),
        Decimal("9"),
        Decimal("10.0001"),
        Decimal("0.00000001"),
        3,
    )
    second = replace(first, opened_at=opened + timedelta(hours=2))
    store = AuditStore(tmp_path / "history.sqlite")

    async def fetch(
        provider: KrakenQuoteProvider, market: MarketId, interval: int
    ) -> list[Candle]:
        assert market == BTC_USD and interval == 60
        return [first, second]

    monkeypatch.setattr(KrakenQuoteProvider, "get_candles", fetch)
    monkeypatch.setattr(launcher, "local_store", lambda: store)
    monkeypatch.setattr(
        "sys.argv", ["hcs-trader", "import-candles", "--interval", "60"]
    )
    launcher.main()
    assert "Imported 2" in capsys.readouterr().out
    launcher.main()
    assert "Imported 0" in capsys.readouterr().out

    reopened = AuditStore(store.path)
    with TestClient(create_app(reopened), base_url="http://127.0.0.1") as client:
        assert client.get("/api/v1/system/status").json()["network_enabled"] is False
        candles = client.get("/api/v1/market/candles").json()
        assert len(candles) == 2
        assert candles[0]["open"] == "10.000"
        assert candles[0]["close"] == "10.0001"
        assert candles[0]["volume"] == "1E-8"
        assert candles[1]["opened_at"] == "2026-01-01T02:00:00Z"
        assert client.get("/api/v1/market/candles?limit=1").json() == candles[1:]
        assert client.get("/api/v1/market/candles?interval=5").json() == []
        assert client.get("/api/v1/market/candles?interval=7").status_code == 422
