"""Tests for KrakenSpotAdapter, order lifecycle transitions and SQLite persistence."""

import asyncio
import base64
import hashlib
import hmac
import json
import urllib.parse
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx2
import pytest
import uvicorn
from fastapi.testclient import TestClient

from hcs_trader import __main__ as launcher
from hcs_trader.api import create_app
from hcs_trader.data.kraken import BTC_USD, SOURCE, KrakenQuoteProvider
from hcs_trader.data.quotes import QuoteService
from hcs_trader.domain.execution import SpotOrder, SpotOrderType, SpotSide
from hcs_trader.domain.lifecycle import OrderState
from hcs_trader.domain.market_data import Quote
from hcs_trader.kraken_spot import KrakenSpotAdapter
from hcs_trader.promotion import PromotionStage, StrategyCandidate
from hcs_trader.risk import RiskEngine, RiskPolicy, RiskRejected
from hcs_trader.spot_execution import KrakenSpotExecution
from hcs_trader.storage import AuditStore

# Synthetic test credentials
_API_KEY = "test-api-key-12345"
_API_SECRET = base64.b64encode(b"01234567890123456789012345678901").decode("ascii")


def _sample_order(
    client_id: str = "cl-ord-001",
    quantity: str = "0.01",
    side: SpotSide = SpotSide.BUY,
    limit_price: str | None = "50000.00",
    state: OrderState = OrderState.PREVIEWED,
    venue_order_id: str | None = None,
) -> SpotOrder:
    now = datetime.now(UTC)
    return SpotOrder(
        client_order_id=client_id,
        order_id="ord-001",
        venue="kraken",
        market="BTC/USD",
        side=side,
        order_type=SpotOrderType.LIMIT if limit_price else SpotOrderType.MARKET,
        quantity=Decimal(quantity),
        limit_price=Decimal(limit_price) if limit_price else None,
        state=state,
        risk_decision_id="risk-001",
        created_at=now,
        updated_at=now,
        venue_order_id=venue_order_id,
    )


def test_kraken_signature_computation() -> None:
    client = httpx2.AsyncClient()
    adapter = KrakenSpotAdapter(_API_KEY, _API_SECRET, client)
    path = "/0/private/AddOrder"
    nonce = 1600000000000000
    data = {"pair": "XXBTZUSD", "type": "buy", "ordertype": "limit"}

    headers = adapter._sign_request(path, data, nonce)
    assert headers["API-Key"] == _API_KEY

    # Independently verify HMAC-SHA512
    postdata = urllib.parse.urlencode(data)
    encoded = (str(nonce) + postdata).encode()
    message = path.encode() + hashlib.sha256(encoded).digest()
    secret_bytes = base64.b64decode(_API_SECRET)
    mac = hmac.new(secret_bytes, message, hashlib.sha512)
    expected_sig = base64.b64encode(mac.digest()).decode()
    assert headers["API-Sign"] == expected_sig


@pytest.mark.anyio
async def test_submit_spot_order_success() -> None:
    now = datetime.now(UTC)
    order = _sample_order()

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/0/private/AddOrder"
        return httpx2.Response(
            200,
            json={
                "error": [],
                "result": {
                    "descr": {"order": "buy 0.01000000 XXBTZUSD @ limit 50000.00"},
                    "txid": ["OXXXXX-XXXXX-XXXXX"],
                },
            },
        )

    transport = httpx2.MockTransport(handler)
    async with httpx2.AsyncClient(transport=transport) as client:
        adapter = KrakenSpotAdapter(_API_KEY, _API_SECRET, client)
        submitted = await adapter.submit_spot_order(order, at=now)

    assert submitted.state is OrderState.ACKNOWLEDGED
    assert submitted.venue_order_id == "OXXXXX-XXXXX-XXXXX"


@pytest.mark.anyio
async def test_submit_spot_order_rejected_by_exchange() -> None:
    now = datetime.now(UTC)
    order = _sample_order()

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={"error": ["EOrder:Insufficient funds"], "result": {}},
        )

    transport = httpx2.MockTransport(handler)
    async with httpx2.AsyncClient(transport=transport) as client:
        adapter = KrakenSpotAdapter(_API_KEY, _API_SECRET, client)
        rejected = await adapter.submit_spot_order(order, at=now)

    assert rejected.state is OrderState.REJECTED


