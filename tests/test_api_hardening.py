from __future__ import annotations

import asyncio
import io
import json
import logging
from pathlib import Path

import httpx2
import pytest
from fastapi.testclient import TestClient

from hcs_trader.api import create_app, generate_api_token
from hcs_trader.logging_config import (
    CorrelationFilter,
    JsonFormatter,
    RedactingFilter,
    correlation_id,
    redact,
)
from hcs_trader.network_policy import (
    NetworkBlocked,
    NetworkPolicy,
    SystemMode,
    build_http_client,
)
from hcs_trader.storage import AuditStore

TOKEN = "t" * 32
BASE = "http://127.0.0.1"


def _client(tmp_path: Path, **kwargs: object) -> TestClient:
    store = AuditStore(tmp_path / "a.sqlite3")
    return TestClient(create_app(store, **kwargs), base_url=BASE)  # type: ignore[arg-type]


# ---- X-01 logging ----------------------------------------------------------


def test_redaction_removes_planted_secrets() -> None:
    secrets_ = [
        "FREDKEY1234567890abcdef",
        "kraken-secret-value-XYZ",
        "hunter2passphrase",
        "abcdef123456tokenvalue",
    ]
    text = (
        f"api_key={secrets_[0]} KRAKEN_API_SECRET: {secrets_[1]} "
        f'{{"passphrase": "{secrets_[2]}"}} Authorization: Bearer {secrets_[3]}'
    )
    out = redact(text)
    for secret in secrets_:
        assert secret not in out


def test_json_formatter_redacts_and_carries_correlation_id() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(CorrelationFilter())
    handler.addFilter(RedactingFilter())
    handler.setFormatter(JsonFormatter())
    log = logging.getLogger("hcs.test.redact")
    log.handlers = [handler]
    log.propagate = False
    log.setLevel(logging.INFO)
    reset = correlation_id.set("req-123")
    try:
        log.info("calling with password=%s", "s3cretpw")
        try:
            raise RuntimeError("boom token=abc123secret")
        except RuntimeError:
            log.exception("failed")
    finally:
        correlation_id.reset(reset)
    lines = [json.loads(x) for x in stream.getvalue().splitlines()]
    assert all(entry["correlation_id"] == "req-123" for entry in lines)
    blob = stream.getvalue()
    assert "s3cretpw" not in blob
    assert "abc123secret" not in blob


