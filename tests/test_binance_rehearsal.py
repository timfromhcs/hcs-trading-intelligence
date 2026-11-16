"""Rehearsal flow, redaction, CLI wiring and Kraken validate probe (CI-safe)."""

import json
import urllib.parse
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx2
import pytest

from hcs_trader import __main__ as cli
from hcs_trader.binance_rehearsal import (
    REDACTED,
    plan_rehearsal_order,
    redact_evidence,
    run_rehearsal,
)
from hcs_trader.binance_testnet import (
    BinanceSpotTestnetAdapter,
    HmacSha256Signer,
    SymbolRules,
)
from hcs_trader.kraken_spot import kraken_validate_probe

FIXTURES = Path(__file__).parent / "fixtures" / "binance_testnet"
KEY = "rehearsal-key-AAAA"
SECRET = "rehearsal-secret-BBBB"  # noqa: S105


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def fx(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class FakeVenue:
    """Minimal stateful testnet double: one resting order keyed by client id."""

    def __init__(self, *, drop_first_post: bool = False, fail_cancel: bool = False):
        self.orders: dict[str, dict[str, Any]] = {}
        self.drop_first_post = drop_first_post
        self.fail_cancel = fail_cancel
        self.posts = 0
        self.hosts: set[str] = set()

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.hosts.add(request.url.host)
        params = dict(request.url.params)
        path = request.url.path
        simple = {
            "/api/v3/time": "server_time",
            "/api/v3/account": "account",
            "/api/v3/ticker/price": "ticker_price",
            "/api/v3/exchangeInfo": "exchange_info_btcusdt",
            "/api/v3/myTrades": "my_trades",
        }
        if path in simple:
            body = fx(simple[path])
            if path == "/api/v3/myTrades":
                body = []
            return httpx2.Response(200, json=body)
        if path == "/api/v3/openOrders":
            return httpx2.Response(200, json=list(self.orders.values()))
        client_id = params.get("newClientOrderId") or params.get("origClientOrderId")
        if request.method == "POST":
            self.posts += 1
            if client_id in self.orders:
                return httpx2.Response(400, json=fx("error_duplicate_order"))
            body = {
                **fx("order_new_full"),
                "clientOrderId": client_id,
                "price": params["price"],
                "origQty": params["quantity"],
            }
            self.orders[str(client_id)] = body
            if self.drop_first_post and self.posts == 1:
                raise httpx2.ReadTimeout("lost response", request=request)
            return httpx2.Response(200, json=body)
        if client_id not in self.orders:
            return httpx2.Response(400, json=fx("error_unknown_order"))
        if request.method == "DELETE":
            if self.fail_cancel:
                return httpx2.Response(503, text="down")
            body = {**self.orders.pop(str(client_id)), "status": "CANCELED"}
            return httpx2.Response(200, json=body)
        return httpx2.Response(200, json=self.orders[str(client_id)])


def make_adapter(
    venue: FakeVenue,
) -> tuple[BinanceSpotTestnetAdapter, httpx2.AsyncClient]:
    async def no_sleep(delay: float) -> None:
        return None

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(venue))
    return (
        BinanceSpotTestnetAdapter(
            KEY, HmacSha256Signer(SECRET), client, sleep=no_sleep
        ),
        client,
    )


def _ids() -> Any:
    counter = iter(range(1000))
    return lambda: f"{next(counter):04d}"


def test_plan_rehearsal_order_is_tick_and_step_aligned() -> None:
    rules = SymbolRules(
        "BTCUSDT",
        Decimal("0.01"),
        Decimal("0.00001"),
        Decimal("0.00001"),
        Decimal("5"),
    )
    price, qty = plan_rehearsal_order(Decimal("60000.123"), rules)
    assert price == Decimal("42000.08")
    assert qty % rules.step_size == 0 and price * qty >= Decimal("5") * Decimal("1.5")
    with pytest.raises(ValueError, match="not positive"):
        plan_rehearsal_order(Decimal("0.001"), rules)


