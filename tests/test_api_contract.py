"""REST contract for AGENTS.md 9.2 and parity with the shared application service."""

import ast
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from hcs_trader.api import create_app
from hcs_trader.application import ApplicationService, chain_capability_matrix
from hcs_trader.data.kraken import BTC_USD, SOURCE
from hcs_trader.domain.execution import SpotOrder, SpotOrderType, SpotSide
from hcs_trader.domain.lifecycle import OrderState
from hcs_trader.domain.market_data import Quote
from hcs_trader.domain.news import NewsItem
from hcs_trader.domain.paper import PaperOrder, PaperSide
from hcs_trader.domain.signing import signing_capabilities
from hcs_trader.domain.wallet import WalletBalance
from hcs_trader.promotion import PromotionStage, StrategyCandidate
from hcs_trader.storage import AuditStore

SRC = Path(__file__).resolve().parents[1] / "src" / "hcs_trader"
ADDRESS = "0x1234567890123456789012345678901234567890"

# AGENTS.md 9.2, with the implemented paths for the two documented deviations.
SPEC_ROUTES: set[tuple[str, str]] = {
    ("get", "/api/v1/health"),
    ("get", "/api/v1/system/status"),
    ("get", "/api/v1/system/capabilities"),
    ("get", "/api/v1/market/assets"),
    ("get", "/api/v1/market/quotes"),
    ("get", "/api/v1/market/candles"),
    ("get", "/api/v1/market/orderbook"),
    ("get", "/api/v1/news/search"),
    ("get", "/api/v1/news/events"),
    ("get", "/api/v1/macro/series"),
    ("get", "/api/v1/portfolio/summary"),
    ("get", "/api/v1/portfolio/positions"),
    ("get", "/api/v1/wallets"),
    ("get", "/api/v1/wallets/{wallet_id}/balances"),
    ("get", "/api/v1/wallets/{wallet_id}/transactions"),
    ("get", "/api/v1/strategies"),
    ("get", "/api/v1/strategies/{strategy_id}/metrics"),
    ("post", "/api/v1/backtests"),
    ("get", "/api/v1/backtests/{report_id}"),
    ("post", "/api/v1/orders/preview"),
    ("post", "/api/v1/orders/paper"),
    ("post", "/api/v1/orders/live"),
    ("get", "/api/v1/orders/{order_id}"),
    ("post", "/api/v1/system/pause"),
    ("post", "/api/v1/system/resume"),
    ("get", "/api/v1/risk/status"),
    ("get", "/api/v1/audit/events"),
}


@pytest.fixture
def store(tmp_path: Path) -> AuditStore:
    store = AuditStore(tmp_path / "contract.sqlite")
    store.initialize()
    return store


@pytest.fixture
def client(store: AuditStore) -> Any:
    with TestClient(create_app(store), base_url="http://127.0.0.1") as api:
        yield api


def _fresh_quote() -> Quote:
    now = datetime.now(UTC)
    return Quote(
        BTC_USD,
        SOURCE,
        Decimal("50000.00"),
        Decimal("50010.00"),
        now,
        now,
        now,
    )


def test_every_brief_endpoint_exists_with_a_typed_response(store: AuditStore) -> None:
    schema = create_app(store).openapi()
    present = {
        (method, path) for path, ops in schema["paths"].items() for method in ops
    }
    assert SPEC_ROUTES - present == set()
    for method, path in SPEC_ROUTES:
        ok = schema["paths"][path][method]["responses"]
        success = next(code for code in ok if code.startswith("2"))
        content = ok[success].get("content", {})
        assert content, f"{method} {path} has no declared response schema"


def test_capabilities_and_chain_matrix_match_the_signer_report(client: Any) -> None:
    capabilities = client.get("/api/v1/system/capabilities").json()
    chains = client.get("/api/v1/system/chains").json()
    assert capabilities["chains"] == chains
    assert capabilities["live_orders"] is False
    assert [row["symbol"] for row in chains] == ["BTC", "ETH", "SOL", "LTC", "DGB"]
    signing = signing_capabilities()
    for row in chains:
        report = signing[row["signing_namespace"]]
        assert row["signing_chain_valid"] is report.chain_valid
        assert row["signing_note"] == report.note
        assert row["live_execution_enabled"] is False
        assert row["direct_onchain_execution"] is False
    assert chains == [c.model_dump(mode="json") for c in chain_capability_matrix()]


def test_read_endpoints_return_stored_data(store: AuditStore, client: Any) -> None:
    now = datetime.now(UTC)
    store.record_quote(_fresh_quote())
    store.record_news(
        [NewsItem("ecb-press", "https://example.org/a", "Rate decision", now, now)]
    )
    assert [a["symbol"] for a in client.get("/api/v1/market/assets").json()] == [
        "BTC",
        "ETH",
        "SOL",
        "LTC",
        "DGB",
        "USD",
    ]
    book = client.get("/api/v1/market/orderbook").json()
    assert book["quality"] == "FRESH"
    assert book["depth_levels"] == 1
    assert book["bids"] == [{"price": "50000.00"}]
    assert Decimal(book["spread"]) == Decimal("10.00")
    hits = client.get("/api/v1/news/search", params={"q": "RATE"}).json()
    assert [h["title"] for h in hits] == ["Rate decision"]
    assert client.get("/api/v1/news/search", params={"q": "nothing"}).json() == []
    assert client.get("/api/v1/news/search").status_code == 422
    assert client.get("/api/v1/portfolio/positions").json() == []


