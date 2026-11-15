from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hcs_trader import __main__ as launcher
from hcs_trader.api import create_app
from hcs_trader.data.kraken import BTC_USD
from hcs_trader.data.quotes import QuoteService
from hcs_trader.domain.assets import MarketId
from hcs_trader.domain.market_data import Quote
from hcs_trader.domain.paper import PaperConflict, PaperOrder, PaperRejected, PaperSide
from hcs_trader.storage import AuditStore


def test_paper_balance_cost_basis_fees_and_realized_pnl_survive_restart(
    tmp_path: Path,
) -> None:
    store = AuditStore(tmp_path / "paper.sqlite")
    store.initialize()
    store.record_paper_fill(
        PaperOrder("buy-1", PaperSide.BUY, Decimal(".4")), Decimal(1000), Decimal(2)
    )
    store.record_paper_fill(
        PaperOrder("buy-2", PaperSide.BUY, Decimal(".6")), Decimal(1000), Decimal(2)
    )
    sale = PaperOrder("sell-1", PaperSide.SELL, Decimal(".5"))
    filled = store.record_paper_fill(sale, Decimal(1100), Decimal(3))
    assert filled.balance.cash == Decimal(9543)
    assert filled.balance.quantity == Decimal(".5")
    assert filled.balance.cost_basis == Decimal(502)
    assert filled.balance.fees == Decimal(7)
    assert filled.balance.realized_pnl == Decimal(45)

    store = AuditStore(store.path)
    assert store.record_paper_fill(sale, Decimal(1200), Decimal(4)) == filled
    with pytest.raises(PaperConflict):
        store.record_paper_fill(
            PaperOrder("sell-1", PaperSide.SELL, Decimal(".4")),
            Decimal(1100),
            Decimal(3),
        )
    with pytest.raises(PaperRejected, match="Insufficient paper BTC"):
        store.record_paper_fill(
            PaperOrder("sell-2", PaperSide.SELL, Decimal(1)), Decimal(1100), Decimal(3)
        )
    with TestClient(create_app(store), base_url="http://127.0.0.1") as api:
        summary = api.get("/api/v1/portfolio/summary").json()
        assert summary["mode"] == "PAPER"
        assert Decimal(summary["balance"]["realized_pnl"]) == 45
        assert len(api.get("/api/v1/paper/fills").json()) == 3
        assert api.get("/api/v1/paper/fills?limit=101").status_code == 422


def test_parallel_retries_book_one_fill_and_competing_orders_cannot_overdraw(
    tmp_path: Path,
) -> None:
    store = AuditStore(tmp_path / "paper.sqlite")
    store.initialize()
    order = PaperOrder("same", PaperSide.BUY, Decimal(1))
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [
            pool.submit(store.record_paper_fill, order, Decimal(6000), Decimal(2))
            for _ in range(8)
        ]
        results = [future.result() for future in futures]
        assert all(item == results[0] for item in results)
        others = [
            pool.submit(
                store.record_paper_fill,
                PaperOrder(f"competing-{index}", PaperSide.BUY, Decimal(1)),
                Decimal(3000),
                Decimal(0),
            )
            for index in range(2)
        ]
        rejections = [future.exception() for future in others]
    assert sum(isinstance(error, PaperRejected) for error in rejections) == 1
    assert store.paper_balance().cash == Decimal(998)
    assert len(store.paper_fills(limit=100)) == 2


class QuoteFixture:
    def __init__(self, age: int = 0, ask: str = "50100") -> None:
        self.age = age
        self.ask = Decimal(ask)
        self.calls = 0

    async def get_quote(self, market: MarketId) -> Quote:
        self.calls += 1
        received = datetime.now(UTC)
        stamp = received - timedelta(seconds=self.age)
        return Quote(
            market,
            "fixture",
            Decimal("50000"),
            self.ask,
            stamp,
            stamp,
            received,
        )


