from datetime import date
from pathlib import Path

import httpx2
import pytest
from fastapi.testclient import TestClient

from hcs_trader.api import create_app
from hcs_trader.data.ecb import MacroUnavailable
from hcs_trader.data.fred import DATA_URL, SERIES, fetch_fred_cpi
from hcs_trader.storage import AuditStore

_FIXTURE_KEY = "a" * 32


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_fred_keeps_as_of_dates_and_skips_missing_values(tmp_path: Path) -> None:
    def respond(request: httpx2.Request) -> httpx2.Response:
        assert request.url.copy_with(query=None) == httpx2.URL(DATA_URL)
        assert request.url.params["series_id"] == SERIES
        assert request.url.params["api_key"] == _FIXTURE_KEY
        start = request.url.params["realtime_start"]
        assert request.url.params["realtime_end"] == start
        return httpx2.Response(
            200,
            json={
                "realtime_start": start,
                "realtime_end": start,
                "observations": [
                    {
                        "date": day,
                        "value": value,
                        "realtime_start": start,
                        "realtime_end": start,
                    }
                    for day, value in [("2020-01-01", "123.450"), ("2020-02-01", ".")]
                ],
            },
        )

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as client:
        first = await fetch_fred_cpi(client, _FIXTURE_KEY, as_of=date(2020, 3, 1))
        second = await fetch_fred_cpi(client, _FIXTURE_KEY, as_of=date(2020, 4, 1))

    store = AuditStore(tmp_path / "fred.sqlite")
    store.initialize()
    assert store.record_macro(first + second) == 2
    assert store.record_macro(first + second) == 0
    with TestClient(create_app(store), base_url="http://127.0.0.1") as api:
        rows = api.get("/api/v1/macro/series?source=fred").json()
        assert [row["vintage_date"] for row in rows] == ["2020-03-01", "2020-04-01"]
        assert {row["observation_date"] for row in rows} == {"2020-01-01"}
        assert {row["value"] for row in rows} == {"123.450"}
        assert api.get("/api/v1/macro/series").json() == []


@pytest.mark.anyio
async def test_fred_http_failure_does_not_expose_key_or_follow_redirects() -> None:
    def respond(request: httpx2.Request) -> httpx2.Response:
        assert request.url.host == "api.stlouisfed.org"
        return httpx2.Response(302, headers={"location": "https://example.invalid"})

    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(respond), follow_redirects=True
    ) as client:
        with pytest.raises(MacroUnavailable, match="FRED request failed") as caught:
            await fetch_fred_cpi(client, _FIXTURE_KEY)
    assert _FIXTURE_KEY not in str(caught.value)
    assert caught.value.__cause__ is None
