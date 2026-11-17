"""Official ECB SDMX daily USD-per-EUR reference observations."""

import asyncio
import csv
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from io import StringIO

import httpx2

from hcs_trader.data.provider import SourceRateLimited, parse_retry_after
from hcs_trader.domain.macro import MacroObservation

SOURCE = "ecb-sdmx"
SERIES = "EXR.D.USD.EUR.SP00.A"
DATA_URL = "https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A"
_MAX_ROWS = 250
_MAX_BYTES = 65_536


class MacroUnavailable(Exception):
    """The macro source did not provide usable observations."""


class MacroRateLimited(MacroUnavailable, SourceRateLimited):
    """The source answered 429/503; ``retry_after`` carries the requested pause."""

    def __init__(self, retry_after: float | None) -> None:
        super().__init__("Macro source is rate limited")
        self.retry_after = retry_after


async def fetch_ecb_rates(client: httpx2.AsyncClient) -> list[MacroObservation]:
    try:
        async with (
            asyncio.timeout(8),
            client.stream(
                "GET",
                DATA_URL,
                params={
                    "format": "csvdata",
                    "detail": "dataonly",
                    "lastNObservations": str(_MAX_ROWS),
                },
                follow_redirects=False,
            ) as response,
        ):
            if response.status_code in (429, 503):
                raise MacroRateLimited(
                    parse_retry_after(response.headers.get("retry-after"))
                )
            response.raise_for_status()
            payload = bytearray()
            async for chunk in response.aiter_bytes():
                if len(payload) + len(chunk) > _MAX_BYTES:
                    raise MacroUnavailable("ECB response exceeded the size limit")
                payload.extend(chunk)
    except (httpx2.HTTPError, TimeoutError) as error:
        raise MacroUnavailable("ECB reference rate request failed") from error

    now = datetime.now(UTC)
    observations: dict[date, MacroObservation] = {}
    try:
        reader = csv.DictReader(
            StringIO(payload.decode("utf-8-sig"), newline=""), strict=True
        )
        if not {"KEY", "TIME_PERIOD", "OBS_VALUE"}.issubset(reader.fieldnames or []):
            raise ValueError("Missing ECB observation columns")
        for row in reader:
            if row["KEY"] != SERIES or len(observations) >= _MAX_ROWS:
                raise ValueError("Unexpected ECB series or observation count")
            day = date.fromisoformat(row["TIME_PERIOD"] or "")
            value = Decimal(row["OBS_VALUE"] or "")
            if day in observations or day > now.date():
                raise ValueError("Duplicate or future ECB reference date")
            if not value.is_finite() or value <= 0:
                raise ValueError("Invalid ECB reference rate")
            observations[day] = MacroObservation(SOURCE, SERIES, day, now, value)
    except (csv.Error, ValueError, InvalidOperation) as error:
        raise MacroUnavailable("Invalid ECB reference rate response") from error
    if not observations:
        raise MacroUnavailable("ECB returned no reference rates")
    return [observations[day] for day in sorted(observations)]
