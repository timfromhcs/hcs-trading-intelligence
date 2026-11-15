from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import httpx2
import pytest
from fastapi.testclient import TestClient

from hcs_trader.api import create_app
from hcs_trader.data.ecb import (
    DATA_URL,
    SERIES,
    SOURCE,
    MacroUnavailable,
    fetch_ecb_rates,
)
from hcs_trader.storage import AuditStore


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_offline_api_preserves_precise_values_and_observed_revisions(
    tmp_path: Path,
) -> None:
    def respond(request: httpx2.Request) -> httpx2.Response:
        assert request.method == "GET"
        assert request.url.copy_with(query=None) == httpx2.URL(DATA_URL)
        assert dict(request.url.params) == {
            "format": "csvdata",
            "detail": "dataonly",
            "lastNObservations": "250",
        }
        return httpx2.Response(
            200, text=f"KEY,TIME_PERIOD,OBS_VALUE\n{SERIES},2020-01-02,1.12340\n"
        )

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as client:
        originals = await fetch_ecb_rates(client)
    original = originals[0]
    store = AuditStore(tmp_path / "macro.sqlite")
    store.initialize()
    assert store.record_macro(originals) == 1
    assert store.record_macro(originals) == 0
    revised = replace(
        original,
        value=Decimal("1.12345"),
        received_at=original.received_at + timedelta(seconds=1),
    )
    assert store.record_macro([revised]) == 1
    assert store.record_macro(originals) == 0
    assert store.macro_history(SOURCE, SERIES, limit=10) == [original, revised]

    with TestClient(create_app(store), base_url="http://127.0.0.1") as api:
        observations = api.get("/api/v1/macro/series").json()
        assert [row["value"] for row in observations] == ["1.12340", "1.12345"]
        assert all(row["observation_date"] == "2020-01-02" for row in observations)
        assert observations[0]["received_at"] < observations[1]["received_at"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "row",
    ["unknown,2020-01-02,1", f"{SERIES},9999-01-02,1", f"{SERIES},2020-01-02,NaN"],
)
async def test_invalid_ecb_payload_is_rejected(row: str) -> None:
    def respond(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, text=f"KEY,TIME_PERIOD,OBS_VALUE\n{row}\n")

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as client:
        with pytest.raises(MacroUnavailable):
            await fetch_ecb_rates(client)