def test_response_carries_request_id(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.get("/api/v1/health", headers={"X-Request-ID": "abc-1"})
        assert response.headers["x-request-id"] == "abc-1"
        bad = client.get("/api/v1/health", headers={"X-Request-ID": "bad id\t!"})
        assert bad.headers["x-request-id"] != "bad id\t!"


# ---- X-03 API hardening ----------------------------------------------------


def test_generated_tokens_are_unique_and_long() -> None:
    assert generate_api_token() != generate_api_token()
    assert len(generate_api_token()) >= 32


def test_short_token_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        create_app(AuditStore(tmp_path / "a.sqlite3"), api_token="short")


def test_mutating_requires_token_reads_do_not(tmp_path: Path) -> None:
    with _client(tmp_path, api_token=TOKEN) as client:
        assert client.get("/api/v1/health").status_code == 200
        assert client.post("/api/v1/risk/pause").status_code == 401
        wrong = client.post("/api/v1/risk/pause", headers={"X-HCS-Token": "x" * 32})
        assert wrong.status_code == 401
        ok = client.post("/api/v1/risk/pause", headers={"X-HCS-Token": TOKEN})
        assert ok.status_code == 200
        bearer = client.post(
            "/api/v1/risk/resume", headers={"Authorization": f"Bearer {TOKEN}"}
        )
        assert bearer.status_code == 200


def test_token_never_echoed_in_error(tmp_path: Path) -> None:
    with _client(tmp_path, api_token=TOKEN) as client:
        response = client.post("/api/v1/risk/pause", headers={"X-HCS-Token": "z" * 32})
        assert TOKEN not in response.text


def test_cross_origin_post_rejected(tmp_path: Path) -> None:
    with _client(tmp_path, api_token=TOKEN) as client:
        response = client.post(
            "/api/v1/risk/pause",
            headers={"X-HCS-Token": TOKEN, "Origin": "https://evil.example"},
        )
        assert response.status_code == 403


def test_body_size_limit(tmp_path: Path) -> None:
    with _client(tmp_path, api_token=TOKEN, max_body_bytes=100) as client:
        response = client.post(
            "/api/v1/orders/paper",
            content=b"{" + b" " * 500 + b"}",
            headers={"X-HCS-Token": TOKEN, "Content-Type": "application/json"},
        )
        assert response.status_code == 413


def test_non_json_body_rejected(tmp_path: Path) -> None:
    with _client(tmp_path, api_token=TOKEN) as client:
        response = client.post(
            "/api/v1/orders/paper",
            content=b"a=b",
            headers={
                "X-HCS-Token": TOKEN,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        assert response.status_code == 415


def test_write_rate_limit(tmp_path: Path) -> None:
    with _client(tmp_path, api_token=TOKEN, write_rate_limit=3) as client:
        codes = [
            client.post(
                "/api/v1/risk/pause", headers={"X-HCS-Token": TOKEN}
            ).status_code
            for _ in range(5)
        ]
        assert codes[:3] == [200, 200, 200]
        assert codes[3:] == [429, 429]
        assert client.get("/api/v1/health").status_code == 200


def test_non_loopback_peer_rejected(tmp_path: Path) -> None:
    app = create_app(AuditStore(tmp_path / "a.sqlite3"))
    with TestClient(app, base_url=BASE, client=("203.0.113.9", 5555)) as client:
        assert client.get("/api/v1/health").status_code == 403
    with TestClient(app, base_url=BASE, client=("127.0.0.1", 5555)) as client:
        assert client.get("/api/v1/health").status_code == 200


def test_foreign_host_header_rejected(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.get("/api/v1/health", headers={"Host": "evil.example"})
        assert response.status_code == 400


# ---- X-06 modes and network policy -----------------------------------------


def test_live_policy_without_execution_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        create_app(
            AuditStore(tmp_path / "a.sqlite3"),
            network_policy=NetworkPolicy(SystemMode.LIVE),
        )


def test_status_reports_policy_mode(tmp_path: Path) -> None:
    with _client(tmp_path, network_policy=NetworkPolicy(SystemMode.OFFLINE)) as client:
        body = client.get("/api/v1/system/status").json()
        assert body["mode"] == "OFFLINE"
        assert body["network_enabled"] is False
        assert body["execution_available"] is False


@pytest.mark.parametrize("mode", list(SystemMode))
def test_loopback_always_allowed(mode: SystemMode) -> None:
    NetworkPolicy(mode).check("http://127.0.0.1:8080/x")


def test_offline_blocks_everything_remote() -> None:
    policy = NetworkPolicy(SystemMode.OFFLINE)
    with pytest.raises(NetworkBlocked):
        policy.check("https://api.kraken.com/0/public/OHLC")


@pytest.mark.parametrize("mode", [SystemMode.DATA_ONLY, SystemMode.PAPER])
def test_data_modes_cannot_reach_private_or_unknown(mode: SystemMode) -> None:
    policy = NetworkPolicy(mode)
    policy.check("https://api.kraken.com/0/public/Depth")
    with pytest.raises(NetworkBlocked):
        policy.check("https://api.kraken.com/0/private/AddOrder")
    with pytest.raises(NetworkBlocked):
        policy.check("https://evil.example/")
    with pytest.raises(NetworkBlocked):
        policy.check("http://api.kraken.com/0/public/Depth")


def test_live_allows_private_kraken_only_on_allowlisted_host() -> None:
    policy = NetworkPolicy(SystemMode.LIVE)
    policy.check("https://api.kraken.com/0/private/AddOrder")
    with pytest.raises(NetworkBlocked):
        policy.check("https://evil.example/0/private/AddOrder")


def test_offline_client_makes_zero_network_calls() -> None:
    asyncio.run(_offline_case())


async def _offline_case() -> None:
    calls: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(str(request.url))
        return httpx2.Response(200)

    client = build_http_client(
        NetworkPolicy(SystemMode.OFFLINE), transport=httpx2.MockTransport(handler)
    )
    async with client:
        with pytest.raises(NetworkBlocked):
            await client.get("https://api.kraken.com/0/public/Depth")
    assert calls == []


def test_data_only_client_blocks_private_endpoint() -> None:
    asyncio.run(_data_only_case())


async def _data_only_case() -> None:
    calls: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(str(request.url))
        return httpx2.Response(200, json={})

    client = build_http_client(
        NetworkPolicy(SystemMode.DATA_ONLY), transport=httpx2.MockTransport(handler)
    )
    async with client:
        await client.get("https://api.kraken.com/0/public/Depth")
        with pytest.raises(NetworkBlocked):
            await client.post("https://api.kraken.com/0/private/Balance")
    assert calls == ["https://api.kraken.com/0/public/Depth"]
