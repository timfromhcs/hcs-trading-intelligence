import json
import sqlite3
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters
from mcp.types import TextContent

from hcs_trader.domain.execution import SpotOrder, SpotOrderType, SpotSide
from hcs_trader.domain.lifecycle import OrderState
from hcs_trader.domain.wallet import WalletBalance
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
            "news.get_events",
            "portfolio.get_paper_balance",
            "risk.get_status",
            "orders.get_spot_orders",
            "wallets.get_snapshots",
        }
        assert all(
            tool.annotations and tool.annotations.read_only_hint for tool in tools
        )
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

        out_of_range = await client.call_tool("market.get_candles", {"limit": 1001})
        assert out_of_range.is_error
        unknown = await client.call_tool("orders.place_live", {"quantity": "1"})
        assert unknown.is_error


def test_read_only_database_rejects_storage_mutation(tmp_path: Path) -> None:
    path = tmp_path / "readonly.sqlite"
    AuditStore(path).initialize()
    store = AuditStore(path, read_only=True)
    assert store.execution_pause().paused is False
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        store.set_execution_pause(True)
