import json
import sqlite3
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters
from mcp.types import TextContent, TextResourceContents

from hcs_trader.domain.execution import SpotOrder, SpotOrderType, SpotSide
from hcs_trader.domain.lifecycle import OrderState
from hcs_trader.domain.wallet import WalletBalance
from hcs_trader.indicators import Indicators
from hcs_trader.local_research import ResearchRun
from hcs_trader.storage import AuditStore


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_mcp_stdio_exposes_only_bounded_read_tools(tmp_path: Path) -> None:
    path = tmp_path / "market data.sqlite"
    store = AuditStore(path)
    store.initialize()
    store.set_execution_pause(True)
    now = datetime.now(UTC)
    store.record_research_run(
        ResearchRun(
            "fixture-run",
            now,
            8,
            60,
            "model_disabled",
            None,
            None,
            "market-research-1",
            Indicators(0),
        )
    )
    store.save_spot_order(
        SpotOrder(
            client_order_id="cl-mcp-01",
            order_id="ord-mcp-01",
            venue="kraken",
            market="BTC/USD",
            side=SpotSide.BUY,
            order_type=SpotOrderType.LIMIT,
            quantity=Decimal("0.05"),
            limit_price=Decimal("51000.00"),
            state=OrderState.ACKNOWLEDGED,
            risk_decision_id="risk-mcp-01",
            created_at=now,
            updated_at=now,
            venue_order_id="V-MCP-01",
        )
    )
    store.save_wallet_snapshot(
        WalletBalance(
            address="0x1234567890123456789012345678901234567890",
            asset="ETH",
            base_units="1500000000000000000",
            decimals=18,
            block_number=19000000,
            block_hash="0xabcdef",
            observed_at=now,
        )
    )
    transport = StdioServerParameters(
        command=sys.executable,
        args=[
            "-c",
            "from pathlib import Path; import sys; "
            "from hcs_trader.mcp_server import create_mcp_server; "
            "create_mcp_server(Path(sys.argv[1])).run()",
            str(path),
        ],
    )
    async with Client(transport, read_timeout_seconds=10) as client:
        tools = (await client.list_tools()).tools
        assert {tool.name for tool in tools} == {
            "market.get_quotes",
            "market.get_candles",
            "quant.get_indicators",
            "research.get_runs",
            "news.get_events",
            "portfolio.get_paper_balance",
            "risk.get_status",
            "orders.get_spot_orders",
            "wallets.get_snapshots",
            "audit.get_events",
            "quant.compare_strategy_candidates",
        }
        assert all(
            tool.annotations and tool.annotations.read_only_hint for tool in tools
        )
        history = await client.call_tool("research.get_runs")
        assert not history.is_error
        assert isinstance(history.content[0], TextContent)
        history_data = json.loads(history.content[0].text)
        assert history_data["runs"][0]["run_id"] == "fixture-run"
        assert history_data["statistics"] == [
            {"status": "model_disabled", "count": 1, "mean_duration_ms": 8.0}
        ]
        risk = await client.call_tool("risk.get_status")
        assert isinstance(risk.content[0], TextContent)
        assert json.loads(risk.content[0].text)["paused"] is True
        portfolio = await client.call_tool("portfolio.get_paper_balance")
        assert isinstance(portfolio.content[0], TextContent)
        assert json.loads(portfolio.content[0].text)["cash"] == "10000"
        orders_resp = await client.call_tool(
            "orders.get_spot_orders", {"venue": "kraken"}
        )
        assert len(orders_resp.content) == 1
        assert isinstance(orders_resp.content[0], TextContent)
        order_data = json.loads(orders_resp.content[0].text)
        assert order_data["client_order_id"] == "cl-mcp-01"
        assert order_data["state"] == "ACKNOWLEDGED"

        wallets_resp = await client.call_tool("wallets.get_snapshots", {"limit": 10})
        assert len(wallets_resp.content) == 1
        assert isinstance(wallets_resp.content[0], TextContent)
        wallet_data = json.loads(wallets_resp.content[0].text)
        assert wallet_data["asset"] == "ETH"

        audit_resp = await client.call_tool("audit.get_events", {"limit": 10})
        assert len(audit_resp.content) >= 1
        assert isinstance(audit_resp.content[0], TextContent)
        audit_data = json.loads(audit_resp.content[0].text)
        assert "event_id" in audit_data
        assert "kind" in audit_data

        metrics = {
            "champion_net_return": "0.10",
            "champion_max_drawdown": "0.15",
            "champion_trades": 10,
            "challenger_net_return": "0.18",
            "challenger_max_drawdown": "0.10",
            "challenger_trades": 12,
        }
        compare_resp = await client.call_tool(
            "quant.compare_strategy_candidates", metrics
        )
        assert len(compare_resp.content) == 1
        assert isinstance(compare_resp.content[0], TextContent)
        compare_data = json.loads(compare_resp.content[0].text)
        assert compare_data["challenger_wins"] is True
        invalid_metrics = await client.call_tool(
            "quant.compare_strategy_candidates",
            {**metrics, "challenger_net_return": "NaN"},
        )
        assert invalid_metrics.is_error

        out_of_range = await client.call_tool("market.get_candles", {"limit": 1001})
        assert out_of_range.is_error
        unknown = await client.call_tool("orders.place_live", {"quantity": "1"})
        assert unknown.is_error

        # Verify MCP Resources
        resources = await client.list_resources()
        resource_uris = {str(r.uri) for r in resources.resources}
        assert "hcs://system/capabilities" in resource_uris
        assert "hcs://risk/policy" in resource_uris
        assert "hcs://skills/registry" in resource_uris
        assert "hcs://audit/recent" in resource_uris

        policy_res = await client.read_resource("hcs://risk/policy")
        assert len(policy_res.contents) >= 1
        content_item = policy_res.contents[0]
        assert isinstance(content_item, TextResourceContents)
        policy_data = json.loads(content_item.text)
        assert policy_data["kill_switch_paused"] is True
        assert policy_data["min_notional_usd"] == "10.00"

        # Verify MCP Prompts
        prompts = await client.list_prompts()
        prompt_names = {p.name for p in prompts.prompts}
        assert "market_brief" in prompt_names
        assert "risk_review" in prompt_names
        assert "strategy_postmortem" in prompt_names
        assert "release_checklist" in prompt_names

        brief = await client.get_prompt("market_brief", {"market": "BTC/USD"})
        assert len(brief.messages) == 1
        msg_content = brief.messages[0].content
        assert isinstance(msg_content, TextContent)
        assert "BTC/USD" in msg_content.text


def test_read_only_database_rejects_storage_mutation(tmp_path: Path) -> None:
    path = tmp_path / "readonly.sqlite"
    AuditStore(path).initialize()
    store = AuditStore(path, read_only=True)
    assert store.execution_pause().paused is False
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        store.set_execution_pause(True)
