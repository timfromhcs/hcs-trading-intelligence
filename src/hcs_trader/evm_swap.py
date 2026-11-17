"""EVM spot swap validation, calldata inspection, and allowance boundary enforcement.

Allowlists are keyed by EVM chain ID and carry a version string, so a Sepolia
address can never authorise anything on Mainnet. Transaction inspection works
on real EIP-1559 transaction bytes (RLP) and ABI-decoded calldata rather than on
a summary supplied by a caller.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType

# Known ERC-20 & Universal Router method selectors
SELECTOR_APPROVE = bytes.fromhex("095ea7b3")  # approve(address,uint256)
SELECTOR_UR_EXECUTE = bytes.fromhex("3593564c")  # execute(bytes,bytes[],uint256)
SELECTOR_BALANCE_OF = bytes.fromhex("70a08231")  # balanceOf(address)
SELECTOR_DECIMALS = bytes.fromhex("313ce567")  # decimals()

UNLIMITED_ALLOWANCE_THRESHOLD = 2**255

_ETH_ADDRESS = re.compile(r"^0x[a-fA-F0-9]{40}$")

# Bump whenever any chain allowlist below changes.
EVM_ALLOWLIST_VERSION = "2026-11-16.v1"

CHAIN_ID_MAINNET = 1
CHAIN_ID_SEPOLIA = 11155111


@dataclass(frozen=True, slots=True)
class EvmToken:
    address: str  # lowercase 0x-prefixed
    symbol: str
    decimals: int


@dataclass(frozen=True, slots=True)
class EvmChainAllowlist:
    """Audited routers and tokens for exactly one EVM chain."""

    chain_id: int
    name: str
    version: str
    routers: frozenset[str]
    tokens: tuple[EvmToken, ...]
    # router address (lowercase) -> expected keccak256 of deployed runtime code.
    # Empty until a hash has been verified against the live deployment.
    router_code_hashes: tuple[tuple[str, str], ...] = ()

    @property
    def token_addresses(self) -> frozenset[str]:
        return frozenset(token.address for token in self.tokens)

    def token(self, address: str) -> EvmToken | None:
        lowered = address.lower()
        return next((t for t in self.tokens if t.address == lowered), None)


EVM_CHAIN_ALLOWLISTS: Mapping[int, EvmChainAllowlist] = MappingProxyType(
    {
        CHAIN_ID_MAINNET: EvmChainAllowlist(
            chain_id=CHAIN_ID_MAINNET,
            name="mainnet",
            version=EVM_ALLOWLIST_VERSION,
            routers=frozenset(
                {
                    # Uniswap Universal Router (Mainnet)
                    "0x3fC91A3afd70395Cd496C647d5a6CC9D4B2b7FAD".lower(),
                }
            ),
            tokens=(
                EvmToken(
                    "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2".lower(), "WETH", 18
                ),
                EvmToken(
                    "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48".lower(), "USDC", 6
                ),
                EvmToken(
                    "0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984".lower(), "UNI", 18
                ),
            ),
        ),
        CHAIN_ID_SEPOLIA: EvmChainAllowlist(
            chain_id=CHAIN_ID_SEPOLIA,
            name="sepolia",
            version=EVM_ALLOWLIST_VERSION,
            # No Sepolia router is allowlisted until its address and code hash
            # have been verified; swaps on Sepolia therefore fail closed.
            routers=frozenset(),
            tokens=(
                EvmToken(
                    "0x7b79995e5f793A07Bc00c21412e50Ecae098E7f9".lower(), "WETH", 18
                ),
                EvmToken(
                    "0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238".lower(), "USDC", 6
                ),
            ),
        ),
    }
)


def allowlist_for_chain(chain_id: int) -> EvmChainAllowlist:
    """Return the chain's allowlist, or an empty (deny-all) one if unknown."""
    known = EVM_CHAIN_ALLOWLISTS.get(chain_id)
    if known is not None:
        return known
    return EvmChainAllowlist(
        chain_id=chain_id,
        name="unknown",
        version=EVM_ALLOWLIST_VERSION,
        routers=frozenset(),
        tokens=(),
    )


