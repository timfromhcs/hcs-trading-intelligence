"""Read-only REST preview of the shared cost model."""

from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from hcs_trader.api import create_app
from hcs_trader.storage import AuditStore


def _client(tmp_path: Path) -> TestClient:
    store = AuditStore(tmp_path / "audit.sqlite")
    store.initialize()
    return TestClient(create_app(store), base_url="http://127.0.0.1")


def test_cost_preview_uses_exact_decimals(tmp_path: Path) -> None:
    client = _client(tmp_path)
    response = client.post(
        "/api/v1/quant/costs",
        json={
            "fee_bps": "25",
            "spread_bps": "10",
            "slippage_bps": "5",
            "reference_price": "100",
            "side": "buy",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert Decimal(body["execution_price"]) == Decimal("100.1")
    assert Decimal(body["fee_rate"]) == Decimal("0.0025")
    assert body["authorization"] == "NONE"
    assert Decimal(body["fee_on_notional"]) > 0


def test_cost_preview_rejects_invalid_bps_and_side(tmp_path: Path) -> None:
    client = _client(tmp_path)
    bad_fee = client.post(
        "/api/v1/quant/costs",
        json={
            "fee_bps": "501",
            "reference_price": "100",
            "side": "buy",
        },
    )
    assert bad_fee.status_code == 422
    bad_side = client.post(
        "/api/v1/quant/costs",
        json={"reference_price": "100", "side": "short"},
    )
    assert bad_side.status_code == 422