def test_redact_evidence_drops_keys_and_masks_secrets() -> None:
    doc = {
        "signature": "abc",
        "ok": ["x", f"url?key={KEY}", {"secret": "s", "n": 1}],
        "msg": f"leaked {SECRET}",
    }
    out = redact_evidence(doc, [KEY, SECRET, ""])
    assert out["signature"] == REDACTED
    assert out["ok"] == ["x", REDACTED, {"secret": REDACTED, "n": 1}]
    assert out["msg"] == REDACTED


@pytest.mark.anyio
async def test_rehearsal_flow_passes_against_fake_testnet() -> None:
    venue = FakeVenue()
    adapter, client = make_adapter(venue)
    async with client:
        evidence = await run_rehearsal(
            adapter,
            now=lambda: datetime(2026, 1, 1, tzinfo=UTC),
            id_factory=_ids(),
            secrets_to_hide=[KEY, SECRET],
        )
    assert evidence["passed"], evidence
    names = [s["name"] for s in evidence["steps"]]
    assert names == [
        "server_time",
        "account",
        "plan",
        "submit",
        "open_orders",
        "query_by_client_id",
        "duplicate_client_id_replay",
        "single_open_order_after_replay",
        "cancel",
        "my_trades",
        "balances_after",
    ]
    assert venue.hosts == {"testnet.binance.vision"}
    assert venue.orders == {}
    assert venue.posts == 2  # original + deliberate duplicate replay
    text = json.dumps(evidence)
    assert KEY not in text and SECRET not in text
    assert evidence["venue"] == "binance-testnet"


@pytest.mark.anyio
async def test_rehearsal_recovers_from_lost_response_via_client_id_query() -> None:
    venue = FakeVenue(drop_first_post=True)
    adapter, client = make_adapter(venue)
    async with client:
        evidence = await run_rehearsal(adapter, id_factory=_ids())
    assert evidence["passed"], evidence
    # Only the deliberate replay is a second POST; the lost one was queried, not resent.
    assert venue.posts == 2


@pytest.mark.anyio
async def test_rehearsal_failure_cleans_up_resting_order() -> None:
    venue = FakeVenue(fail_cancel=True)
    adapter, client = make_adapter(venue)
    async with client:
        evidence = await run_rehearsal(adapter, id_factory=_ids())
    assert not evidence["passed"]
    names = [s["name"] for s in evidence["steps"]]
    assert "failure" in names and names[-1] == "cleanup_cancel"
    assert (
        evidence["steps"][-1]["ok"] is False
    )  # cancel is still down; reported honestly


@pytest.mark.anyio
async def test_rehearsal_fails_cleanly_before_any_order_when_balance_call_fails() -> (
    None
):
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/api/v3/time":
            return httpx2.Response(200, json=fx("server_time"))
        return httpx2.Response(400, json=fx("error_timestamp"))

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    adapter = BinanceSpotTestnetAdapter(KEY, HmacSha256Signer(SECRET), client)
    async with client:
        evidence = await run_rehearsal(adapter)
    assert not evidence["passed"]
    assert [s["name"] for s in evidence["steps"]] == ["server_time", "failure"]


# -- CLI ---------------------------------------------------------------------


