"""MCP typed outputs, output_schema, cursor pagination and projection."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from mcp import Client

from hcs_trader.domain.news import NewsItem
from hcs_trader.mcp_server import create_mcp_server
from hcs_trader.storage import AuditStore


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _store(path: Path) -> AuditStore:
    store = AuditStore(path)
    store.initialize()
    now = datetime.now(UTC)
    store.record_news(
        [
            NewsItem(
                "fixture",
                f"https://example.org/news/{index}",
                f"Bitcoin market note {index}",
                now,
                now,
            )
            for index in range(5)
        ]
    )
    return store


@pytest.mark.anyio
async def test_structured_tools_declare_output_schema(tmp_path: Path) -> None:
    async with Client(
        create_mcp_server(_store(tmp_path / "audit.sqlite").path)
    ) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    for name in (
        "webfetch.list_sources",
        "webfetch.get",
        "news.search_page",
        "quant.size_position",
        "quant.cost_preview",
    ):
        assert tools[name].output_schema is not None
        assert tools[name].annotations is not None


@pytest.mark.anyio
async def test_webfetch_sources_paginate_with_opaque_cursor(tmp_path: Path) -> None:
    async with Client(
        create_mcp_server(_store(tmp_path / "audit.sqlite").path)
    ) as client:
        first = await client.call_tool("webfetch.list_sources", {"limit": 5})
        assert first.structured_content is not None
        page_one = first.structured_content
        assert page_one["has_more"] is True
        assert len(page_one["sources"]) == 5
        second = await client.call_tool(
            "webfetch.list_sources", {"limit": 5, "cursor": page_one["next_cursor"]}
        )
        assert second.structured_content is not None
        first_ids = {row["id"] for row in page_one["sources"]}
        second_ids = {row["id"] for row in second.structured_content["sources"]}
        assert first_ids.isdisjoint(second_ids)
        cursor = second.structured_content["next_cursor"]
        seen: set[str] = first_ids | second_ids
        for _ in range(10):
            page = await client.call_tool(
                "webfetch.list_sources", {"limit": 5, "cursor": cursor}
            )
            assert page.structured_content is not None
            seen.update(row["id"] for row in page.structured_content["sources"])
            if not page.structured_content["has_more"]:
                break
            cursor = page.structured_content["next_cursor"]
        else:
            raise AssertionError("Pagination did not terminate")
        assert len(seen) == 16
        assert all(not row["enabled"] for row in page_one["sources"])


@pytest.mark.anyio
async def test_news_page_projects_fields_and_rejects_bad_cursor(tmp_path: Path) -> None:
    async with Client(
        create_mcp_server(_store(tmp_path / "audit.sqlite").path)
    ) as client:
        result = await client.call_tool(
            "news.search_page",
            {"query": "Bitcoin", "limit": 2, "fields": ["title", "url"]},
        )
        assert result.structured_content is not None
        items = result.structured_content["items"]
        assert len(items) == 2
        assert set(items[0]) == {"title", "url"}
        assert result.structured_content["has_more"] is True
        bad = await client.call_tool(
            "news.search_page", {"query": "Bitcoin", "cursor": "nope"}
        )
        assert bad.is_error is True


@pytest.mark.anyio
async def test_cost_preview_tool_is_typed_and_read_only(tmp_path: Path) -> None:
    async with Client(
        create_mcp_server(_store(tmp_path / "audit.sqlite").path)
    ) as client:
        result = await client.call_tool(
            "quant.cost_preview", {"reference_price": "100", "side": "buy"}
        )
    assert result.structured_content is not None
    body = result.structured_content
    assert body["authorization"] == "NONE"
    assert float(body["execution_price"]) > 100


@pytest.mark.anyio
async def test_sizing_tool_returns_typed_no_authorization_preview(
    tmp_path: Path,
) -> None:
    async with Client(
        create_mcp_server(_store(tmp_path / "audit.sqlite").path)
    ) as client:
        result = await client.call_tool(
            "quant.size_position",
            {
                "hot_equity": "10000",
                "entry_price": "100",
                "invalidation_price": "95",
                "risk_fraction": "0.0025",
                "max_position_notional": "2000",
                "available_quote": "5000",
            },
        )
    assert result.structured_content is not None
    preview = result.structured_content
    assert preview["authorization"] == "NONE"
    assert float(preview["quantity"]) > 0


@pytest.mark.anyio
async def test_webfetch_get_is_read_only_and_disabled_without_fetcher(
    tmp_path: Path,
) -> None:
    async with Client(
        create_mcp_server(_store(tmp_path / "audit.sqlite").path)
    ) as client:
        result = await client.call_tool("webfetch.get", {"source_id": "ecb-sdmx"})
    assert result.is_error is True
    assert json.dumps(result.model_dump(mode="json"))