@pytest.mark.anyio
async def test_submit_spot_order_timeout_requires_reconciliation() -> None:
    now = datetime.now(UTC)
    order = _sample_order()

    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.TimeoutException("Connection timed out")

    transport = httpx2.MockTransport(handler)
    async with httpx2.AsyncClient(transport=transport) as client:
        adapter = KrakenSpotAdapter(_API_KEY, _API_SECRET, client)
        ambiguous = await adapter.submit_spot_order(order, at=now)

    # Invariant 8: must advance to RECONCILIATION_REQUIRED
    assert ambiguous.state is OrderState.RECONCILIATION_REQUIRED


@pytest.mark.anyio
async def test_cancel_spot_order_success() -> None:
    now = datetime.now(UTC)
    order = _sample_order(
        state=OrderState.ACKNOWLEDGED, venue_order_id="OXXXXX-XXXXX-XXXXX"
    )

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/0/private/CancelOrder"
        return httpx2.Response(
            200,
            json={"error": [], "result": {"count": 1}},
        )

    transport = httpx2.MockTransport(handler)
    async with httpx2.AsyncClient(transport=transport) as client:
        adapter = KrakenSpotAdapter(_API_KEY, _API_SECRET, client)
        cancelled = await adapter.cancel_spot_order(order, at=now)

    assert cancelled.state is OrderState.CANCELLED


@pytest.mark.anyio
async def test_reconcile_order_filled() -> None:
    now = datetime.now(UTC)
    order = _sample_order(
        state=OrderState.ACKNOWLEDGED, venue_order_id="OXXXXX-XXXXX-XXXXX"
    )

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/0/private/QueryOrders"
        return httpx2.Response(
            200,
            json={
                "error": [],
                "result": {
                    "OXXXXX-XXXXX-XXXXX": {
                        "status": "closed",
                        "vol_exec": "0.01000000",
                        "cost": "500.00",
                        "fee": "1.25",
                    }
                },
            },
        )

    transport = httpx2.MockTransport(handler)
    async with httpx2.AsyncClient(transport=transport) as client:
        adapter = KrakenSpotAdapter(_API_KEY, _API_SECRET, client)
        filled = await adapter.reconcile_order(order, at=now)

    assert filled.state is OrderState.FILLED
    assert filled.filled_quantity == Decimal("0.01000000")
    assert filled.average_price == Decimal("50000.00")
    assert filled.fee == Decimal("1.25")


@pytest.mark.anyio
@pytest.mark.parametrize("venue_outcome", ["closed", "missing", "duplicate", "partial"])
async def test_kraken_reconciliation_after_reopen_never_resubmits(
    tmp_path: Path, venue_outcome: str
) -> None:
    path = tmp_path / "orders.db"
    store = AuditStore(path)
    store.initialize()
    order = _sample_order()
    calls: list[str] = []
    venue_id = "OXXXXX-XXXXX-XXXXX"

    def handler(request: httpx2.Request) -> httpx2.Response:
        endpoint = request.url.path.rsplit("/", 1)[1]
        calls.append(endpoint)
        if endpoint == "AddOrder":
            assert len(calls) == 1
            raise httpx2.ReadTimeout("Venue reply lost", request=request)
        fields = urllib.parse.parse_qs(request.content.decode())
        assert fields["cl_ord_id"] == [order.client_order_id]
        collection = "open" if endpoint == "OpenOrders" else "closed"
        entries = {}
        if venue_outcome in {"closed", "duplicate"} and collection == "closed":
            entries[venue_id] = {
                "cl_ord_id": order.client_order_id,
                "status": "closed",
                "vol_exec": "0.01",
                "cost": "500",
                "fee": "1.25",
            }
        if venue_outcome in {"duplicate", "partial"} and collection == "open":
            entries["OOTHER-OTHER-OTHER"] = {
                "cl_ord_id": order.client_order_id,
                "status": "open",
                "vol_exec": "0.005",
                "cost": "250",
                "fee": "0.625",
            }
        return httpx2.Response(200, json={"error": [], "result": {collection: entries}})

    transport = httpx2.MockTransport(handler)
    async with httpx2.AsyncClient(transport=transport) as client:
        pending = await KrakenSpotAdapter(
            _API_KEY, _API_SECRET, client
        ).submit_spot_order(order, at=datetime.now(UTC))
        assert pending.state is OrderState.RECONCILIATION_REQUIRED
        store.save_spot_order(pending)
    restarted = AuditStore(path)
    restored = restarted.get_spot_order(order.client_order_id)
    assert restored is not None and restored == pending
    async with httpx2.AsyncClient(transport=transport) as client:
        reconciled = await KrakenSpotAdapter(
            _API_KEY, _API_SECRET, client
        ).reconcile_order(restored, at=datetime.now(UTC))
    assert calls == ["AddOrder", "OpenOrders", "ClosedOrders"]
    assert (
        reconciled.state
        is {
            "closed": OrderState.FILLED,
            "partial": OrderState.PARTIALLY_FILLED,
            "missing": OrderState.RECONCILIATION_REQUIRED,
            "duplicate": OrderState.RECONCILIATION_REQUIRED,
        }[venue_outcome]
    )
    assert reconciled.venue_order_id == {
        "closed": venue_id,
        "partial": "OOTHER-OTHER-OTHER",
    }.get(venue_outcome)
    restarted.save_spot_order(reconciled)
    assert AuditStore(path).get_spot_order(order.client_order_id) == reconciled


