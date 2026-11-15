"""Tests for EVM spot swap calldata validation and allowance boundary enforcement."""

import time
from decimal import Decimal

import pytest

from hcs_trader.evm_swap import (
    SELECTOR_APPROVE,
    SELECTOR_UR_EXECUTE,
    EVMAllowanceError,
    EVMCalldataTamperingError,
    EVMSwapAdapter,
    EVMSwapError,
)

_ROUTER = "0x3fC91A3afd70395Cd496C647d5a6CC9D4B2b7FAD"
_WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
_SUSPICIOUS_ROUTER = "0x000000000000000000000000000000000000dEaD"
_SUSPICIOUS_TOKEN = "0x1111111111111111111111111111111111111111"


def test_validate_chain_id() -> None:
    adapter = EVMSwapAdapter(chain_id=1)
    adapter.validate_chain_id(1)

    with pytest.raises(EVMSwapError, match="Wrong chain ID"):
        adapter.validate_chain_id(137)  # Polygon rejected


def test_validate_router_and_token() -> None:
    adapter = EVMSwapAdapter(chain_id=1)
    adapter.validate_router(_ROUTER)
    adapter.validate_token(_WETH)

    with pytest.raises(EVMSwapError, match="not on the audited allowlist"):
        adapter.validate_router(_SUSPICIOUS_ROUTER)

    with pytest.raises(EVMSwapError, match="not on the audited allowlist"):
        adapter.validate_token(_SUSPICIOUS_TOKEN)


def test_exact_approval_calldata() -> None:
    adapter = EVMSwapAdapter(chain_id=1)
    amount = 5000000000  # 5000 USDC
    calldata = adapter.build_exact_approval_calldata(_ROUTER, amount)

    assert len(calldata) == 68
    assert calldata[:4] == SELECTOR_APPROVE
    # Inspect using adapter
    adapter.inspect_approval_calldata(calldata, _ROUTER, amount)


def test_unlimited_approval_strictly_rejected() -> None:
    adapter = EVMSwapAdapter(chain_id=1)
    unlimited = 2**256 - 1

    with pytest.raises(
        EVMAllowanceError, match="Unlimited token approvals are strictly forbidden"
    ):
        adapter.build_exact_approval_calldata(_ROUTER, unlimited)


def test_revoke_approval_calldata() -> None:
    adapter = EVMSwapAdapter(chain_id=1)
    calldata = adapter.build_revoke_approval_calldata(_ROUTER)

    assert len(calldata) == 68
    assert calldata[:4] == SELECTOR_APPROVE
    # Amount bytes must be all zero
    assert calldata[36:68] == b"\x00" * 32
    adapter.inspect_approval_calldata(calldata, _ROUTER, 0)


def test_inspect_approval_calldata_tampering() -> None:
    adapter = EVMSwapAdapter(chain_id=1)
    calldata = adapter.build_exact_approval_calldata(_ROUTER, 1000)

    # Expected amount mismatch
    with pytest.raises(EVMCalldataTamperingError, match="does not match expected"):
        adapter.inspect_approval_calldata(calldata, _ROUTER, 999)

    # Expected spender mismatch
    with pytest.raises(EVMCalldataTamperingError, match="does not match expected"):
        adapter.inspect_approval_calldata(
            calldata, "0x1111111111111111111111111111111111111111", 1000
        )


def test_inspect_universal_router_calldata() -> None:
    adapter = EVMSwapAdapter(chain_id=1)
    now = int(time.time())
    future_deadline = now + 300
    expired_deadline = now - 60

    # Build dummy valid Universal Router execute calldata
    # Selector (4 bytes) + commands offset (32b) + inputs offset (32b) + deadline (32b)
    calldata = (
        SELECTOR_UR_EXECUTE
        + (64).to_bytes(32, "big")
        + (128).to_bytes(32, "big")
        + future_deadline.to_bytes(32, "big")
        + b"\x00" * 32
    )

    # Should pass
    adapter.inspect_universal_router_calldata(calldata, now)

    # Expired deadline
    expired_calldata = (
        SELECTOR_UR_EXECUTE
        + (64).to_bytes(32, "big")
        + (128).to_bytes(32, "big")
        + expired_deadline.to_bytes(32, "big")
        + b"\x00" * 32
    )
    with pytest.raises(EVMSwapError, match="deadline.*has expired"):
        adapter.inspect_universal_router_calldata(expired_calldata, now)


def test_validate_gas_and_fees() -> None:
    adapter = EVMSwapAdapter(
        chain_id=1,
        max_gas_limit=350000,
        max_fee_gwei=Decimal("50.0"),
    )

    # Valid gas
    adapter.validate_gas_and_fees(250000, Decimal("30.0"))

    # Excessive gas limit
    with pytest.raises(EVMSwapError, match="Gas limit.*exceeds safety maximum"):
        adapter.validate_gas_and_fees(500000, Decimal("30.0"))

    # Excessive fee per gas
    with pytest.raises(EVMSwapError, match="Max fee per gas.*exceeds safety maximum"):
        adapter.validate_gas_and_fees(250000, Decimal("80.0"))