# Universal Router command allowlist (low five bits of each command byte; the
# high bit only means "allow revert"). Transfers, sweeps and permit commands are
# excluded because they can move funds to caller-chosen recipients.
_UR_ALLOWED_COMMANDS = frozenset(
    {
        0x00,  # V3_SWAP_EXACT_IN
        0x01,  # V3_SWAP_EXACT_OUT
        0x08,  # V2_SWAP_EXACT_IN
        0x09,  # V2_SWAP_EXACT_OUT
        0x0B,  # WRAP_ETH
        0x0C,  # UNWRAP_WETH
    }
)
_UR_EXACT_IN_COMMANDS = frozenset({0x00, 0x08})
# Universal Router recipient sentinels: MSG_SENDER and ADDRESS_THIS.
_UR_SENTINEL_RECIPIENTS = frozenset({1, 2})


class EVMSwapError(Exception):
    """Base error for EVM swap validation and calldata inspection."""


class EVMAllowanceError(EVMSwapError):
    """The token allowance request violates exact-amount safety policy."""


class EVMCalldataTamperingError(EVMSwapError):
    """Calldata does not match declared transaction boundaries."""


# --- Keccak-256 (Ethereum's pre-NIST padding; hashlib only has SHA3) -------------

_KECCAK_ROUND_CONSTANTS = (
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
)  # fmt: skip
_KECCAK_ROTATIONS = (
    (0, 36, 3, 41, 18),
    (1, 44, 10, 45, 2),
    (62, 6, 43, 15, 61),
    (28, 55, 25, 21, 56),
    (27, 20, 39, 8, 14),
)
_MASK64 = (1 << 64) - 1


def _rol64(value: int, shift: int) -> int:
    shift %= 64
    return ((value << shift) | (value >> (64 - shift))) & _MASK64 if shift else value


def _keccak_f(state: list[int]) -> None:
    for round_constant in _KECCAK_ROUND_CONSTANTS:
        c = [
            state[x] ^ state[x + 5] ^ state[x + 10] ^ state[x + 15] ^ state[x + 20]
            for x in range(5)
        ]
        d = [c[(x - 1) % 5] ^ _rol64(c[(x + 1) % 5], 1) for x in range(5)]
        for i in range(25):
            state[i] ^= d[i % 5]
        b = [0] * 25
        for x in range(5):
            for y in range(5):
                b[y + 5 * ((2 * x + 3 * y) % 5)] = _rol64(
                    state[x + 5 * y], _KECCAK_ROTATIONS[x][y]
                )
        for x in range(5):
            for y in range(5):
                state[x + 5 * y] = b[x + 5 * y] ^ (
                    ~b[(x + 1) % 5 + 5 * y] & b[(x + 2) % 5 + 5 * y]
                )
        state[0] ^= round_constant