@pytest.mark.anyio
async def test_durable_submission_recovers_after_cancellation(tmp_path: Path) -> None:
    path = tmp_path / "execution.db"
    store = AuditStore(path)
    store.initialize()
    order = _sample_order()
    now = datetime.now(UTC)
    quote = Quote(BTC_USD, SOURCE, Decimal("49999"), Decimal("50000"), now, now, now)
    risk = RiskEngine(RiskPolicy(live_mode_enabled=True))
    candidate = StrategyCandidate(
        candidate_id="canary",
        name="SMA",
        version="v1.0.0",
        dataset_hash="snapshot",
        stage=PromotionStage.PAPER_APPROVED,
    )
    store.save_strategy_candidate(candidate)
    store.authorize_spot_canary(
        candidate,
        hashlib.sha256(_API_KEY.encode()).hexdigest(),
        Decimal("500"),
        now,
        now + timedelta(hours=1),
    )
    assert store.update_candidate_stage(candidate, PromotionStage.LIVE_CANARY, now)
    strategy: dict[str, Any] = {
        "candidate_id": candidate.candidate_id,
        "strategy_version": candidate.version,
    }
    calls: list[str] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        endpoint = request.url.path.rsplit("/", 1)[1]
        calls.append(endpoint)
        before = AuditStore(path).get_spot_order(order.client_order_id)
        assert before is not None and before.state == OrderState.SUBMITTED
        assert before.risk_decision_id != order.risk_decision_id
        if endpoint == "AddOrder":
            raise asyncio.CancelledError
        collection = "open" if endpoint == "OpenOrders" else "closed"
        result = {}
        if collection == "open":
            result["OXXXXX-XXXXX-XXXXX"] = {
                "cl_ord_id": order.client_order_id,
                "status": "open",
                "vol_exec": "0",
                "cost": "0",
                "fee": "0",
            }
        return httpx2.Response(200, json={"error": [], "result": {collection: result}})

    transport = httpx2.MockTransport(handler)
    async with httpx2.AsyncClient(transport=transport) as client:
        spot = KrakenSpotExecution(
            store, KrakenSpotAdapter(_API_KEY, _API_SECRET, client), risk
        )
        other_key = KrakenSpotExecution(
            store, KrakenSpotAdapter("different-key", _API_SECRET, client), risk
        )
        with pytest.raises(RiskRejected, match="authorization"):
            await other_key.submit(order, quote, **strategy)
        with pytest.raises(RiskRejected, match="authorization"):
            await spot.submit(_sample_order(quantity="0.012"), quote, **strategy)
        assert calls == []
        store.set_execution_pause(True)
        with pytest.raises(RiskRejected, match="paused"):
            await spot.submit(order, quote, **strategy)
        assert not calls and store.get_spot_order(order.client_order_id) is None
        store.set_execution_pause(False)
        with pytest.raises(asyncio.CancelledError):
            await spot.submit(order, quote, **strategy)
    reopened = AuditStore(path)
    pending = reopened.get_spot_order(order.client_order_id)
    assert pending is not None and pending.state == OrderState.SUBMITTED
    async with httpx2.AsyncClient(transport=transport) as client:
        recovered = KrakenSpotExecution(
            reopened, KrakenSpotAdapter(_API_KEY, _API_SECRET, client), risk
        )
        with pytest.raises(RiskRejected, match="already recorded"):
            await recovered.submit(order, quote, **strategy)
        with pytest.raises(RiskRejected, match="unresolved"):
            await recovered.submit(
                _sample_order(client_id="other-id"), quote, **strategy
            )
        (observed,) = await recovered.reconcile_startup()
    assert observed.state == OrderState.ACKNOWLEDGED
    assert reopened.get_spot_order(order.client_order_id) == observed
    assert not reopened.apply_spot_observation(
        pending, pending.transition_to(OrderState.RECONCILIATION_REQUIRED, now)
    )
    assert calls == ["AddOrder", "OpenOrders", "ClosedOrders"]


