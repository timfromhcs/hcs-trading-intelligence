import json
from decimal import Decimal

import httpx2
import pytest

from hcs_trader.indicators import Indicators
from hcs_trader.local_research import (
    LlamaCppProvider,
    NoneModelProvider,
    research_report,
)

_EVIDENCE = Indicators(contiguous_candles=15, rsi14=Decimal("51.25"))


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_optional_model_returns_only_research_evidence() -> None:
    disabled = await research_report(_EVIDENCE, NoneModelProvider())
    assert disabled.status == "model_disabled"
    assert disabled.execution_decision == "NO_TRADE"
    assert disabled.evidence == _EVIDENCE


@pytest.mark.anyio
@pytest.mark.parametrize("extra", [False, True])
async def test_model_schema_and_loopback_transport(extra: bool) -> None:
    def respond(request: httpx2.Request) -> httpx2.Response:
        assert str(request.url) == "http://127.0.0.1:9931/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer fixture-token"
        body = json.loads(request.content)
        assert "tools" not in body
        assert body["max_tokens"] == 300
        assert body["response_format"]["schema"]["additionalProperties"] is False
        assert json.loads(body["messages"][1]["content"])["rsi14"] == "51.25"
        assert "fixture-token" not in request.content.decode()
        assessment = {
            "regime": "mixed",
            "thesis": "Mixed trend.",
            "uncertainties": ["Limited sample."],
        }
        if extra:
            assessment["action"] = "buy"
        return httpx2.Response(
            200,
            json={
                "model": "local-fixture",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps(assessment)},
                    }
                ],
            },
        )

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as client:
        provider = LlamaCppProvider(client, 9931, "local-fixture", "fixture-token")
        report = await research_report(_EVIDENCE, provider)
    assert report.execution_decision == "NO_TRADE"
    assert report.prompt_version == "market-research-1"
    if extra:
        assert report.assessment is None and report.status == "unavailable"
    else:
        assert report.assessment is not None and report.assessment.regime == "mixed"


@pytest.mark.anyio
async def test_model_does_not_follow_redirect_or_run_during_indicator_warmup() -> None:
    calls = 0

    def respond(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        assert request.url.host == "127.0.0.1"
        return httpx2.Response(302, headers={"location": "https://example.invalid"})

    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(respond), follow_redirects=True
    ) as client:
        provider = LlamaCppProvider(client, 9931, "local-fixture", "fixture-token")
        warming_up = await research_report(Indicators(0), provider)
        assert warming_up.status == "insufficient_data" and calls == 0
        assert (await research_report(_EVIDENCE, provider)).status == "unavailable"
    assert calls == 1


@pytest.mark.anyio
@pytest.mark.parametrize("problem", [None, "unknown", "arguments", "repeated"])
async def test_opt_in_tool_reads_only_frozen_indicator_snapshot(
    problem: str | None,
) -> None:
    call_id = "call_1"
    call = {
        "id": call_id,
        "type": "function",
        "function": {
            "name": "orders_place"
            if problem == "unknown"
            else "get_archived_indicators",
            "arguments": '{"asset":"ETH"}' if problem == "arguments" else "{}",
        },
    }
    requests = 0

    def respond(request: httpx2.Request) -> httpx2.Response:
        nonlocal requests
        requests += 1
        body = json.loads(request.content)
        assert [tool["function"]["name"] for tool in body["tools"]] == [
            "get_archived_indicators"
        ]
        if requests == 1:
            assert body["tool_choice"] == "required"
            assert "51.25" not in request.content.decode()
        else:
            assert body["tool_choice"] == "none"
            indicators = body["messages"][-1]
            assert (
                indicators["role"] == "tool" and indicators["tool_call_id"] == call_id
            )
            assert json.loads(indicators["content"])["rsi14"] == "51.25"
        if requests == 1 or problem == "repeated":
            choice = {"finish_reason": "tool_calls", "message": {"tool_calls": [call]}}
        else:
            assessment = {
                "regime": "mixed",
                "thesis": "Mixed momentum.",
                "uncertainties": ["Short history."],
            }
            choice = {
                "finish_reason": "stop",
                "message": {"content": json.dumps(assessment)},
            }
        return httpx2.Response(
            200, json={"model": "local-fixture", "choices": [choice]}
        )

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as client:
        provider = LlamaCppProvider(
            client, 9931, "local-fixture", "fixture-token", use_tools=True
        )
        report = await research_report(_EVIDENCE, provider)
    assert requests == (1 if problem in {"unknown", "arguments"} else 2)
    assert report.execution_decision == "NO_TRADE"
    assert report.status == ("available" if problem is None else "unavailable")
