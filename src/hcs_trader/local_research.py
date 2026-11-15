"""Structured market commentary from a loopback-only inference service."""

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal, Protocol

import httpx2
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from hcs_trader.indicators import Indicators
from hcs_trader.model_manifest import ModelMetadata

_PROMPT_VERSION = "market-research-1"
_MAX_TOKENS = 300
_INDICATORS = TypeAdapter(Indicators)
type ResearchStatus = Literal[
    "available", "model_disabled", "unavailable", "insufficient_data"
]
_INDICATOR_TOOL = {
    "type": "function",
    "function": {
        "name": "get_archived_indicators",
        "description": "Read deterministic BTC/USD indicators including RSI(14) "
        "from the current archived candle snapshot.",
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
}


class ResearchOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    regime: Literal["uptrend", "downtrend", "mixed", "uncertain"]
    thesis: Annotated[str, Field(min_length=1, max_length=600)]
    uncertainties: list[Annotated[str, Field(min_length=1, max_length=180)]] = Field(
        min_length=1, max_length=3
    )


@dataclass(frozen=True, slots=True)
class ResearchReport:
    status: ResearchStatus
    evidence: Indicators
    prompt_version: str = _PROMPT_VERSION
    assessment: ResearchOutput | None = None
    model: str | None = None
    execution_decision: Literal["NO_TRADE"] = "NO_TRADE"
    verified_artifact: ModelMetadata | None = None
    max_generation_tokens: int = _MAX_TOKENS
    temperature: int = 0


@dataclass(frozen=True, slots=True)
class ResearchRun:
    run_id: str
    started_at: datetime
    duration_ms: int
    interval_minutes: int
    status: ResearchStatus
    model: str | None
    model_sha256: str | None
    prompt_version: str
    evidence: Indicators


@dataclass(frozen=True, slots=True)
class ResearchRunStats:
    status: ResearchStatus
    count: int
    mean_duration_ms: float


@dataclass(frozen=True, slots=True)
class ResearchHistory:
    runs: list[ResearchRun]
    statistics: list[ResearchRunStats]


class LocalModelProvider(Protocol):
    model: str | None

    async def assess(self, evidence: Indicators) -> ResearchOutput: ...


class ModelUnavailable(Exception):
    """The optional local inference service did not produce a valid response."""


class NoneModelProvider:
    model: str | None = None

    async def assess(self, evidence: Indicators) -> ResearchOutput:
        raise ModelUnavailable("No local model configured")


class _ToolFunction(BaseModel):
    name: Literal["get_archived_indicators"]
    arguments: Annotated[str, Field(max_length=64)]


class _ToolCall(BaseModel):
    id: Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]{1,128}$")]
    type: Literal["function"]
    function: _ToolFunction


class _Message(BaseModel):
    content: str | None = None
    tool_calls: list[_ToolCall] | None = None


class _Choice(BaseModel):
    finish_reason: Literal["stop", "tool_calls"]
    message: _Message


class _Completion(BaseModel):
    model: str
    choices: list[_Choice] = Field(min_length=1, max_length=1)


class LlamaCppProvider:
    def __init__(
        self,
        client: httpx2.AsyncClient,
        port: int,
        model: str,
        token: str,
        *,
        use_tools: bool = False,
    ) -> None:
        if not 1 <= port <= 65535 or not model or not token:
            raise ValueError("Local model port, alias and token are required")
        self._client = client
        self._token = token
        self._url = f"http://127.0.0.1:{port}/v1/chat/completions"
        self.model: str | None = model
        self._use_tools = use_tools

    async def _complete(self, request: dict[str, object]) -> _Choice:
        async with self._client.stream(
            "POST",
            self._url,
            headers={"Authorization": f"Bearer {self._token}"},
            json={"model": self.model, "stream": False, "temperature": 0, **request},
            follow_redirects=False,
        ) as response:
            if response.status_code != 200:
                raise ModelUnavailable("Local model request failed")
            payload = bytearray()
            async for chunk in response.aiter_bytes():
                if len(payload) + len(chunk) > 32_768:
                    raise ModelUnavailable("Local model response is too large")
                payload.extend(chunk)
        envelope = _Completion.model_validate_json(payload, strict=True)
        if envelope.model != self.model:
            raise ModelUnavailable("Local model alias did not match")
        return envelope.choices[0]

    async def assess(self, evidence: Indicators) -> ResearchOutput:
        if evidence.rsi14 is None:
            raise ModelUnavailable("Indicators are still warming up")
        instructions = (
            "Summarize the supplied archived BTC/USD indicator data. "
            "Discuss uncertainty. Provide research commentary only. "
            "Do not prescribe orders, quantities, transfers, or actions."
        )
        data = _INDICATORS.dump_json(evidence).decode("utf-8")
        messages: list[dict[str, object]] = [
            {"role": "system", "content": instructions}
        ]
        try:
            async with asyncio.timeout(20):
                tool_options: dict[str, object] = {}
                if self._use_tools:
                    messages.append(
                        {
                            "role": "user",
                            "content": "Call get_archived_indicators for the evidence.",
                        }
                    )
                    choice = await self._complete(
                        {
                            "messages": messages,
                            "tools": [_INDICATOR_TOOL],
                            "tool_choice": "required",
                            "max_tokens": 96,
                        }
                    )
                    calls = choice.message.tool_calls or []
                    if choice.finish_reason != "tool_calls" or len(calls) != 1:
                        raise ModelUnavailable("Expected one indicator tool call")
                    call = calls[0]
                    if json.loads(call.function.arguments) != {}:
                        raise ModelUnavailable("The indicator tool takes no arguments")
                    messages.extend(
                        [
                            {"role": "assistant", "tool_calls": [call.model_dump()]},
                            {"role": "tool", "tool_call_id": call.id, "content": data},
                        ]
                    )
                    tool_options = {"tools": [_INDICATOR_TOOL], "tool_choice": "none"}
                else:
                    messages.append({"role": "user", "content": data})
                final = await self._complete(
                    {
                        "messages": messages,
                        "max_tokens": _MAX_TOKENS,
                        "response_format": {
                            "type": "json_object",
                            "schema": ResearchOutput.model_json_schema(),
                        },
                        **tool_options,
                    }
                )
                if (
                    final.finish_reason != "stop"
                    or final.message.tool_calls
                    or final.message.content is None
                ):
                    raise ModelUnavailable("Expected a final research assessment")
                return ResearchOutput.model_validate_json(final.message.content)
        except (httpx2.HTTPError, TimeoutError, ValueError):
            raise ModelUnavailable(
                "Invalid or unavailable local model response"
            ) from None


async def research_report(
    evidence: Indicators, provider: LocalModelProvider
) -> ResearchReport:
    if provider.model is None:
        return ResearchReport("model_disabled", evidence)
    if evidence.rsi14 is None:
        return ResearchReport("insufficient_data", evidence, model=provider.model)
    try:
        assessment = await provider.assess(evidence)
    except ModelUnavailable:
        return ResearchReport("unavailable", evidence, model=provider.model)
    return ResearchReport(
        "available", evidence, assessment=assessment, model=provider.model
    )