def test_orderbook_without_quotes_reports_missing(client: Any) -> None:
    book = client.get("/api/v1/market/orderbook").json()
    assert book["quality"] == "MISSING"
    assert book["bids"] == [] and book["asks"] == []


def test_wallet_balance_and_transaction_endpoints(
    store: AuditStore, client: Any
) -> None:
    store.save_wallet_snapshot(
        WalletBalance(ADDRESS, "ETH", "5", 18, 10, "0xab", datetime.now(UTC))
    )
    balances = client.get(f"/api/v1/wallets/{ADDRESS}/balances").json()
    assert [(b["asset"], b["base_units"]) for b in balances] == [("ETH", "5")]
    history = client.get(f"/api/v1/wallets/{ADDRESS}/transactions").json()
    assert history["available"] is False and history["transactions"] == []
    assert client.get("/api/v1/wallets/0xunknownaddress/balances").status_code == 404
    assert client.get("/api/v1/wallets/bad!/balances").status_code == 422
    assert client.get("/api/v1/wallets/bad!/transactions").status_code == 422


def _candidate(store: AuditStore) -> StrategyCandidate:
    now = datetime.now(UTC)
    candidate = StrategyCandidate(
        candidate_id="cand-contract",
        name="SMA",
        version="1.0.0",
        stage=PromotionStage.RESEARCH,
        parameters={},
        dataset_hash="h",
        created_at=now,
        updated_at=now,
    )
    store.save_strategy_candidate(candidate)
    return candidate


def test_strategy_list_and_metrics(store: AuditStore, client: Any) -> None:
    assert client.get("/api/v1/strategies").json() == []
    _candidate(store)
    listed = client.get("/api/v1/strategies").json()
    assert [row["candidate_id"] for row in listed] == ["cand-contract"]
    metrics = client.get("/api/v1/strategies/cand-contract/metrics").json()
    assert metrics["candidate"]["candidate_id"] == "cand-contract"
    assert metrics["shadow_run_id"] is None and metrics["net_return"] is None
    assert client.get("/api/v1/strategies/missing/metrics").status_code == 404
    assert client.get("/api/v1/strategies/bad id/metrics").status_code == 422


def test_order_preview_has_no_side_effects_and_uses_submission_rules(
    store: AuditStore, client: Any
) -> None:
    body = {"side": "buy", "quantity": "0.01"}
    audit_before = len(store.list_audit_events(limit=100))
    stale = client.post("/api/v1/orders/preview", json=body).json()
    assert stale["would_fill"] is False
    assert "No BTC/USD quote is available" in stale["reasons"]
    store.record_quote(_fresh_quote())
    ok = client.post("/api/v1/orders/preview", json=body).json()
    assert ok["would_fill"] is True
    assert Decimal(ok["price"]) == Decimal("50010.00")
    assert Decimal(ok["cash_after"]) < Decimal("10000")
    too_large = client.post(
        "/api/v1/orders/preview", json={"side": "buy", "quantity": "10"}
    ).json()
    assert too_large["would_fill"] is False
    store.set_execution_pause(True)
    paused = client.post("/api/v1/orders/preview", json=body).json()
    assert paused["would_fill"] is False and paused["execution_paused"] is True
    assert store.paper_fills(limit=5) == []
    assert store.paper_balance().cash == Decimal(10000)
    assert len(store.list_audit_events(limit=100)) == audit_before + 1  # pause only


def test_order_preview_validates_input_and_origin(client: Any) -> None:
    assert (
        client.post(
            "/api/v1/orders/preview", json={"side": "short", "quantity": "1"}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/orders/preview", json={"side": "buy", "quantity": "-1"}
        ).status_code
        == 422
    )
    foreign = client.post(
        "/api/v1/orders/preview",
        json={"side": "buy", "quantity": "1"},
        headers={"origin": "http://evil.example"},
    )
    assert foreign.status_code == 403