def keccak256(data: bytes) -> bytes:
    """Keccak-256 as used by Ethereum (EIP-55, selectors, code hashes)."""
    rate = 136
    padded = bytearray(data)
    padded.append(0x01)
    while len(padded) % rate:
        padded.append(0x00)
    padded[-1] |= 0x80
    state = [0] * 25
    for offset in range(0, len(padded), rate):
        block = padded[offset : offset + rate]
        for i in range(rate // 8):
            state[i] ^= int.from_bytes(block[8 * i : 8 * i + 8], "little")
        _keccak_f(state)
    return b"".join(lane.to_bytes(8, "little") for lane in state)[:32]


def to_checksum_address(address: str) -> str:
    """EIP-55 mixed-case checksum encoding of a 20-byte hex address."""
    if not _ETH_ADDRESS.match(address):
        raise EVMSwapError(f"Invalid Ethereum address format: '{address}'")
    lowered = address[2:].lower()
    digest = keccak256(lowered.encode("ascii")).hex()
    return "0x" + "".join(
        char.upper() if int(digest[i], 16) >= 8 else char
        for i, char in enumerate(lowered)
    )


def is_valid_eth_address(address: str) -> bool:
    """True for all-lowercase/all-uppercase hex or a correct EIP-55 checksum."""
    if not _ETH_ADDRESS.match(address):
        return False
    body = address[2:]
    if body == body.lower() or body == body.upper():
        return True
    return to_checksum_address(address) == address


# --- RLP / EIP-1559 transaction decoding ---------------------------------------

_MAX_TX_BYTES = 131_072


def _rlp_decode_item(data: bytes, pos: int) -> tuple[object, int]:
    if pos >= len(data):
        raise EVMCalldataTamperingError("RLP input truncated")
    prefix = data[pos]
    if prefix < 0x80:
        return data[pos : pos + 1], pos + 1
    if prefix <= 0xB7:
        length = prefix - 0x80
        start = pos + 1
        end = start + length
        if end > len(data):
            raise EVMCalldataTamperingError("RLP string exceeds input")
        if length == 1 and data[start] < 0x80:
            raise EVMCalldataTamperingError("Non-canonical RLP single byte")
        return data[start:end], end
    if prefix <= 0xBF:
        length_of_length = prefix - 0xB7
        start = pos + 1 + length_of_length
        if start > len(data) or data[pos + 1] == 0:
            raise EVMCalldataTamperingError("Non-canonical RLP length")
        length = int.from_bytes(data[pos + 1 : start], "big")
        if length < 56 or start + length > len(data):
            raise EVMCalldataTamperingError("Invalid RLP string length")
        return data[start : start + length], start + length
    if prefix <= 0xF7:
        length = prefix - 0xC0
        start = pos + 1
    else:
        length_of_length = prefix - 0xF7
        start = pos + 1 + length_of_length
        if start > len(data) or data[pos + 1] == 0:
            raise EVMCalldataTamperingError("Non-canonical RLP length")
        length = int.from_bytes(data[pos + 1 : start], "big")
        if length < 56:
            raise EVMCalldataTamperingError("Non-canonical RLP list length")
    end = start + length
    if end > len(data):
        raise EVMCalldataTamperingError("RLP list exceeds input")
    items: list[object] = []
    cursor = start
    while cursor < end:
        item, cursor = _rlp_decode_item(data[:end], cursor)
        items.append(item)
    return items, end


def rlp_decode(data: bytes) -> object:
    """Strict RLP decode of exactly one top-level item."""
    item, end = _rlp_decode_item(data, 0)
    if end != len(data):
        raise EVMCalldataTamperingError("Trailing bytes after RLP item")
    return item


def _rlp_uint(item: object, name: str) -> int:
    if not isinstance(item, bytes) or (item and item[0] == 0):
        raise EVMCalldataTamperingError(f"Invalid RLP integer for {name}")
    return int.from_bytes(item, "big")


@dataclass(frozen=True, slots=True)
class DecodedEvmTransaction:
    chain_id: int
    nonce: int
    max_priority_fee_per_gas: int
    max_fee_per_gas: int
    gas_limit: int
    to: str  # lowercase 0x address
    value: int
    data: bytes
    signed: bool


def decode_eip1559_transaction(raw: bytes) -> DecodedEvmTransaction:
    """Decode a type-2 transaction (signed or unsigned signing payload)."""
    if not raw or len(raw) > _MAX_TX_BYTES:
        raise EVMCalldataTamperingError("Transaction bytes empty or too large")
    if raw[0] != 0x02:
        raise EVMCalldataTamperingError(
            f"Unsupported transaction type 0x{raw[0]:02x}; only EIP-1559 is accepted"
        )
    fields = rlp_decode(raw[1:])
    if not isinstance(fields, list) or len(fields) not in (9, 12):
        raise EVMCalldataTamperingError("Malformed EIP-1559 transaction field count")
    to_field = fields[5]
    if not isinstance(to_field, bytes) or len(to_field) != 20:
        raise EVMCalldataTamperingError(
            "Transaction must have a 20-byte recipient (contract creation refused)"
        )
    data = fields[7]
    access_list = fields[8]
    if not isinstance(data, bytes):
        raise EVMCalldataTamperingError("Invalid transaction data field")
    if access_list != []:
        raise EVMCalldataTamperingError("Access lists are not permitted")
    if len(fields) == 12:
        v = _rlp_uint(fields[9], "v")
        _rlp_uint(fields[10], "r")
        _rlp_uint(fields[11], "s")
        if v not in (0, 1):
            raise EVMCalldataTamperingError("Invalid signature parity")
    return DecodedEvmTransaction(
        chain_id=_rlp_uint(fields[0], "chain_id"),
        nonce=_rlp_uint(fields[1], "nonce"),
        max_priority_fee_per_gas=_rlp_uint(fields[2], "max_priority_fee_per_gas"),
        max_fee_per_gas=_rlp_uint(fields[3], "max_fee_per_gas"),
        gas_limit=_rlp_uint(fields[4], "gas_limit"),
        to="0x" + to_field.hex(),
        value=_rlp_uint(fields[6], "value"),
        data=data,
        signed=len(fields) == 12,
    )


@dataclass(frozen=True, slots=True)
class DecodedRouterExecute:
    commands: bytes
    inputs: tuple[bytes, ...]
    deadline: int


def _abi_word(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 32 > len(data):
        raise EVMCalldataTamperingError("ABI word outside calldata")
    return int.from_bytes(data[offset : offset + 32], "big")


def _abi_bytes(data: bytes, offset: int) -> bytes:
    length = _abi_word(data, offset)
    start = offset + 32
    if length > len(data) or start + length > len(data):
        raise EVMCalldataTamperingError("ABI bytes length exceeds calldata")
    return data[start : start + length]


def decode_universal_router_execute(calldata: bytes) -> DecodedRouterExecute:
    """ABI-decode ``execute(bytes commands, bytes[] inputs, uint256 deadline)``."""
    if calldata[:4] != SELECTOR_UR_EXECUTE:
        raise EVMCalldataTamperingError(
            f"Unexpected selector '{calldata[:4].hex()}', expected execute (3593564c)"
        )
    body = calldata[4:]
    if len(body) < 96:
        raise EVMCalldataTamperingError("Universal router calldata too short")
    commands = _abi_bytes(body, _abi_word(body, 0))
    inputs_offset = _abi_word(body, 32)
    deadline = _abi_word(body, 64)
    count = _abi_word(body, inputs_offset)
    if count > len(body) // 32:
        raise EVMCalldataTamperingError("Universal router input count is implausible")
    array_base = inputs_offset + 32
    inputs = []
    for index in range(count):
        element_offset = _abi_word(body, array_base + 32 * index)
        inputs.append(_abi_bytes(body, array_base + element_offset))
    return DecodedRouterExecute(commands, tuple(inputs), deadline)


@dataclass(frozen=True, slots=True)
class EVMSwapAdapter:
    """Bounded EVM swap adapter and calldata safety inspector.

    ``allowed_routers`` / ``allowed_tokens`` default to the versioned allowlist
    for ``chain_id`` (deny-all for an unknown chain); passing a value overrides
    it explicitly.
    """

    chain_id: int
    allowed_routers: frozenset[str] | None = None
    allowed_tokens: frozenset[str] | None = None
    max_gas_limit: int = 350000
    max_fee_gwei: Decimal = Decimal("50.0")
    router_code_hashes: tuple[tuple[str, str], ...] | None = None

    @property
    def allowlist(self) -> EvmChainAllowlist:
        return allowlist_for_chain(self.chain_id)

    @property
    def allowlist_version(self) -> str:
        return self.allowlist.version

    @property
    def routers(self) -> frozenset[str]:
        if self.allowed_routers is not None:
            return self.allowed_routers
        return self.allowlist.routers

    @property
    def tokens(self) -> frozenset[str]:
        if self.allowed_tokens is not None:
            return self.allowed_tokens
        return self.allowlist.token_addresses

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
        if router_address.lower() not in self.routers:
            raise EVMSwapError(
                f"Router '{router_address}' is not on the audited allowlist"
            )

    def validate_token(self, token_address: str) -> None:
        """Reject unapproved token contracts."""
        if not _ETH_ADDRESS.match(token_address):
            raise EVMSwapError(f"Invalid Ethereum address format: '{token_address}'")
        if token_address.lower() not in self.tokens:
            raise EVMSwapError(
                f"Token '{token_address}' is not on the audited allowlist"
            )

    def validate_router_code_hash(
        self, router_address: str, observed_hash: str
    ) -> None:
        """Compare the deployed runtime-code hash with the pinned value.

        Fails closed when no hash has been pinned for the router.
        """
        self.validate_router(router_address)
        pinned = dict(
            self.router_code_hashes
            if self.router_code_hashes is not None
            else self.allowlist.router_code_hashes
        )
        expected = pinned.get(router_address.lower())
        if expected is None:
            raise EVMSwapError(
                f"No code hash is pinned for router '{router_address}'; refusing"
            )
        if observed_hash.lower() != expected.lower():
            raise EVMSwapError(
                f"Router code hash {observed_hash} does not match the pinned hash"
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

    def inspect_universal_router_execute(
        self,
        calldata: bytes,
        *,
        current_timestamp: int,
        expected_recipient: str,
        min_amount_out: int | None = None,
    ) -> DecodedRouterExecute:
        """Fully decode a Universal Router ``execute`` call and assert its fields.

        Every command must be a swap or WETH wrap/unwrap, every recipient must
        be the signer (or a router-internal sentinel), the deadline must be in the
        future, and exact-in swaps must not accept less than ``min_amount_out``.
        """
        if not _ETH_ADDRESS.match(expected_recipient):
            raise EVMSwapError(
                f"Invalid Ethereum address format: '{expected_recipient}'"
            )
        decoded = decode_universal_router_execute(calldata)
        if decoded.deadline <= current_timestamp:
            raise EVMSwapError(
                f"Universal router swap deadline {decoded.deadline} has expired "
                f"(now: {current_timestamp})"
            )
        if not decoded.commands or len(decoded.commands) != len(decoded.inputs):
            raise EVMCalldataTamperingError(
                "Universal router commands and inputs do not line up"
            )
        signer = int(expected_recipient, 16)
        for index, (raw_command, payload) in enumerate(
            zip(decoded.commands, decoded.inputs, strict=True)
        ):
            command = raw_command & 0x1F
            if command not in _UR_ALLOWED_COMMANDS:
                raise EVMCalldataTamperingError(
                    f"Universal router command 0x{command:02x} at index {index} "
                    "is not allowlisted"
                )
            recipient = _abi_word(payload, 0)
            if recipient != signer and recipient not in _UR_SENTINEL_RECIPIENTS:
                raise EVMCalldataTamperingError(
                    f"Universal router command at index {index} pays an "
                    "unexpected recipient"
                )
            if (
                min_amount_out is not None
                and command in _UR_EXACT_IN_COMMANDS
                and _abi_word(payload, 64) < min_amount_out
            ):
                raise EVMCalldataTamperingError(
                    f"Universal router command at index {index} accepts less than "
                    f"the required minimum output {min_amount_out}"
                )
        return decoded

    def inspect_transaction(
        self,
        raw_transaction: bytes,
        *,
        current_timestamp: int,
        expected_signer: str,
        expected_approval: tuple[str, int] | None = None,
        expected_value: int = 0,
        min_amount_out: int | None = None,
    ) -> DecodedEvmTransaction:
        """Decode real EIP-1559 bytes and assert chain, target, fees and calldata.

        ``expected_approval`` is ``(spender, exact_amount)`` for an ERC-20
        approve; otherwise the transaction must be a Universal Router execute.
        """
        tx = decode_eip1559_transaction(raw_transaction)
        self.validate_chain_id(tx.chain_id)
        if tx.max_priority_fee_per_gas > tx.max_fee_per_gas:
            raise EVMSwapError("Priority fee exceeds the max fee per gas")
        self.validate_gas_and_fees(
            tx.gas_limit, Decimal(tx.max_fee_per_gas) / Decimal(10**9)
        )
        if tx.value != expected_value:
            raise EVMCalldataTamperingError(
                f"Transaction value {tx.value} does not match expected {expected_value}"
            )
        if expected_approval is not None:
            if tx.value != 0:
                raise EVMCalldataTamperingError("Approvals must not carry value")
            self.validate_token(tx.to)
            spender, amount = expected_approval
            self.validate_router(spender)
            self.inspect_approval_calldata(tx.data, spender, amount)
        else:
            self.validate_router(tx.to)
            self.inspect_universal_router_execute(
                tx.data,
                current_timestamp=current_timestamp,
                expected_recipient=expected_signer,
                min_amount_out=min_amount_out,
            )
        return tx

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