def test_canary_cli_authorization_persists_expires_and_revokes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "audit.sqlite"
    store = AuditStore(path)
    store.initialize()
    candidate = StrategyCandidate(
        candidate_id="test-canary",
        name="SMA",
        version="v1.0.0",
        dataset_hash="research-data",
        stage=PromotionStage.PAPER_APPROVED,
    )
    store.save_strategy_candidate(candidate)
    credentials = tmp_path / "keys.json"
    credentials.write_text(
        json.dumps({"api_key": _API_KEY, "api_secret": _API_SECRET}), encoding="utf-8"
    )
    monkeypatch.setattr(launcher, "local_store", lambda: AuditStore(path))
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr(
        "sys.argv",
        [
            "hcs-trader",
            "spot-canary-allow",
            candidate.candidate_id,
            "--credentials-file",
            str(credentials),
            "--max-notional",
            "75",
        ],
    )
    monkeypatch.setattr("builtins.input", lambda prompt: "declined")
    with pytest.raises(SystemExit) as rejected:
        launcher.main()
    assert rejected.value.code == 1
    assert not store.has_spot_canary_approval(candidate.candidate_id, datetime.now(UTC))
    monkeypatch.setattr("builtins.input", lambda prompt: "AUTHORIZE")
    launcher.main()
    report = capsys.readouterr()
    assert "USD 75.00" in report.err
    assert (
        _API_KEY not in report.err + report.out
        and _API_SECRET not in report.err + report.out
    )
    reopened = AuditStore(path)
    now = datetime.now(UTC)
    assert reopened.has_spot_canary_approval(candidate.candidate_id, now)
    assert not reopened.has_spot_canary_approval(
        candidate.candidate_id, now + timedelta(hours=25)
    )

    from hcs_trader.api import create_app

    with TestClient(create_app(reopened), base_url="http://127.0.0.1") as client:
        uri = f"/api/v1/strategies/candidates/{candidate.candidate_id}/promote"
        assert client.post(uri, json={"target_stage": "LIVE_CANARY"}).is_success
        assert (
            client.post(uri, json={"target_stage": "LIVE_BOUNDED"}).status_code == 409
        )
        assert client.get("/api/v1/system/capabilities").json()["live_orders"] is False
    monkeypatch.setattr("sys.argv", ["hcs-trader", "spot-canary-revoke"])
    launcher.main()
    assert not reopened.has_spot_canary_approval(candidate.candidate_id, now)


@pytest.mark.parametrize("unexpected_txid", [False, True])
def test_validation_cli_never_records_a_live_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    unexpected_txid: bool,
) -> None:
    credentials = tmp_path / "keys.json"
    credentials.write_text(
        json.dumps({"api_key": _API_KEY, "api_secret": _API_SECRET}), encoding="utf-8"
    )
    calls = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        assert str(request.url) == "https://api.kraken.com/0/private/AddOrder"
        fields = urllib.parse.parse_qs(request.content.decode())
        assert fields["validate"] == ["true"]
        assert fields["ordertype"] == ["limit"]
        assert fields["volume"] == ["0.01000000"]
        assert fields["price"] == ["50000.00"]
        assert "leverage" not in fields and "margin" not in fields
        result: dict[str, object] = {"descr": {"order": "buy 0.01 XBTUSD @ 50000"}}
        if unexpected_txid:
            result["txid"] = ["unexpected-order"]
        return httpx2.Response(200, json={"error": [], "result": result})

    client_class = httpx2.AsyncClient

    def client(*, trust_env: bool) -> httpx2.AsyncClient:
        assert trust_env is False
        return client_class(transport=httpx2.MockTransport(handler), trust_env=False)

    monkeypatch.setattr(httpx2, "AsyncClient", client)
    monkeypatch.setattr(
        launcher, "local_store", lambda: pytest.fail("Validation used SQLite")
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "hcs-trader",
            "kraken-validate",
            "--credentials-file",
            str(credentials),
            "--side",
            "buy",
            "--quantity",
            "0.01",
            "--limit-price",
            "50000",
        ],
    )
    if unexpected_txid:
        with pytest.raises(SystemExit) as exit_info:
            launcher.main()
        assert exit_info.value.code == 1
        assert capsys.readouterr().out == ""
    else:
        launcher.main()
        result = json.loads(capsys.readouterr().out)
        assert result["validated"] is True and result["market"] == "BTC/USD"
        assert "state" not in result and "venue_order_id" not in result
    assert calls == 1