def test_paper_order_requires_opt_in_and_books_the_verified_quote(
    tmp_path: Path,
) -> None:
    store = AuditStore(tmp_path / "paper.sqlite")
    request = {"idempotency_key": "verified-quote", "side": "buy", "quantity": "0.01"}
    provider = QuoteFixture()
    with TestClient(
        create_app(store, quote_service=QuoteService(provider, BTC_USD)),
        base_url="http://127.0.0.1:8765",
    ) as api:
        assert api.post("/api/v1/orders/paper", json=request).status_code == 403
        assert provider.calls == 0

    with TestClient(
        create_app(
            store, quote_service=QuoteService(provider, BTC_USD), paper_mode=True
        ),
        base_url="http://127.0.0.1:8765",
    ) as api:
        assert api.get("/api/v1/system/capabilities").json()["paper_orders"] is True
        assert api.get("/api/v1/system/status").json()["mode"] == "PAPER"
        forbidden = api.post(
            "/api/v1/orders/paper",
            json=request,
            headers={"Origin": "https://example.invalid"},
        )
        assert forbidden.status_code == 403 and provider.calls == 0
        fill = api.post("/api/v1/orders/paper", json=request)
        assert fill.status_code == 200
        assert fill.json()["price"] == "50100"
        assert Decimal(fill.json()["fee"]) == Decimal("1.2525")
        assert fill.json()["quote"]["source"] == "fixture"
        assert api.get("/api/v1/system/capabilities").json()["live_orders"] is False

    stale_provider = QuoteFixture(age=31)
    with TestClient(
        create_app(
            store, quote_service=QuoteService(stale_provider, BTC_USD), paper_mode=True
        ),
        base_url="http://127.0.0.1:8765",
    ) as api:
        assert api.post("/api/v1/orders/paper", json=request).json() == fill.json()
        assert stale_provider.calls == 0
        rejected = api.post(
            "/api/v1/orders/paper", json={**request, "idempotency_key": "stale"}
        )
        assert rejected.status_code == 409
        assert "fresh" in rejected.json()["detail"]
        assert len(api.get("/api/v1/paper/fills").json()) == 1


@pytest.mark.parametrize(
    ("ask", "quantity", "reason"),
    [("51000", "0.01", "spread"), ("50100", "0.1", "notional")],
)
def test_paper_order_enforces_simulation_limits(
    tmp_path: Path, ask: str, quantity: str, reason: str
) -> None:
    store = AuditStore(tmp_path / "paper.sqlite")
    quotes = QuoteService(QuoteFixture(ask=ask), BTC_USD)
    with TestClient(
        create_app(store, quote_service=quotes, paper_mode=True),
        base_url="http://127.0.0.1",
    ) as api:
        response = api.post(
            "/api/v1/orders/paper",
            json={"idempotency_key": "limits", "side": "buy", "quantity": quantity},
        )
        assert response.status_code == 409
        assert reason in response.json()["detail"]
        assert api.get("/api/v1/paper/fills").json() == []


def test_persistent_pause_blocks_orders_until_explicit_local_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = AuditStore(tmp_path / "paused.sqlite")
    monkeypatch.setattr(launcher, "local_store", lambda: AuditStore(store.path))
    monkeypatch.setattr("sys.argv", ["hcs-trader", "pause-execution"])
    launcher.main()
    assert "paused" in capsys.readouterr().out
    first_pause = store.execution_pause()
    assert first_pause.paused and first_pause.changed_at is not None
    launcher.main()
    assert store.execution_pause() == first_pause
    event = store.latest_event()
    assert event is not None and event.kind == "execution.paused"
    provider = QuoteFixture()
    app = create_app(
        store, quote_service=QuoteService(provider, BTC_USD), paper_mode=True
    )
    order = {"idempotency_key": "pause-test", "side": "buy", "quantity": "0.01"}
    with TestClient(app, base_url="http://127.0.0.1") as api:
        assert api.get("/api/v1/risk/status").json()["paused"] is True
        assert api.get("/api/v1/system/status").json()["execution_available"] is False
        rejected = api.post("/api/v1/orders/paper", json=order)
        assert rejected.status_code == 409
        assert rejected.json()["detail"] == "Execution is paused"
        assert provider.calls == 0
        monkeypatch.setattr("sys.argv", ["hcs-trader", "resume-execution"])
        launcher.main()
        assert api.get("/api/v1/risk/status").json()["paused"] is False
        assert api.post("/api/v1/orders/paper", json=order).status_code == 200


def test_pause_during_quote_refresh_blocks_financial_side_effect(
    tmp_path: Path,
) -> None:
    store = AuditStore(tmp_path / "paused.sqlite")

    class PausingProvider(QuoteFixture):
        async def get_quote(self, market: MarketId) -> Quote:
            store.set_execution_pause(True)
            return await super().get_quote(market)

    app = create_app(
        store, quote_service=QuoteService(PausingProvider(), BTC_USD), paper_mode=True
    )
    with TestClient(app, base_url="http://127.0.0.1") as api:
        rejected = api.post(
            "/api/v1/orders/paper",
            json={
                "idempotency_key": "paused-in-flight",
                "side": "buy",
                "quantity": "0.01",
            },
        )
        assert rejected.status_code == 409
        assert rejected.json()["detail"] == "Execution is paused"
        assert api.get("/api/v1/paper/fills").json() == []
