"""MCP capability-tier permission matrix."""

import json
from pathlib import Path

import pytest
from mcp import Client
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from hcs_trader.mcp_server import (
    _TOOL_TIERS,
    DEFAULT_GRANTED_TIERS,
    CapabilityGuard,
    CapabilityTier,
    InvocationAudit,
    create_mcp_server,
)
from hcs_trader.storage import AuditStore


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _db(tmp_path: Path) -> Path:
    path = tmp_path / "matrix.sqlite"
    AuditStore(path).initialize()
    return path


def _audit_outcomes(path: Path) -> list[tuple[str, str]]:
    log = path.with_name("mcp_invocations.jsonl")
    if not log.exists():
        return []
    rows = [json.loads(line) for line in log.read_text("utf-8").splitlines()]
    return [(row["tool"], row["outcome"]) for row in rows]


def test_default_grant_is_read_only_and_no_tool_exceeds_it() -> None:
    assert DEFAULT_GRANTED_TIERS == {CapabilityTier.READ}
    # Non-READ tools exist but none reaches LIVE_TRADE.
    assert CapabilityTier.LIVE_TRADE not in set(_TOOL_TIERS.values())
    assert {n for n, t in _TOOL_TIERS.items() if t is not CapabilityTier.READ} == {
        "quant.run_backtest",
        "research.create_candidate",
        "orders.preview",
        "orders.place_paper",
        "system.pause",
    }


def test_tier_set_is_exactly_the_documented_five() -> None:
    assert {tier.value for tier in CapabilityTier} == {
        "READ",
        "RESEARCH",
        "PAPER_TRADE",
        "LIVE_TRADE",
        "ADMIN_LOCAL",
    }


@pytest.mark.parametrize("tool_tier", list(CapabilityTier))
@pytest.mark.parametrize("granted_tier", list(CapabilityTier))
def test_guard_allows_only_exactly_granted_tier(
    tmp_path: Path, tool_tier: CapabilityTier, granted_tier: CapabilityTier
) -> None:
    audit = InvocationAudit(tmp_path / "audit.jsonl")
    guard = CapabilityGuard(frozenset({granted_tier}), audit)
    server = MCPServer("matrix")
    calls: list[str] = []

    @guard.tool(server, "probe.tool", tool_tier)
    def probe() -> str:
        calls.append("ran")
        return "ok"

    if tool_tier == granted_tier:
        assert probe() == "ok"
        assert calls == ["ran"]
        outcome = "ok"
    else:
        # No tier implies another: not even ADMIN_LOCAL grants READ.
        with pytest.raises(ToolError, match="not granted"):
            probe()
        assert calls == []
        outcome = "denied"
    rows = [json.loads(x) for x in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert [row["outcome"] for row in rows] == [outcome]
    assert rows[0]["capability_tier"] == tool_tier.value


@pytest.mark.anyio
async def test_real_server_denies_every_tool_without_grant(tmp_path: Path) -> None:
    path = _db(tmp_path)
    server = create_mcp_server(path, granted_tiers=frozenset())
    async with Client(server) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        assert names == set(_TOOL_TIERS)
        for name in sorted(names):
            result = await client.call_tool(name, {})
            assert result.is_error, name
    outcomes = _audit_outcomes(path)
    # Tools with required arguments fail schema validation before the guard,
    # so only the tools reached are audited; none may ever be audited "ok".
    assert outcomes
    assert {tool for tool, _ in outcomes} <= names
    assert {outcome for _, outcome in outcomes} == {"denied"}


@pytest.mark.anyio
@pytest.mark.parametrize(
    "tier",
    [t for t in CapabilityTier if t is not CapabilityTier.READ],
)
async def test_non_read_grants_do_not_unlock_read_tools(
    tmp_path: Path, tier: CapabilityTier
) -> None:
    path = _db(tmp_path)
    server = create_mcp_server(path, granted_tiers=frozenset({tier}))
    async with Client(server) as client:
        result = await client.call_tool("risk.get_status", {})
        assert result.is_error


@pytest.mark.anyio
async def test_read_grant_unlocks_a_read_tool(tmp_path: Path) -> None:
    path = _db(tmp_path)
    server = create_mcp_server(path)
    async with Client(server) as client:
        assert not (await client.call_tool("risk.get_status", {})).is_error