@pytest.mark.parametrize("visible", [False, True])
def test_serve_recovers_kraken_orders_only_when_opted_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, visible: bool
) -> None:
    store = AuditStore(tmp_path / "audit.sqlite")
    store.initialize()
    order = _sample_order().transition_to(OrderState.SUBMITTED, at=datetime.now(UTC))
    store.save_spot_order(order)
    credentials = tmp_path / "keys.json"
    credentials.write_text(
        json.dumps({"api_key": _API_KEY, "api_secret": _API_SECRET}), encoding="utf-8"
    )
    calls: list[str] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        endpoint = request.url.path.rsplit("/", 1)[1]
        assert endpoint in {"OpenOrders", "ClosedOrders"}
        calls.append(endpoint)
        kind = "open" if endpoint == "OpenOrders" else "closed"
        records = {}
        if visible and kind == "open":
            records["OXXXXX-XXXXX-XXXXX"] = {
                "cl_ord_id": order.client_order_id,
                "status": "open",
                "vol_exec": "0",
                "cost": "0",
                "fee": "0",
            }
        return httpx2.Response(200, json={"error": [], "result": {kind: records}})

    client_class = httpx2.AsyncClient
    monkeypatch.setattr(
        httpx2,
        "AsyncClient",
        lambda *, trust_env: client_class(
            trust_env=trust_env, transport=httpx2.MockTransport(respond)
        ),
    )
    status: dict[str, object] = {}

    class Server:
        def __init__(self, config: uvicorn.Config) -> None:
            self.app: Any = config.app

        async def serve(self) -> None:
            with TestClient(self.app, base_url="http://127.0.0.1") as client:
                status.update(client.get("/api/v1/system/status").json())
                assert (
                    client.get("/api/v1/system/capabilities").json()["live_orders"]
                    is False
                )

    monkeypatch.setattr(uvicorn, "Server", Server)
    monkeypatch.setattr(
        "sys.argv", ["hcs-trader", "serve", "--data-dir", str(tmp_path)]
    )
    launcher.main()
    assert calls == [] and status["network_enabled"] is False
    assert store.get_spot_order(order.client_order_id) == order

    monkeypatch.setattr(
        "sys.argv",
        [
            "hcs-trader",
            "serve",
            "--data-dir",
            str(tmp_path),
            "--kraken-reconcile-credentials",
            str(credentials),
        ],
    )
    launcher.main()
    assert calls == ["OpenOrders", "ClosedOrders"]
    assert status == {
        "mode": "DATA_ONLY",
        "execution_available": False,
        "network_enabled": True,
    }
    updated = store.get_spot_order(order.client_order_id)
    assert updated and updated.state is (
        OrderState.ACKNOWLEDGED if visible else OrderState.RECONCILIATION_REQUIRED
    )
    events = {event.kind for event in store.list_audit_events(limit=10)}
    assert (
        "spot.recovery.completed" if visible else "spot.recovery.incomplete"
    ) in events


def test_spot_order_storage_persistence(tmp_path: Path) -> None:
    store = AuditStore(tmp_path / "test_spot.db")
    store.initialize()

    now = datetime.now(UTC)
    order = _sample_order(client_id="cl-ord-persist")
    store.save_spot_order(order)

    retrieved = store.get_spot_order("cl-ord-persist")
    assert retrieved is not None
    assert retrieved.client_order_id == "cl-ord-persist"
    assert retrieved.quantity == Decimal("0.01")
    assert retrieved.state is OrderState.PREVIEWED

    # Advance state to SUBMITTED -> ACKNOWLEDGED
    advanced = order.transition_to(OrderState.SUBMITTED, at=now).transition_to(
        OrderState.ACKNOWLEDGED, at=now, venue_order_id="OTX-999"
    )
    store.save_spot_order(advanced)

    updated = store.get_spot_order("cl-ord-persist")
    assert updated is not None
    assert updated.state is OrderState.ACKNOWLEDGED
    assert updated.venue_order_id == "OTX-999"

    # List open orders
    open_orders = store.list_open_spot_orders("kraken")
    assert len(open_orders) == 1
    assert open_orders[0].client_order_id == "cl-ord-persist"