def test_cli_rehearsal_requires_testnet_credentials(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("HCS_BINANCE_TESTNET_KEY", raising=False)
    monkeypatch.delenv("HCS_BINANCE_TESTNET_SECRET", raising=False)
    monkeypatch.setattr("sys.argv", ["hcs-trader", "rehearsal-binance"])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 1
    assert "HCS_BINANCE_TESTNET_KEY" in capsys.readouterr().err


def test_cli_rehearsal_writes_redacted_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    venue = FakeVenue()
    real_client = httpx2.AsyncClient

    def client_factory(**kwargs: Any) -> httpx2.AsyncClient:
        return real_client(transport=httpx2.MockTransport(venue))

    monkeypatch.setenv("HCS_BINANCE_TESTNET_KEY", KEY)
    monkeypatch.setenv("HCS_BINANCE_TESTNET_SECRET", SECRET)
    monkeypatch.setattr(httpx2, "AsyncClient", client_factory)
    target = tmp_path / "nested" / "evidence.json"
    monkeypatch.setattr(
        "sys.argv", ["hcs-trader", "rehearsal-binance", "--output", str(target)]
    )
    cli.main()
    assert "evidence written" in capsys.readouterr().out
    text = target.read_text(encoding="utf-8")
    assert json.loads(text)["passed"] is True
    assert KEY not in text and SECRET not in text


def test_cli_rehearsal_nonpassing_run_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    venue = FakeVenue(fail_cancel=True)
    real_client = httpx2.AsyncClient
    monkeypatch.setenv("HCS_BINANCE_TESTNET_KEY", KEY)
    monkeypatch.setenv("HCS_BINANCE_TESTNET_SECRET", SECRET)
    monkeypatch.setattr(
        httpx2,
        "AsyncClient",
        lambda **kw: real_client(transport=httpx2.MockTransport(venue)),
    )
    target = tmp_path / "e.json"
    monkeypatch.setattr(
        "sys.argv", ["hcs-trader", "rehearsal-binance", "--output", str(target)]
    )
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 1
    assert json.loads(target.read_text(encoding="utf-8"))["passed"] is False


# -- Kraken validate probe -------------------------------------------------------


@pytest.mark.anyio
async def test_kraken_validate_probe_sends_validate_true_and_places_nothing() -> None:
    import base64

    secret = base64.b64encode(b"0123456789abcdef0123456789abcdef").decode()
    seen: list[dict[str, str]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(dict(urllib.parse.parse_qsl(request.content.decode())))
        return httpx2.Response(
            200,
            json={"error": [], "result": {"descr": {"order": "buy 0.0001 XBTUSD"}}},
        )

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
        result = await kraken_validate_probe("key", secret, client)
    assert seen[0]["validate"] == "true"
    assert seen[0]["pair"] == "XXBTZUSD"
    assert result == {
        "accepted": True,
        "detail": {"order": "buy 0.0001 XBTUSD"},
        "order_placed": False,
    }


@pytest.mark.anyio
async def test_kraken_validate_probe_reports_rejection() -> None:
    import base64

    secret = base64.b64encode(b"0123456789abcdef0123456789abcdef").decode()

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200, json={"error": ["EOrder:Invalid price"], "result": {}}
        )

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
        result = await kraken_validate_probe("key", secret, client)
    assert result["accepted"] is False and result["order_placed"] is False


def test_cli_kraken_probe_requires_explicit_confirmation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.argv", ["hcs-trader", "kraken-validate-probe"])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 2
    assert "--confirm-validate-only" in capsys.readouterr().err


def test_cli_kraken_probe_missing_credentials(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("HCS_KRAKEN_KEY", raising=False)
    monkeypatch.delenv("HCS_KRAKEN_SECRET", raising=False)
    monkeypatch.setattr(
        "sys.argv", ["hcs-trader", "kraken-validate-probe", "--confirm-validate-only"]
    )
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 1
    assert "HCS_KRAKEN_KEY" in capsys.readouterr().err


def test_cli_kraken_probe_prints_result(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import base64

    secret = base64.b64encode(b"0123456789abcdef0123456789abcdef").decode()
    real_client = httpx2.AsyncClient

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"error": [], "result": {"descr": {}}})

    monkeypatch.setenv("HCS_KRAKEN_KEY", "k")
    monkeypatch.setenv("HCS_KRAKEN_SECRET", secret)
    monkeypatch.setattr(
        httpx2,
        "AsyncClient",
        lambda **kw: real_client(transport=httpx2.MockTransport(handler)),
    )
    monkeypatch.setattr(
        "sys.argv", ["hcs-trader", "kraken-validate-probe", "--confirm-validate-only"]
    )
    cli.main()
    assert json.loads(capsys.readouterr().out)["order_placed"] is False
