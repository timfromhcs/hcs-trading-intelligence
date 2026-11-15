"""Structured market commentary from a loopback-only inference service."""

import asyncio
from dataclasses import dataclass
from typing import Annotated, Literal, Protocol

import httpx2
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from hcs_trader.indicators import Indicators
from hcs_trader.model_manifest import ModelMetadata

_PROMPT_VERSION = "market-research-1"
_MAX_TOKENS = 300
_INDICATORS = TypeAdapter(Indicators)


class ResearchOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    regime: Literal["uptrend", "downtrend", "mixed", "uncertain"]
    thesis: Annotated[str, Field(min_length=1, max_length=600)]
    uncertainties: list[Annotated[str, Field(min_length=1, max_length=180)]] = Field(
        min_length=1, max_length=3
    )


@dataclass(frozen=True, slots=True)
class ResearchReport:
    status: Literal["available", "model_disabled", "unavailable", "insufficient_data"]
    evidence: Indicators
    prompt_version: str = _PROMPT_VERSION
    assessment: ResearchOutput | None = None
    model: str | None = None
    execution_decision: Literal["NO_TRADE"] = "NO_TRADE"
    verified_artifact: ModelMetadata | None = None
    max_generation_tokens: int = _MAX_TOKENS
    temperature: int = 0


class LocalModelProvider(Protocol):
    model: str | None

    async def assess(self, evidence: Indicators) -> ResearchOutput: ...


class ModelUnavailable(Exception):
    """The optional local inference service did not produce a valid response."""


class NoneModelProvider:
    model: str | None = None

    async def assess(self, evidence: Indicators) -> ResearchOutput:
        raise ModelUnavailable("No local model configured")


class _Message(BaseModel):
    content: str


class _Choice(BaseModel):
    finish_reason: Literal["stop"]
    message: _Message


class _Completion(BaseModel):
    model: str
    choices: list[_Choice] = Field(min_length=1, max_length=1)


class LlamaCppProvider:
    def __init__(
        self, client: httpx2.AsyncClient, port: int, model: str, token: str
    ) -> None:
        if not 1 <= port <= 65535 or not model or not token:
            raise ValueError("Local model port, alias and token are required")
        self._client = client
        self._token = token
        self._url = f"http://127.0.0.1:{port}/v1/chat/completions"
        self.model: str | None = model

    async def assess(self, evidence: Indicators) -> ResearchOutput:
        if evidence.rsi14 is None:
            raise ModelUnavailable("Indicators are still warming up")
        instructions = (
            "Summarize the supplied archived BTC/USD indicator data. "
            "Discuss uncertainty. Provide research commentary only. "
            "Do not prescribe orders, quantities, transfers, or actions."
        )
        data = _INDICATORS.dump_json(evidence).decode("utf-8")
        try:
            async with (
                asyncio.timeout(20),
                self._client.stream(
                    "POST",
                    self._url,
                    headers={"Authorization": f"Bearer {self._token}"},
                    json={
                        "model": self.model,
                        "messages": [
                            {"role": "system", "content": instructions},
                            {"role": "user", "content": data},
                        ],
                        "stream": False,
                        "temperature": 0,
                        "max_tokens": _MAX_TOKENS,
                        "response_format": {
                            "type": "json_object",
                            "schema": ResearchOutput.model_json_schema(),
                        },
                    },
                    follow_redirects=False,
                ) as response,
            ):
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
            return ResearchOutput.model_validate_json(
                envelope.choices[0].message.content
            )
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