@pytest.mark.anyio
async def test_canary_execution_end_to_end_and_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "execution.db"
    store = AuditStore(path)
    store.initialize()
    now = datetime.now(UTC)
    quote = Quote(BTC_USD, SOURCE, Decimal("49999"), Decimal("50000"), now, now, now)
    risk = RiskEngine(RiskPolicy(live_mode_enabled=True))
    candidate = StrategyCandidate(
        candidate_id="canary-e2e",
        name="SMA-Canary",
        version="v1.0.0",
        dataset_hash="hash-snap-01",
        stage=PromotionStage.PAPER_APPROVED,
    )
    store.save_strategy_candidate(candidate)

    fingerprint = hashlib.sha256(_API_KEY.encode()).hexdigest()
    expires_at = now + timedelta(hours=24)
    store.authorize_spot_canary(candidate, fingerprint, Decimal("500"), now, expires_at)

    approval = store.get_spot_canary_approval(candidate.candidate_id, now)
    assert approval is not None
    assert approval.candidate_id == "canary-e2e"
    assert approval.max_notional == Decimal("500")
    assert approval.key_fingerprint == fingerprint
    assert approval.active is False

    assert store.update_candidate_stage(candidate, PromotionStage.LIVE_CANARY, now)
    active_approval = store.get_spot_canary_approval(candidate.candidate_id, now)
    assert active_approval is not None and active_approval.active is True

    async def handler(request: httpx2.Request) -> httpx2.Response:
        endpoint = request.url.path.rsplit("/", 1)[1]
        if endpoint == "AddOrder":
            return httpx2.Response(
                200,
                json={
                    "error": [],
                    "result": {
                        "descr": {"order": "buy 0.005 XXBTZUSD @ limit 50000.00"},
                        "txid": ["OTX-CANARY-999"],
                    },
                },
            )
        if endpoint == "QueryOrders":
            return httpx2.Response(
                200,
                json={
                    "error": [],
                    "result": {
                        "OTX-CANARY-999": {
                            "status": "closed",
                            "vol_exec": "0.00500000",
                            "cost": "250.00",
                            "fee": "0.50",
                            "cl_ord_id": "canary-order-001",
                        }
                    },
                },
            )
        return httpx2.Response(404, json={"error": ["EGeneral:Invalid endpoint"]})

    transport = httpx2.MockTransport(handler)
    async with httpx2.AsyncClient(transport=transport) as client:
        adapter = KrakenSpotAdapter(_API_KEY, _API_SECRET, client)
        execution = KrakenSpotExecution(store, adapter, risk)

        order = _sample_order(
            client_id="canary-order-001",
            quantity="0.005",
            limit_price="50000.00",
        )
        submitted = await execution.submit(
            order,
            quote,
            candidate_id=candidate.candidate_id,
            strategy_version=candidate.version,
        )
        assert submitted.state == OrderState.ACKNOWLEDGED
        assert submitted.venue_order_id == "OTX-CANARY-999"

        saved = store.get_spot_order("canary-order-001")
        assert saved is not None
        assert saved.state == OrderState.ACKNOWLEDGED
        assert saved.venue_order_id == "OTX-CANARY-999"

        reconciled = await adapter.reconcile_order(saved, at=now)
        assert reconciled.state == OrderState.FILLED
        assert reconciled.filled_quantity == Decimal("0.00500000")
        assert reconciled.average_price == Decimal("50000")
        assert reconciled.fee == Decimal("0.50")
        assert store.apply_spot_observation(saved, reconciled)
        assert store.get_spot_order("canary-order-001") == reconciled

        # Fail-closed: notional exceeds authorized cap ($600 > $500)
        over_cap_order = _sample_order(
            client_id="canary-over-cap",
            quantity="0.012",
            limit_price="50000.00",
        )
        with pytest.raises(RiskRejected, match="authorization"):
            await execution.submit(
                over_cap_order,
                quote,
                candidate_id=candidate.candidate_id,
                strategy_version=candidate.version,
            )

        # Fail-closed: different API key
        other_adapter = KrakenSpotAdapter("other-key", _API_SECRET, client)
        other_exec = KrakenSpotExecution(store, other_adapter, risk)
        with pytest.raises(RiskRejected, match="authorization"):
            await other_exec.submit(
                _sample_order(client_id="canary-wrong-key", quantity="0.001"),
                quote,
                candidate_id=candidate.candidate_id,
                strategy_version=candidate.version,
            )

        # Fail-closed: paused execution
        store.set_execution_pause(True)
        with pytest.raises(RiskRejected, match="paused"):
            await execution.submit(
                _sample_order(client_id="canary-paused", quantity="0.001"),
                quote,
                candidate_id=candidate.candidate_id,
                strategy_version=candidate.version,
            )
        store.set_execution_pause(False)

        # Fail-closed: revoked authorization
        store.revoke_spot_canary()
        with pytest.raises(RiskRejected, match="authorization"):
            await execution.submit(
                _sample_order(client_id="canary-revoked", quantity="0.001"),
                quote,
                candidate_id=candidate.candidate_id,
                strategy_version=candidate.version,
            )


