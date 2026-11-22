"""W1-D: Host/Origin/content-type/CSP hardening for REST and MCP HTTP."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from mcp import Client
from starlette.testclient import TestClient as StarletteClient

from hcs_trader import mcp_server
from hcs_trader.__main__ import main
from hcs_trader.api import create_app
from hcs_trader.loopback import HostPolicy, fetch_site_allowed
from hcs_trader.mcp_server import (
    _TOOL_TIERS,
    CapabilityTier,
    ClientRegistry,
    create_http_app,
    create_mcp_server,
)
from hcs_trader.skills import list_skills
from hcs_trader.storage import AuditStore

PAUSE = "/api/v1/risk/pause"
LOCAL = ("127.0.0.1", 5000)


def _api(tmp_path: Path, base: str = "http://127.0.0.1", **kwargs: Any) -> TestClient:
    store = AuditStore(tmp_path / "a.sqlite3")
    return TestClient(create_app(store, **kwargs), base_url=base)


# ---- policy unit tests -------------------------------------------------------


@pytest.mark.parametrize(
    "host",
    [
        "localhost:8765",
        "127.0.0.1:8765",
        "[::1]:8765",
    ],
)
def test_host_policy_accepts_local_hosts_on_bound_port(host: str) -> None:
    assert HostPolicy(8765).host_allowed(host)


@pytest.mark.parametrize(
    "host",
    [
        "evil.example",
        "127.0.0.1.evil.example",
        "evil.example:8765",
        "localhost:9999",
        "user@127.0.0.1:8765",
        "127.0.0.1:8765/path",
        "127.0.0.1:notaport",
        "",
        None,
    ],
)
def test_host_policy_rejects_foreign_or_malformed_hosts(host: str | None) -> None:
    assert not HostPolicy(8765).host_allowed(host)


def test_host_policy_without_port_pin_accepts_bare_local_names() -> None:
    policy = HostPolicy()
    for host in ("localhost", "127.0.0.1", "[::1]", "127.0.0.1:1234"):
        assert policy.host_allowed(host)


def test_host_policy_extra_hosts_and_origin() -> None:
    policy = HostPolicy(8765, ("hcs.local",))
    assert policy.host_allowed("hcs.local:8765")
    assert policy.origin_allowed("http://localhost:8765")
    assert not policy.origin_allowed("https://localhost:8765")
    assert not policy.origin_allowed("http://localhost:1")
    assert not policy.origin_allowed("http://evil.example:8765")
    assert not policy.origin_allowed("null")
    assert fetch_site_allowed(None)
    assert fetch_site_allowed("same-origin")
    assert fetch_site_allowed("none")
    assert not fetch_site_allowed("same-site")
    assert not fetch_site_allowed("cross-site")


# ---- REST ---------------------------------------------------------------------


def test_api_dns_rebinding_host_fails_closed(tmp_path: Path) -> None:
    with _api(tmp_path, bound_port=8765) as client:
        for host in ("attacker.example:8765", "127.0.0.1:1", "127.0.0.1.evil.test"):
            r = client.get("/api/v1/health", headers={"Host": host})
            assert r.status_code == 400, host
            assert "content-security-policy" in r.headers
        for host in ("127.0.0.1:8765", "localhost:8765", "[::1]:8765"):
            assert (
                client.get("/api/v1/health", headers={"Host": host}).status_code == 200
            )


def test_api_extra_allowed_host_is_configurable(tmp_path: Path) -> None:
    with _api(tmp_path, extra_allowed_hosts=("hcs.local",)) as client:
        ok = client.get("/api/v1/health", headers={"Host": "hcs.local"})
        assert ok.status_code == 200


def test_api_foreign_origin_write_rejected(tmp_path: Path) -> None:
    with _api(tmp_path, bound_port=8765, base="http://127.0.0.1:8765") as client:
        bad = client.post(PAUSE, headers={"Origin": "http://evil.example"})
        assert bad.status_code == 403
        wrong_port = client.post(PAUSE, headers={"Origin": "http://127.0.0.1:9"})
        assert wrong_port.status_code == 403
        site = client.post(PAUSE, headers={"Sec-Fetch-Site": "same-site"})
        assert site.status_code == 403
        cross = client.post(PAUSE, headers={"Sec-Fetch-Site": "cross-site"})
        assert cross.status_code == 403
        good = client.post(
            PAUSE,
            headers={
                "Origin": "http://127.0.0.1:8765",
                "Sec-Fetch-Site": "same-origin",
            },
        )
        assert good.status_code == 200
        none = client.post(PAUSE, headers={"Sec-Fetch-Site": "none"})
        assert none.status_code == 200


def test_api_wrong_content_type_rejected_on_write(tmp_path: Path) -> None:
    with _api(tmp_path) as client:
        for ctype in ("text/plain", "application/x-www-form-urlencoded", ""):
            r = client.post(PAUSE, content=b"{}", headers={"Content-Type": ctype})
            assert r.status_code == 415, ctype
        ok = client.post(
            PAUSE, content=b"{}", headers={"Content-Type": "application/json"}
        )
        assert ok.status_code == 200


def test_api_oversize_body_rejected(tmp_path: Path) -> None:
    with _api(tmp_path, max_body_bytes=64) as client:
        r = client.post(
            PAUSE,
            content=b"{" + b" " * 200 + b"}",
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 413


def test_api_csp_and_security_headers_on_api_rejections_and_ui(tmp_path: Path) -> None:
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text(
        '<!doctype html><script type="module" src="/a.js"></script>', encoding="utf-8"
    )
    store = AuditStore(tmp_path / "a.sqlite3")
    app = create_app(store, ui)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        responses = [
            client.get("/api/v1/health"),
            client.get("/"),
            client.post(PAUSE, content=b"x", headers={"Content-Type": "text/plain"}),
        ]
    for response in responses:
        csp = response.headers["content-security-policy"]
        assert "default-src 'self'" in csp
        assert "connect-src 'self'" in csp
        assert "frame-ancestors 'none'" in csp
        assert "unsafe-inline" not in csp
        assert "unsafe-eval" not in csp
        assert not re.search(r"script-src[^;]*'unsafe", csp)
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"
    assert responses[1].status_code == 200
    assert "<script type" in responses[1].text


def test_token_behaviour_unchanged(tmp_path: Path) -> None:
    token = "t" * 32
    with _api(tmp_path, api_token=token) as client:
        assert client.post(PAUSE).status_code == 401
        ok = client.post(PAUSE, headers={"Authorization": f"Bearer {token}"})
        assert ok.status_code == 200


# ---- MCP Streamable HTTP --------------------------------------------------------


def _mcp(tmp_path: Path, port: int | None = 8766) -> tuple[Any, str]:
    store = AuditStore(tmp_path / "m.sqlite3")
    store.initialize()
    registry = ClientRegistry()
    token = registry.register("reader", frozenset({CapabilityTier.READ}))
    server = create_mcp_server(store.path, granted_tiers=frozenset())
    return create_http_app(server, registry, port=port), token


def _headers(token: str | None, **extra: str) -> dict[str, str]:
    base = {
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
    }
    if token:
        base["authorization"] = f"Bearer {token}"
    base.update(extra)
    return base


_CALL = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {"name": "risk.get_status", "arguments": {}},
}


def test_mcp_http_dns_rebinding_host_fails_closed(tmp_path: Path) -> None:
    app, token = _mcp(tmp_path)
    with StarletteClient(app, base_url="http://127.0.0.1:8766", client=LOCAL) as http:
        for host in ("attacker.example:8766", "127.0.0.1:1", "127.0.0.1.evil.test"):
            r = http.post("/mcp", headers=_headers(token, host=host), json=_CALL)
            assert r.status_code == 403, host
        ok = http.post(
            "/mcp", headers=_headers(token, host="localhost:8766"), json=_CALL
        )
        assert ok.status_code == 200


def test_mcp_http_foreign_origin_and_fetch_site_rejected(tmp_path: Path) -> None:
    app, token = _mcp(tmp_path)
    with StarletteClient(app, base_url="http://127.0.0.1:8766", client=LOCAL) as http:
        for extra in (
            {"origin": "http://evil.example"},
            {"origin": "http://127.0.0.1:1"},
            {"sec-fetch-site": "cross-site"},
        ):
            r = http.post("/mcp", headers=_headers(token, **extra), json=_CALL)
            assert r.status_code == 403, extra
        ok = http.post(
            "/mcp",
            headers=_headers(token, origin="http://127.0.0.1:8766"),
            json=_CALL,
        )
        assert ok.status_code == 200


def test_mcp_http_malformed_jsonrpc_is_a_400_jsonrpc_error(tmp_path: Path) -> None:
    app, token = _mcp(tmp_path)
    with StarletteClient(app, base_url="http://127.0.0.1:8766", client=LOCAL) as http:
        for body in (b"{not json", b"[]", b'{"jsonrpc":"2.0","id":1}', b'{"id":"x"}'):
            r = http.post("/mcp", headers=_headers(token), content=body)
            assert r.status_code == 400, body
            error = r.json()["error"]
            assert error["code"] in {-32700, -32600, -32602}, body
        # no token: still rejected before parsing
        r = http.post("/mcp", headers=_headers(None), content=b"{not json")
        assert r.status_code == 401


def test_mcp_http_cli_is_loopback_only_and_prints_token_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, Any] = {}

    def fake_run(server: Any, registry: ClientRegistry, port: int = 0) -> None:
        captured.update(registry=registry, port=port)

    monkeypatch.setattr(mcp_server, "run_http", fake_run)
    monkeypatch.setattr(
        "sys.argv",
        [
            "hcs-trader",
            "mcp-http",
            "--data-dir",
            str(tmp_path),
            "--port",
            "9123",
            "--tier",
            "READ",
            "--tier",
            "RESEARCH",
        ],
    )
    main()
    out = capsys.readouterr().out
    token = re.search(r"Bearer token \(shown once, not stored\): (\S+)", out)
    assert token is not None
    assert "http://127.0.0.1:9123/mcp" in out
    grant = captured["registry"].authenticate(token.group(1))
    assert grant is not None
    assert grant.tiers == {CapabilityTier.READ, CapabilityTier.RESEARCH}
    assert captured["port"] == 9123
    monkeypatch.setattr("sys.argv", ["hcs-trader", "mcp-http", "--tier", "LIVE_TRADE"])
    with pytest.raises(SystemExit):
        main()


def test_run_http_binds_loopback_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import uvicorn

    seen: dict[str, Any] = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(kw))
    store = AuditStore(tmp_path / "r.sqlite3")
    store.initialize()
    server = create_mcp_server(store.path)
    mcp_server.run_http(server, ClientRegistry(), port=9000)
    assert seen["host"] == "127.0.0.1" and seen["port"] == 9000


# ---- skills ------------------------------------------------------------------------


@pytest.mark.anyio
async def test_skill_required_tools_are_registered_and_fit_tier(tmp_path: Path) -> None:
    store = AuditStore(tmp_path / "s.sqlite3")
    store.initialize()
    server = create_mcp_server(store.path)
    async with Client(server) as client:
        registered = {t.name for t in (await client.list_tools()).tools}
    order = list(CapabilityTier)
    for skill in list_skills():
        assert skill.required_tools, skill.name
        tier = CapabilityTier(skill.capability_tier)
        for tool in skill.required_tools:
            assert tool in registered, f"{skill.name}: {tool} is not a registered tool"
            assert order.index(_TOOL_TIERS[tool]) <= order.index(tier), (
                skill.name,
                tool,
            )


def test_skill_frontmatter_follows_agent_skills_format() -> None:
    allowed = {
        "name",
        "description",
        "license",
        "compatibility",
        "metadata",
        "allowed-tools",
    }
    for skill in list_skills():
        text = skill.path.read_text(encoding="utf-8").replace("\r\n", "\n")
        header = text.split("\n---\n", 1)[0].removeprefix("---\n")
        top_level = {
            line.split(":", 1)[0]
            for line in header.splitlines()
            if line and line[0] != " "
        }
        assert top_level <= allowed, (skill.name, top_level - allowed)
        assert {"name", "description"} <= top_level
        assert skill.description
        assert skill.path.parent.name == skill.name
        assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", skill.name)


# ---- SDK pin ----------------------------------------------------------------------


def test_installed_mcp_matches_pin_and_lockfile() -> None:
    import tomllib
    from importlib.metadata import version

    root = Path(__file__).resolve().parents[1]
    lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    locked = next(p["version"] for p in lock["package"] if p["name"] == "mcp")
    assert version("mcp") == locked
    assert version("mcp").split(".")[0] == "2"


@pytest.mark.parametrize("port", ["0", "65536", "-1"])
def test_mcp_http_rejects_out_of_range_port(
    port: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.argv", ["hcs-trader", "mcp-http", "--port", port])
    with pytest.raises(SystemExit):
        main()
