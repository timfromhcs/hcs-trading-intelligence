"""EVM spot swap validation, calldata inspection, and allowance boundary enforcement."""

import re
from dataclasses import dataclass
from decimal import Decimal

# Known ERC-20 & Universal Router method selectors
SELECTOR_APPROVE = bytes.fromhex("095ea7b3")  # approve(address,uint256)
SELECTOR_UR_EXECUTE = bytes.fromhex("3593564c")  # execute(bytes,bytes[],uint256)

UNLIMITED_ALLOWANCE_THRESHOLD = 2**255

_ETH_ADDRESS = re.compile(r"^0x[a-fA-F0-9]{40}$")

# Verified Router allowlist
ALLOWED_EVM_ROUTERS = frozenset(
    {
        # Uniswap Universal Router (Mainnet & Sepolia)
        "0x3fC91A3afd70395Cd496C647d5a6CC9D4B2b7FAD".lower(),
    }
)

# Verified token contract allowlist
ALLOWED_EVM_TOKENS = frozenset(
    {
        "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2".lower(),  # WETH (Mainnet)
        "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48".lower(),  # USDC (Mainnet)
        "0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984".lower(),  # UNI (Mainnet)
        "0x7b79995e5f793A07Bc00c21412e50Ecae098E7f9".lower(),  # WETH (Sepolia)
        "0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238".lower(),  # USDC (Sepolia)
    }
)


class EVMSwapError(Exception):
    """Base error for EVM swap validation and calldata inspection."""


class EVMAllowanceError(EVMSwapError):
    """The token allowance request violates exact-amount safety policy."""


class EVMCalldataTamperingError(EVMSwapError):
    """Calldata does not match declared transaction boundaries."""


@dataclass(frozen=True, slots=True)
class EVMSwapAdapter:
    """Bounded EVM swap adapter and calldata safety inspector."""

    chain_id: int
    allowed_routers: frozenset[str] = ALLOWED_EVM_ROUTERS
    allowed_tokens: frozenset[str] = ALLOWED_EVM_TOKENS
    max_gas_limit: int = 350000
    max_fee_gwei: Decimal = Decimal("50.0")

    def validate_chain_id(self, reported_chain_id: int) -> None:
        """Reject mismatched or forked chain IDs."""
        if reported_chain_id != self.chain_id:
            raise EVMSwapError(
                f"Wrong chain ID: expected {self.chain_id}, got {reported_chain_id}"
            )

    def validate_router(self, router_address: str) -> None:
        """Reject unapproved router destinations."""
        if not _ETH_ADDRESS.match(router_address):
            raise EVMSwapError(f"Invalid Ethereum address format: '{router_address}'")
        if router_address.lower() not in self.allowed_routers:
            raise EVMSwapError(
                f"Router '{router_address}' is not on the audited allowlist"
            )

    def validate_token(self, token_address: str) -> None:
        """Reject unapproved token contracts."""
        if not _ETH_ADDRESS.match(token_address):
            raise EVMSwapError(f"Invalid Ethereum address format: '{token_address}'")
        if token_address.lower() not in self.allowed_tokens:
            raise EVMSwapError(
                f"Token '{token_address}' is not on the audited allowlist"
            )

    def build_exact_approval_calldata(
        self, spender: str, amount_base_units: int
    ) -> bytes:
        """Generate ERC-20 approve calldata for the exact swap amount only."""
        self.validate_router(spender)
        if amount_base_units <= 0:
            raise EVMAllowanceError("Approval amount must be strictly positive")
        if amount_base_units >= UNLIMITED_ALLOWANCE_THRESHOLD:
            raise EVMAllowanceError(
                "Unlimited token approvals are strictly forbidden by policy"
            )

        spender_bytes = bytes.fromhex(spender[2:].lower()).rjust(32, b"\x00")
        amount_bytes = amount_base_units.to_bytes(32, byteorder="big")
        return SELECTOR_APPROVE + spender_bytes + amount_bytes

    def build_revoke_approval_calldata(self, spender: str) -> bytes:
        """Generate ERC-20 approve calldata setting allowance to zero."""
        self.validate_router(spender)
        spender_bytes = bytes.fromhex(spender[2:].lower()).rjust(32, b"\x00")
        amount_bytes = (0).to_bytes(32, byteorder="big")
        return SELECTOR_APPROVE + spender_bytes + amount_bytes

    def inspect_approval_calldata(
        self, calldata: bytes, expected_spender: str, expected_amount: int
    ) -> None:
        """Verify that calldata approves the exact expected spender and amount."""
        if len(calldata) != 68:
            raise EVMCalldataTamperingError(
                f"ERC-20 approve calldata must be exactly 68 bytes, got {len(calldata)}"
            )
        selector = calldata[:4]
        if selector != SELECTOR_APPROVE:
            raise EVMCalldataTamperingError(
                f"Unexpected method selector '{selector.hex()}', "
                "expected approve (095ea7b3)"
            )

        spender_hex = "0x" + calldata[16:36].hex().lower()
        amount = int.from_bytes(calldata[36:68], byteorder="big")

        if spender_hex != expected_spender.lower():
            raise EVMCalldataTamperingError(
                f"Approval spender '{spender_hex}' does not match "
                f"expected '{expected_spender.lower()}'"
            )

        if amount >= UNLIMITED_ALLOWANCE_THRESHOLD:
            raise EVMAllowanceError(
                "Calldata attempts unlimited token approval (forbidden by policy)"
            )

        if amount != expected_amount:
            raise EVMCalldataTamperingError(
                f"Approved amount {amount} does not match expected {expected_amount}"
            )

    def inspect_universal_router_calldata(
        self, calldata: bytes, current_timestamp: int
    ) -> None:
        """Verify Universal Router execute selector and basic length constraints."""
        if len(calldata) < 100:
            raise EVMCalldataTamperingError(
                f"Universal router calldata too short: {len(calldata)} bytes"
            )
        selector = calldata[:4]
        if selector != SELECTOR_UR_EXECUTE:
            raise EVMCalldataTamperingError(
                f"Unexpected selector '{selector.hex()}', expected execute (3593564c)"
            )

        # In execute(bytes commands, bytes[] inputs, uint256 deadline):
        # deadline is 3rd argument (offset 68..100)
        deadline_bytes = calldata[68:100]
        deadline = int.from_bytes(deadline_bytes, byteorder="big")
        if deadline <= current_timestamp:
            raise EVMSwapError(
                f"Universal router swap deadline {deadline} has expired "
                f"(now: {current_timestamp})"
            )

    def validate_gas_and_fees(self, gas_limit: int, max_fee_gwei: Decimal) -> None:
        """Ensure gas limit and gas fee do not exceed configured safety bounds."""
        if gas_limit <= 0 or gas_limit > self.max_gas_limit:
            raise EVMSwapError(
                f"Gas limit {gas_limit} exceeds safety maximum {self.max_gas_limit}"
            )
        if max_fee_gwei <= 0 or max_fee_gwei > self.max_fee_gwei:
            raise EVMSwapError(
                f"Max fee per gas {max_fee_gwei} Gwei exceeds safety maximum "
                f"{self.max_fee_gwei} Gwei"
            )