def _depth_response() -> httpx2.Response:
    timestamp = int(datetime.now(UTC).timestamp())
    return httpx2.Response(
        200,
        json={
            "error": [],
            "result": {
                "XXBTZUSD": {
                    "asks": [["50000", "1", timestamp]],
                    "bids": [["49999", "1", timestamp]],
                }
            },
        },
    )


def test_canary_cli_submit_and_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "audit.sqlite"
    store = AuditStore(db_path)
    store.initialize()
    now = datetime.now(UTC)
    store.record_quote(
        Quote(BTC_USD, SOURCE, Decimal("49999"), Decimal("50000"), now, now, now)
    )

    candidate = StrategyCandidate(
        candidate_id="canary-cli",
        name="SMA-CLI",
        version="v1.0.0",
        dataset_hash="hash-snap-02",
        stage=PromotionStage.PAPER_APPROVED,
    )
    store.save_strategy_candidate(candidate)

    credentials = tmp_path / "keys.json"
    credentials.write_text(
        json.dumps({"api_key": _API_KEY, "api_secret": _API_SECRET}), encoding="utf-8"
    )

    fingerprint = hashlib.sha256(_API_KEY.encode()).hexdigest()
    store.authorize_spot_canary(
        candidate, fingerprint, Decimal("500"), now, now + timedelta(hours=24)
    )
    store.update_candidate_stage(candidate, PromotionStage.LIVE_CANARY, now)

    market_available = False

    def handler(request: httpx2.Request) -> httpx2.Response:
        endpoint = request.url.path.rsplit("/", 1)[1]
        if endpoint == "Depth":
            return _depth_response() if market_available else httpx2.Response(503)
        if endpoint == "AddOrder":
            return httpx2.Response(
                200,
                json={
                    "error": [],
                    "result": {
                        "descr": {"order": "buy 0.005 XXBTZUSD @ limit 50000.00"},
                        "txid": ["OTX-CLI-12345"],
                    },
                },
            )
        return httpx2.Response(404, json={"error": ["EGeneral:Invalid endpoint"]})

    client_class = httpx2.AsyncClient
    monkeypatch.setattr(
        httpx2,
        "AsyncClient",
        lambda *, trust_env: client_class(
            trust_env=trust_env, transport=httpx2.MockTransport(handler)
        ),
    )

    monkeypatch.setattr(
        "sys.argv",
        [
            "hcs-trader",
            "spot-canary-submit",
            "canary-cli",
            "--credentials-file",
            str(credentials),
            "--side",
            "buy",
            "--quantity",
            "0.005",
            "--limit-price",
            "50000",
            "--data-dir",
            str(tmp_path),
        ],
    )
    monkeypatch.setattr(
        launcher, "blocking_limitations", lambda _path: ["KL-001: no venue evidence"]
    )
    with pytest.raises(SystemExit) as blocked:
        launcher.main()
    assert blocked.value.code == 1
    assert "KL-001" in capsys.readouterr().err
    assert store.list_open_spot_orders("kraken") == []

    monkeypatch.setattr(
        launcher,
        "require_runtime_live_policy",
        lambda _store: RiskPolicy(live_mode_enabled=True),
    )
    with pytest.raises(SystemExit) as unavailable:
        launcher.main()
    assert unavailable.value.code == 1
    assert "requires a fresh market quote" in capsys.readouterr().err
    assert store.list_open_spot_orders("kraken") == []

    market_available = True
    launcher.main()
    out = json.loads(capsys.readouterr().out)
    assert out["state"] == "ACKNOWLEDGED"
    assert out["venue_order_id"] == "OTX-CLI-12345"
    assert out["quantity"] == "0.005"

    monkeypatch.setattr(
        "sys.argv",
        [
            "hcs-trader",
            "spot-canary-submit",
            "unknown-candidate",
            "--credentials-file",
            str(credentials),
            "--side",
            "buy",
            "--quantity",
            "0.005",
            "--limit-price",
            "50000",
            "--data-dir",
            str(tmp_path),
        ],
    )
    with pytest.raises(SystemExit) as exit_info:
        launcher.main()
    assert exit_info.value.code == 1