def test_get_order_resolves_spot_and_paper_orders(
    store: AuditStore, client: Any
) -> None:
    now = datetime.now(UTC)
    store.save_spot_order(
        SpotOrder(
            client_order_id="cl-contract-1",
            order_id="ord-contract-1",
            venue="kraken",
            market="BTC/USD",
            side=SpotSide.BUY,
            order_type=SpotOrderType.LIMIT,
            quantity=Decimal("0.01"),
            limit_price=Decimal("50000"),
            state=OrderState.ACKNOWLEDGED,
            risk_decision_id="risk-1",
            created_at=now,
            updated_at=now,
        )
    )
    store.record_paper_fill(
        PaperOrder("paper-contract-1", PaperSide.BUY, Decimal("0.01")),
        Decimal("50010"),
        Decimal("1.25"),
    )
    spot = client.get("/api/v1/orders/cl-contract-1").json()
    assert spot["kind"] == "spot" and spot["state"] == "ACKNOWLEDGED"
    paper = client.get("/api/v1/orders/paper-contract-1").json()
    assert paper["kind"] == "paper" and paper["price"] == "50010"
    assert client.get("/api/v1/orders/nope").status_code == 404
    assert client.get("/api/v1/orders/bad%20id").status_code == 422
    # The static spot listing is still reachable next to the dynamic route.
    assert client.get("/api/v1/orders/spot").status_code == 200


def test_pause_and_resume_aliases_share_the_risk_path(
    store: AuditStore, client: Any
) -> None:
    assert client.post("/api/v1/system/pause").json()["paused"] is True
    assert client.get("/api/v1/risk/status").json()["paused"] is True
    assert client.post("/api/v1/system/resume").json()["paused"] is False
    assert client.post("/api/v1/risk/pause").json()["paused"] is True
    assert client.post("/api/v1/risk/resume").json()["paused"] is False
    foreign = client.post(
        "/api/v1/system/resume", headers={"origin": "http://evil.example"}
    )
    assert foreign.status_code == 403
    assert client.get("/api/v1/risk/policy").json()["leverage_allowed"] is False


def test_live_alias_is_gated_like_the_spot_path(client: Any) -> None:
    body = {
        "side": "buy",
        "quantity": "0.001",
        "limit_price": "50000",
        "candidate_id": "x",
        "strategy_version": "v1.0.0",
    }
    assert client.post("/api/v1/orders/live", json=body).status_code == 403
    assert client.post("/api/v1/orders/spot", json=body).status_code == 403


def test_backtest_endpoint_and_service_produce_the_same_report(
    store: AuditStore, client: Any
) -> None:
    from hcs_trader.backtest import BacktestConfig  # noqa: PLC0415
    from hcs_trader.domain.market_data import Candle  # noqa: PLC0415

    start = datetime(2024, 1, 1, tzinfo=UTC)
    candles = []
    for index in range(30):
        opened = start + timedelta(hours=index)
        price = Decimal(100 + index)
        candles.append(
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
    store.record_candles(candles)
    config = {"fast_period": 2, "slow_period": 5}
    response = client.post("/api/v1/backtests", json=config, params={"limit": 30})
    assert response.status_code == 200
    report_id = response.headers["location"].rsplit("/", 1)[1]
    run = ApplicationService(store).run_backtest(
        BacktestConfig(fast_period=2, slow_period=5), limit=30, persist=False
    )
    assert run.report_id == report_id
    stored = client.get(f"/api/v1/backtests/{report_id}")
    assert stored.status_code == 200
    bad = client.post("/api/v1/backtests", json={"fast_period": 9, "slow_period": 3})
    assert bad.status_code == 422
    assert (
        client.get("/api/v1/market/candles", params={"interval": 7}).status_code == 422
    )


def test_candidate_creation_goes_through_the_service(client: Any) -> None:
    created = client.post(
        "/api/v1/strategies/candidates",
        json={"name": "SMA", "version": "1", "dataset_hash": "h", "parameters": {}},
    )
    assert created.status_code == 200
    assert created.json()["stage"] == "RESEARCH"
    assert [r["name"] for r in client.get("/api/v1/strategies").json()] == ["SMA"]


# --- layering (AGENTS.md 3.4) ---


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text("utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


def test_risk_modules_import_no_model_network_or_transport_code() -> None:
    banned_prefixes = (
        "httpx",
        "requests",
        "aiohttp",
        "mcp",
        "fastapi",
        "hcs_trader.api",
        "hcs_trader.mcp_server",
        "hcs_trader.local_research",
        "hcs_trader.kraken_spot",
        "hcs_trader.data",
        "hcs_trader.application",
    )
    for path in (SRC / "risk.py", SRC / "portfolio_state.py"):
        offenders = [m for m in _imports(path) if m.startswith(banned_prefixes)]
        assert offenders == [], f"{path.name}: {offenders}"


def test_application_layer_does_not_depend_on_transports_or_signing() -> None:
    for path in (SRC / "application").glob("*.py"):
        offenders = [
            m
            for m in _imports(path)
            if m.startswith(
                (
                    "fastapi",
                    "mcp",
                    "hcs_trader.api",
                    "hcs_trader.mcp_server",
                    "hcs_trader.signing",
                    "hcs_trader.keystore",
                )
            )
        ]
        assert offenders == [], f"{path.name}: {offenders}"


def test_transports_do_not_duplicate_sql_access_for_shared_use_cases() -> None:
    # The shared use cases live in application/; transports must not re-read the
    # candle table or write the paper ledger directly.
    for name in ("api.py", "mcp_server.py"):
        source = (SRC / name).read_text("utf-8")
        assert "store.candle_history(" not in source, name
        assert "record_paper_fill(" not in source, name
