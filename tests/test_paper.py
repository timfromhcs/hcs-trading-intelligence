from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hcs_trader.api import create_app
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