def test_spot_order_api_serving_and_canary_approval(tmp_path: Path) -> None:
    store = AuditStore(tmp_path / "api_test.db")
    store.initialize()
    now = datetime.now(UTC).replace(microsecond=0)
    quote = Quote(BTC_USD, SOURCE, Decimal("49999"), Decimal("50000"), now, now, now)
    store.record_quote(quote)

    candidate = StrategyCandidate(
        candidate_id="cand-api",
        name="SMA-API",
        version="v1.0.0",
        dataset_hash="hash-api-01",
        stage=PromotionStage.PAPER_APPROVED,
    )
    store.save_strategy_candidate(candidate)

    app_disabled = create_app(store)
    with TestClient(app_disabled, base_url="http://127.0.0.1") as client:
        resp = client.post(
            "/api/v1/orders/spot",
            json={
                "side": "buy",
                "quantity": "0.005",
                "limit_price": "50000",
                "candidate_id": "cand-api",
                "strategy_version": "v1.0.0",
            },
        )
        assert resp.status_code == 403
        caps = client.get("/api/v1/system/capabilities").json()
        assert caps["live_orders"] is False
        assert (
            client.get(
                "/api/v1/strategies/candidates/cand-api/canary-approval"
            ).status_code
            == 404
        )

    fingerprint = hashlib.sha256(_API_KEY.encode()).hexdigest()
    store.authorize_spot_canary(
        candidate, fingerprint, Decimal("500"), now, now + timedelta(hours=24)
    )
    store.update_candidate_stage(candidate, PromotionStage.LIVE_CANARY, now)

    market_available = False
    elapsed = 0.0

    def handler(request: httpx2.Request) -> httpx2.Response:
        endpoint = request.url.path.rsplit("/", 1)[1]
        if endpoint == "Depth":
            return _depth_response() if market_available else httpx2.Response(503)
        if endpoint == "AddOrder":
            return httpx2.Response(
                200,
                json={
                    "error": [],
                    "result": {
                        "descr": {"order": "buy 0.005 XXBTZUSD @ limit 50000.00"},
                        "txid": ["OTX-API-999"],
                    },
                },
            )
        return httpx2.Response(404, json={"error": ["EGeneral:Invalid endpoint"]})

    mock_client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    adapter = KrakenSpotAdapter(_API_KEY, _API_SECRET, mock_client)
    spot_exec = KrakenSpotExecution(
        store, adapter, RiskEngine(RiskPolicy(live_mode_enabled=True))
    )
    quote_srv = QuoteService(
        KrakenQuoteProvider(mock_client),
        BTC_USD,
        record_quote=store.record_quote,
        elapsed=lambda: elapsed,
    )
    quote_srv.restore(quote)

    app_live = create_app(store, quote_service=quote_srv, spot_execution=spot_exec)
    with TestClient(app_live, base_url="http://127.0.0.1") as client:
        caps = client.get("/api/v1/system/capabilities").json()
        assert caps["live_orders"] is True

        status = client.get("/api/v1/system/status").json()
        assert status["mode"] == "LIVE"
        assert status["execution_available"] is True

        approval_resp = client.get(
            "/api/v1/strategies/candidates/cand-api/canary-approval"
        )
        assert approval_resp.status_code == 200
        appr = approval_resp.json()
        assert appr["candidate_id"] == "cand-api"
        assert appr["max_notional"] == "500"
        assert appr["active"] is True

        cross = client.post(
            "/api/v1/orders/spot",
            json={
                "side": "buy",
                "quantity": "0.005",
                "limit_price": "50000",
                "candidate_id": "cand-api",
                "strategy_version": "v1.0.0",
            },
            headers={"sec-fetch-site": "cross-site", "origin": "http://malicious.org"},
        )
        assert cross.status_code == 403

        proposal = {
            "side": "buy",
            "quantity": "0.005",
            "limit_price": "50000",
            "candidate_id": "cand-api",
            "strategy_version": "v1.0.0",
        }
        unavailable = client.post("/api/v1/orders/spot", json=proposal)
        assert unavailable.status_code == 503
        assert "requires a fresh market quote" in unavailable.json()["detail"]
        assert store.list_open_spot_orders("kraken") == []
        cached = client.get("/api/v1/market/quotes").json()
        assert cached["quality"] == "DEGRADED"
        assert cached["source"] == SOURCE
        assert cached["provider_issue"] == "unavailable"

        market_available = True
        elapsed = 20.0
        submit_resp = client.post("/api/v1/orders/spot", json=proposal)
        assert submit_resp.status_code == 200
        data = submit_resp.json()
        assert data["state"] == "ACKNOWLEDGED"
        assert data["venue_order_id"] == "OTX-API-999"

        over_resp = client.post(
            "/api/v1/orders/spot",
            json={
                "side": "buy",
                "quantity": "0.012",
                "limit_price": "50000",
                "candidate_id": "cand-api",
                "strategy_version": "v1.0.0",
            },
        )
        assert over_resp.status_code == 409
