"""Live Binance Spot TESTNET rehearsal. Skipped unless testnet keys are in the env.

Run manually:
    HCS_BINANCE_TESTNET_KEY=... HCS_BINANCE_TESTNET_SECRET=... \
        uv run pytest tests/rehearsal -m rehearsal --no-cov

Testnet funds only. The adapter cannot target mainnet hosts. Never run in CI.
"""

import os

import httpx2
import pytest

from hcs_trader.binance_rehearsal import run_rehearsal
from hcs_trader.binance_testnet import BinanceSpotTestnetAdapter, HmacSha256Signer

KEY = os.environ.get("HCS_BINANCE_TESTNET_KEY", "")
SECRET = os.environ.get("HCS_BINANCE_TESTNET_SECRET", "")

pytestmark = [
    pytest.mark.rehearsal,
    pytest.mark.skipif(
        not (KEY and SECRET),
        reason="HCS_BINANCE_TESTNET_KEY / HCS_BINANCE_TESTNET_SECRET not set",
    ),
]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_testnet_rehearsal_passes() -> None:
    async with httpx2.AsyncClient(trust_env=False, follow_redirects=False) as client:
        adapter = BinanceSpotTestnetAdapter(KEY, HmacSha256Signer(SECRET), client)
        evidence = await run_rehearsal(adapter, secrets_to_hide=[KEY, SECRET])
    assert evidence["passed"], evidence
