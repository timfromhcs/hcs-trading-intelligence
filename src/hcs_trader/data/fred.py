"""FRED CPI snapshots for a specific real-time period."""

import asyncio
import re
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

import httpx2
from pydantic import BaseModel, Field

from hcs_trader.data.ecb import MacroUnavailable
from hcs_trader.domain.macro import MacroObservation

SOURCE = "fred"
SERIES = "CPIAUCSL"
DATA_URL = "https://api.stlouisfed.org/fred/series/observations"


class _Row(BaseModel):
    date: str
    value: str
    realtime_start: str
    realtime_end: str


class _Result(BaseModel):
    realtime_start: str
    realtime_end: str
    observations: list[_Row] = Field(min_length=1, max_length=250)


async def fetch_fred_cpi(
    client: httpx2.AsyncClient, api_key: str, *, as_of: date | None = None
) -> list[MacroObservation]:
    if not re.fullmatch(r"[a-z0-9]{32}", api_key):
        raise MacroUnavailable("A valid FRED API key is required")
    params = {
        "api_key": api_key,
        "series_id": SERIES,
        "file_type": "json",
        "sort_order": "desc",
        "limit": "250",
    }
    if as_of is not None:
        params.update(realtime_start=as_of.isoformat(), realtime_end=as_of.isoformat())
    try:
        async with (
            asyncio.timeout(8),
            client.stream(
                "GET", DATA_URL, params=params, follow_redirects=False
            ) as response,
        ):
            # Avoid HTTPStatusError, whose message includes the query-string credential.
            if response.status_code != 200:
                raise MacroUnavailable("FRED request failed")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > 65_536:
                    raise MacroUnavailable("FRED response exceeded the size limit")
                body.extend(chunk)
    except (httpx2.HTTPError, TimeoutError):
        raise MacroUnavailable("FRED request failed") from None

    received = datetime.now(UTC)
    try:
        result = _Result.model_validate_json(body, strict=True)
        vintage = date.fromisoformat(result.realtime_start)
        if (
            result.realtime_start != result.realtime_end
            or vintage > received.date()
            or (as_of is not None and vintage != as_of)
        ):
            raise ValueError("Unexpected FRED vintage")
        observations = {}
        for row in result.observations:
            day = date.fromisoformat(row.date)
            if (
                row.realtime_start != result.realtime_start
                or row.realtime_end != result.realtime_end
                or day > vintage
                or day in observations
            ):
                raise ValueError("Unexpected FRED observation")
            # FRED encodes missing observations as a period.
            value = None if row.value == "." else Decimal(row.value)
            if value is not None and (not value.is_finite() or value <= 0):
                raise ValueError("Invalid CPI value")
            observations[day] = value
    except (ValueError, InvalidOperation):
        raise MacroUnavailable("Invalid FRED response") from None
    return [
        MacroObservation(SOURCE, SERIES, day, received, value, vintage)
        for day, value in sorted(observations.items())
        if value is not None
    ]
