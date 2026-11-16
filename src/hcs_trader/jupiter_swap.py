"""Solana Jupiter swap route verification and instruction inspection.

Jupiter routing is deployed on Solana mainnet only. The instruction inspector
accepts the Jupiter Swap API V2 ``/build`` response
(https://developers.jup.ag/docs/swap/build, base ``https://api.jup.ag/swap/v2``)
and the legacy flat ``instructions`` payload.
"""

import base64
import binascii
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

# Public on-chain program addresses. The S105 suppressions below are for names
# containing "TOKEN"; these values are not secrets.
JUPITER_V6_PROGRAM_ID = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
SPL_TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"  # noqa: S105
SYSTEM_PROGRAM_ID = "11111111111111111111111111111111"
COMPUTE_BUDGET_PROGRAM_ID = "ComputeBudget111111111111111111111111111111"
ASSOCIATED_TOKEN_PROGRAM_ID = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"  # noqa: S105
# Token-2022 is deliberately NOT allowlisted: its extensions (transfer hooks,
# transfer fees, permanent delegates) need a separate review.
TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"  # noqa: S105

# Bump this version whenever the program allowlist or its per-program
# instruction rules change.
JUPITER_PROGRAM_ALLOWLIST_VERSION = "2026-11-15.v2"
ALLOWED_JUPITER_PROGRAMS = frozenset(
    {
        JUPITER_V6_PROGRAM_ID,
        SPL_TOKEN_PROGRAM_ID,
        SYSTEM_PROGRAM_ID,
        COMPUTE_BUDGET_PROGRAM_ID,
        # Allowed only for CreateIdempotent with owner == signer; see
        # JupiterSwapAdapter._check_associated_token_instruction.
        ASSOCIATED_TOKEN_PROGRAM_ID,
    }
)

# Associated Token Account program instruction discriminators: 0 = Create,
# 1 = CreateIdempotent, 2 = RecoverNested. Only CreateIdempotent is accepted.
_ATA_CREATE_IDEMPOTENT_DATA = b"\x01"
_ATA_ACCOUNT_COUNT = 6

# V2 /build instruction fields, in transaction order.
_BUILD_LIST_FIELDS = ("computeBudgetInstructions", "setupInstructions")
_BUILD_TRAILING_LIST_FIELDS = ("otherInstructions",)
_BUILD_KNOWN_INSTRUCTION_FIELDS = frozenset(
    {
        "computeBudgetInstructions",
        "setupInstructions",
        "swapInstruction",
        "cleanupInstruction",
        "otherInstructions",
        "tipInstruction",
    }
)

# Verified Solana mainnet-beta token mint allowlist. Jupiter has no devnet
# deployment, so there is no devnet routing.
ALLOWED_SOLANA_MINTS = frozenset(
    {
        "So11111111111111111111111111111111111111112",  # Wrapped SOL / Native SOL
        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC (Mainnet)
        "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT (Mainnet)
    }
)


class JupiterRouteError(Exception):
    """The Jupiter swap route violates safety policy or allowlists."""


def _validate_api_instruction(field: str, inst: Any) -> dict[str, Any]:
    """Validate a V2 ``ApiInstruction`` (programId, accounts, base64 data)."""
    if not isinstance(inst, dict):
        raise JupiterRouteError(f"{field} entry must be an instruction object")
    program_id = inst.get("programId")
    if not isinstance(program_id, str) or not program_id:
        raise JupiterRouteError(f"{field} instruction is missing programId")
    accounts = inst.get("accounts")
    if not isinstance(accounts, list):
        raise JupiterRouteError(f"{field} instruction accounts must be a list")
    for account in accounts:
        if (
            not isinstance(account, dict)
            or not isinstance(account.get("pubkey"), str)
            or not isinstance(account.get("isSigner"), bool)
            or not isinstance(account.get("isWritable"), bool)
        ):
            raise JupiterRouteError(f"{field} instruction has a malformed account")
    if not isinstance(inst.get("data"), str):
        raise JupiterRouteError(f"{field} instruction data must be a base64 string")
    return inst


def normalize_build_instructions(response: dict[str, Any]) -> list[dict[str, Any]]:
    """Map a Jupiter Swap API V2 ``/build`` response to ordered instructions.

    Order: computeBudget, setup, swap, cleanup, other, tip. Unknown top-level
    instruction fields are rejected so new upstream fields fail closed.
    """
    if not isinstance(response, dict):
        raise JupiterRouteError("Build response must be an object")
    for key in response:
        if "instruction" in key.lower() and key not in _BUILD_KNOWN_INSTRUCTION_FIELDS:
            raise JupiterRouteError(f"Unknown instruction field '{key}' in build")

    ordered: list[dict[str, Any]] = []

    def _extend_list(field: str) -> None:
        value = response.get(field)
        if not isinstance(value, list):
            raise JupiterRouteError(f"{field} must be a list")
        ordered.extend(_validate_api_instruction(field, inst) for inst in value)

    def _append_optional(field: str) -> None:
        value = response.get(field)
        if value is not None:
            ordered.append(_validate_api_instruction(field, value))

    for field in _BUILD_LIST_FIELDS:
        _extend_list(field)
    if response.get("swapInstruction") is None:
        raise JupiterRouteError("Build response is missing swapInstruction")
    ordered.append(
        _validate_api_instruction("swapInstruction", response["swapInstruction"])
    )
    _append_optional("cleanupInstruction")
    for field in _BUILD_TRAILING_LIST_FIELDS:
        _extend_list(field)
    _append_optional("tipInstruction")
    return ordered


@dataclass(frozen=True, slots=True)
class JupiterRouteQuote:
    input_mint: str
    output_mint: str
    in_amount: int
    out_amount: int
    other_amount_threshold: int
    slippage_bps: int
    price_impact_pct: Decimal
    route_plan: list[dict[str, Any]]
    quoted_at: datetime


class JupiterSwapAdapter:
    """Bounded Solana Jupiter swap route validator and instruction inspector."""

    def __init__(
        self,
        allowed_mints: frozenset[str] = ALLOWED_SOLANA_MINTS,
        allowed_programs: frozenset[str] = ALLOWED_JUPITER_PROGRAMS,
        max_quote_age_seconds: int = 30,
    ) -> None:
        self.allowed_mints = allowed_mints
        self.allowed_programs = allowed_programs
        self.max_quote_age_seconds = max_quote_age_seconds

    def validate_route(
        self,
        quote: JupiterRouteQuote,
        max_slippage_bps: int,
        max_price_impact_pct: Decimal,
        at: datetime,
    ) -> None:
        """Validate route mints, slippage, price impact, and freshness."""
        if quote.input_mint not in self.allowed_mints:
            raise JupiterRouteError(
                f"Input mint '{quote.input_mint}' is not allowlisted"
            )
        if quote.output_mint not in self.allowed_mints:
            raise JupiterRouteError(
                f"Output mint '{quote.output_mint}' is not allowlisted"
            )
        if quote.input_mint == quote.output_mint:
            raise JupiterRouteError("Input and output mints cannot be identical")
        if quote.in_amount <= 0 or quote.out_amount <= 0:
            raise JupiterRouteError("Swap amounts must be strictly positive")
        if quote.other_amount_threshold <= 0:
            raise JupiterRouteError(
                "Minimum received amount threshold must be positive"
            )

        # Freshness check
        age = at - quote.quoted_at
        if age > timedelta(seconds=self.max_quote_age_seconds):
            raise JupiterRouteError(
                f"Quote age {age.total_seconds():.1f}s exceeds freshness window "
                f"{self.max_quote_age_seconds}s"
            )

        # Slippage check
        if quote.slippage_bps > max_slippage_bps:
            raise JupiterRouteError(
                f"Route slippage {quote.slippage_bps} bps exceeds policy cap "
                f"{max_slippage_bps} bps"
            )

        # Price impact check
        if quote.price_impact_pct > max_price_impact_pct:
            raise JupiterRouteError(
                f"Route price impact {quote.price_impact_pct:.2f}% exceeds policy cap "
                f"{max_price_impact_pct:.2f}%"
            )

        # Route plan program check
        for step in quote.route_plan:
            swap_info = step.get("swapInfo", {})
            amm_label = swap_info.get("label", "unknown")
            input_token = swap_info.get("inputMint")
            output_token = swap_info.get("outputMint")
            if input_token and input_token not in self.allowed_mints:
                raise JupiterRouteError(
                    f"Route step AMM '{amm_label}' uses unapproved "
                    f"input token '{input_token}'"
                )
            if output_token and output_token not in self.allowed_mints:
                raise JupiterRouteError(
                    f"Route step AMM '{amm_label}' uses unapproved "
                    f"output token '{output_token}'"
                )

    def inspect_swap_instructions(
        self,
        transaction_payload: dict[str, Any],
        *,
        signer: str | None = None,
    ) -> None:
        """Inspect all instructions and program invocations in the swap payload.

        Accepts a Jupiter Swap API V2 ``/build`` response or a legacy payload
        with a flat ``instructions`` list. ``signer`` is the expected hot-wallet
        address; it is required when an Associated Token Account instruction
        is present.
        """
        if "swapInstruction" in transaction_payload:
            instructions = normalize_build_instructions(transaction_payload)
            swap_program = transaction_payload["swapInstruction"]["programId"]
            if swap_program != JUPITER_V6_PROGRAM_ID:
                raise JupiterRouteError(
                    f"swapInstruction invokes '{swap_program}', not the Jupiter program"
                )
        else:
            instructions = transaction_payload.get("instructions", [])
        if not instructions:
            raise JupiterRouteError("Transaction payload contains no instructions")

        for idx, inst in enumerate(instructions):
            program_id = inst.get("program_id") or inst.get("programId")
            if not program_id:
                raise JupiterRouteError(
                    f"Instruction at index {idx} is missing program identifier"
                )
            if program_id not in self.allowed_programs:
                raise JupiterRouteError(
                    f"Instruction at index {idx} invokes unapproved "
                    f"program '{program_id}'"
                )
            if program_id == ASSOCIATED_TOKEN_PROGRAM_ID:
                self._check_associated_token_instruction(idx, inst, signer)

    def inspect_message_bytes(
        self,
        raw_message: bytes,
        *,
        signer: str,
        max_swap_lamports: int = 0,
        max_priority_fee_lamports: int = 1_000_000,
    ) -> "DecodedSolanaMessage":
        """Decode serialized message bytes and assert every instruction.

        ``max_swap_lamports`` bounds any system Transfer (SOL wrapping) and is
        zero by default, so a plan that does not spend SOL permits none.
        """
        return _inspect_message(
            self,
            decode_solana_message(raw_message),
            signer=signer,
            max_swap_lamports=max_swap_lamports,
            max_priority_fee_lamports=max_priority_fee_lamports,
        )

    def inspect_transaction_bytes(
        self,
        raw_transaction: bytes,
        *,
        signer: str,
        max_swap_lamports: int = 0,
        max_priority_fee_lamports: int = 1_000_000,
    ) -> "DecodedSolanaMessage":
        """Like :meth:`inspect_message_bytes` for a full serialized transaction."""
        _, message = decode_solana_transaction(raw_transaction)
        return _inspect_message(
            self,
            message,
            signer=signer,
            max_swap_lamports=max_swap_lamports,
            max_priority_fee_lamports=max_priority_fee_lamports,
        )

    def _check_associated_token_instruction(
        self, idx: int, inst: dict[str, Any], signer: str | None
    ) -> None:
        """Allow only ATA CreateIdempotent for the signer's own allowlisted mint."""
        if not signer:
            raise JupiterRouteError(
                f"Instruction at index {idx}: associated token instruction "
                "requires an expected signer address"
            )
        raw_data = inst.get("data")
        if not isinstance(raw_data, str):
            raise JupiterRouteError(
                f"Instruction at index {idx}: associated token instruction "
                "data must be a base64 string"
            )
        try:
            data = base64.b64decode(raw_data, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise JupiterRouteError(
                f"Instruction at index {idx}: invalid base64 instruction data"
            ) from exc
        if data != _ATA_CREATE_IDEMPOTENT_DATA:
            raise JupiterRouteError(
                f"Instruction at index {idx}: only associated token "
                "CreateIdempotent is allowed"
            )
        accounts = inst.get("accounts")
        if not isinstance(accounts, list) or len(accounts) != _ATA_ACCOUNT_COUNT:
            raise JupiterRouteError(
                f"Instruction at index {idx}: associated token instruction "
                f"must have exactly {_ATA_ACCOUNT_COUNT} accounts"
            )
        keys = [a.get("pubkey") if isinstance(a, dict) else None for a in accounts]
        payer, _ata, owner, mint, system_program, token_program = keys
        if payer != signer:
            raise JupiterRouteError(
                f"Instruction at index {idx}: associated token payer must be the signer"
            )
        if owner != signer:
            raise JupiterRouteError(
                f"Instruction at index {idx}: associated token owner must be the signer"
            )
        if mint not in self.allowed_mints:
            raise JupiterRouteError(
                f"Instruction at index {idx}: associated token mint "
                f"'{mint}' is not allowlisted"
            )
        if system_program != SYSTEM_PROGRAM_ID:
            raise JupiterRouteError(
                f"Instruction at index {idx}: unexpected system program account"
            )
        if token_program != SPL_TOKEN_PROGRAM_ID:
            raise JupiterRouteError(
                f"Instruction at index {idx}: associated token program uses "
                f"unapproved token program '{token_program}'"
            )


# --- Real transaction-byte decoding (W3) ----------------------------------------

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_MAX_MESSAGE_BYTES = 1232  # Solana packet limit for a serialized transaction
_COMPUTE_BUDGET_SET_LIMIT = 2
_COMPUTE_BUDGET_SET_PRICE = 3
_TOKEN_SYNC_NATIVE = 17
_TOKEN_CLOSE_ACCOUNT = 9
_SYSTEM_TRANSFER = 2
MAX_COMPUTE_UNIT_LIMIT = 1_400_000


def base58_encode(data: bytes) -> str:
    """Bitcoin-alphabet base58 (no checksum), as used for Solana keys."""
    number = int.from_bytes(data, "big")
    out = ""
    while number:
        number, rem = divmod(number, 58)
        out = _B58_ALPHABET[rem] + out
    leading = len(data) - len(data.lstrip(b"\x00"))
    return "1" * leading + out


def base58_decode(text: str) -> bytes:
    number = 0
    for char in text:
        index = _B58_ALPHABET.find(char)
        if index < 0:
            raise JupiterRouteError(f"Invalid base58 character '{char}'")
        number = number * 58 + index
    leading = len(text) - len(text.lstrip("1"))
    body = number.to_bytes((number.bit_length() + 7) // 8, "big")
    return b"\x00" * leading + body


@dataclass(frozen=True, slots=True)
class DecodedSolanaInstruction:
    program_id: str
    accounts: tuple[str, ...]
    data: bytes


@dataclass(frozen=True, slots=True)
class DecodedSolanaMessage:
    version: str  # "legacy" or "v0"
    required_signatures: int
    account_keys: tuple[str, ...]
    recent_blockhash: str
    instructions: tuple[DecodedSolanaInstruction, ...]


class _Reader:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self.pos = 0

    def take(self, count: int) -> bytes:
        if count < 0 or self.pos + count > len(self._data):
            raise JupiterRouteError("Solana message is truncated")
        chunk = self._data[self.pos : self.pos + count]
        self.pos += count
        return chunk

    def byte(self) -> int:
        return self.take(1)[0]

    def shortvec(self) -> int:
        value = 0
        for shift in (0, 7, 14):
            current = self.byte()
            value |= (current & 0x7F) << shift
            if not current & 0x80:
                if shift and current == 0:
                    raise JupiterRouteError("Non-canonical shortvec length")
                if value > 0xFFFF:
                    raise JupiterRouteError("Shortvec length out of range")
                return value
        raise JupiterRouteError("Shortvec length too long")

    def finished(self) -> bool:
        return self.pos == len(self._data)


def decode_solana_message(raw: bytes) -> DecodedSolanaMessage:
    """Decode a serialized legacy or v0 message; fail closed on anything unusual.

    v0 messages with address-table lookups are rejected because the looked-up
    accounts cannot be resolved offline and would hide program ids.
    """
    if not raw or len(raw) > _MAX_MESSAGE_BYTES:
        raise JupiterRouteError("Solana message is empty or exceeds the packet limit")
    reader = _Reader(raw)
    first = raw[0]
    version = "legacy"
    if first & 0x80:
        if first != 0x80:
            raise JupiterRouteError(f"Unsupported message version {first & 0x7F}")
        reader.byte()
        version = "v0"
    required = reader.byte()
    readonly_signed = reader.byte()
    readonly_unsigned = reader.byte()
    key_count = reader.shortvec()
    keys = tuple(base58_encode(reader.take(32)) for _ in range(key_count))
    if (
        required < 1
        or readonly_signed >= required
        or required > key_count
        or readonly_unsigned > key_count - required
    ):
        raise JupiterRouteError("Inconsistent Solana message header")
    blockhash = base58_encode(reader.take(32))
    instructions = []
    for _ in range(reader.shortvec()):
        program_index = reader.byte()
        account_indexes = reader.take(reader.shortvec())
        data = reader.take(reader.shortvec())
        if program_index >= key_count or program_index < required:
            raise JupiterRouteError("Instruction program index is invalid")
        if any(index >= key_count for index in account_indexes):
            raise JupiterRouteError("Instruction references an unresolved account")
        instructions.append(
            DecodedSolanaInstruction(
                keys[program_index], tuple(keys[i] for i in account_indexes), data
            )
        )
    if version == "v0":
        if reader.shortvec() != 0:
            raise JupiterRouteError("Address table lookups are not permitted")
    if not reader.finished():
        raise JupiterRouteError("Trailing bytes after Solana message")
    if not instructions:
        raise JupiterRouteError("Transaction payload contains no instructions")
    return DecodedSolanaMessage(version, required, keys, blockhash, tuple(instructions))


def decode_solana_transaction(raw: bytes) -> tuple[int, DecodedSolanaMessage]:
    """Split a serialized transaction into signature count and decoded message."""
    reader = _Reader(raw)
    count = reader.shortvec()
    reader.take(64 * count)
    message = decode_solana_message(raw[reader.pos :])
    if count != message.required_signatures:
        raise JupiterRouteError("Signature count does not match the message header")
    return count, message


def _inspect_message_instruction(
    idx: int,
    inst: DecodedSolanaInstruction,
    signer: str,
    max_swap_lamports: int,
) -> int:
    """Per-program byte-level checks; returns the priority-price (µlamports/CU)."""
    price = 0
    if inst.program_id == COMPUTE_BUDGET_PROGRAM_ID:
        kind = inst.data[:1]
        if kind == bytes([_COMPUTE_BUDGET_SET_LIMIT]) and len(inst.data) == 5:
            limit = int.from_bytes(inst.data[1:], "little")
            if not 0 < limit <= MAX_COMPUTE_UNIT_LIMIT:
                raise JupiterRouteError(
                    f"Instruction at index {idx}: compute unit limit {limit} invalid"
                )
        elif kind == bytes([_COMPUTE_BUDGET_SET_PRICE]) and len(inst.data) == 9:
            price = int.from_bytes(inst.data[1:], "little")
        else:
            raise JupiterRouteError(
                f"Instruction at index {idx}: only SetComputeUnitLimit and "
                "SetComputeUnitPrice are allowed"
            )
    elif inst.program_id == SYSTEM_PROGRAM_ID:
        if (
            len(inst.data) != 12
            or int.from_bytes(inst.data[:4], "little") != _SYSTEM_TRANSFER
        ):
            raise JupiterRouteError(
                f"Instruction at index {idx}: only system Transfer is allowed"
            )
        lamports = int.from_bytes(inst.data[4:], "little")
        if not inst.accounts or inst.accounts[0] != signer:
            raise JupiterRouteError(
                f"Instruction at index {idx}: system transfer source must be the signer"
            )
        if lamports > max_swap_lamports:
            raise JupiterRouteError(
                f"Instruction at index {idx}: system transfer of {lamports} "
                f"lamports exceeds the permitted {max_swap_lamports}"
            )
    elif inst.program_id == SPL_TOKEN_PROGRAM_ID:
        kind = inst.data[:1]
        if kind == bytes([_TOKEN_SYNC_NATIVE]) and len(inst.data) == 1:
            pass
        elif kind == bytes([_TOKEN_CLOSE_ACCOUNT]) and len(inst.data) == 1:
            if len(inst.accounts) < 3 or inst.accounts[1] != signer:
                raise JupiterRouteError(
                    f"Instruction at index {idx}: CloseAccount destination "
                    "must be the signer"
                )
            if inst.accounts[2] != signer:
                raise JupiterRouteError(
                    f"Instruction at index {idx}: CloseAccount owner must be the signer"
                )
        else:
            raise JupiterRouteError(
                f"Instruction at index {idx}: top-level token instruction "
                f"0x{inst.data[:1].hex() or '??'} is not allowed"
            )
    elif inst.program_id == JUPITER_V6_PROGRAM_ID and signer not in inst.accounts:
        raise JupiterRouteError(
            f"Instruction at index {idx}: Jupiter instruction omits the signer"
        )
    return price


def _inspect_message(
    adapter: "JupiterSwapAdapter",
    message: DecodedSolanaMessage,
    *,
    signer: str,
    max_swap_lamports: int,
    max_priority_fee_lamports: int,
) -> DecodedSolanaMessage:
    if message.required_signatures != 1 or message.account_keys[0] != signer:
        raise JupiterRouteError(
            "The signer must be the fee payer and the only required signature"
        )
    payload = {
        "instructions": [
            {
                "programId": inst.program_id,
                "accounts": [{"pubkey": key} for key in inst.accounts],
                "data": base64.b64encode(inst.data).decode("ascii"),
            }
            for inst in message.instructions
        ]
    }
    adapter.inspect_swap_instructions(payload, signer=signer)
    if not any(i.program_id == JUPITER_V6_PROGRAM_ID for i in message.instructions):
        raise JupiterRouteError("Transaction does not invoke the Jupiter program")
    price = 0
    limit = MAX_COMPUTE_UNIT_LIMIT
    for idx, inst in enumerate(message.instructions):
        found_price = _inspect_message_instruction(idx, inst, signer, max_swap_lamports)
        price = max(price, found_price)
        if inst.program_id == COMPUTE_BUDGET_PROGRAM_ID and inst.data[:1] == bytes(
            [_COMPUTE_BUDGET_SET_LIMIT]
        ):
            limit = int.from_bytes(inst.data[1:], "little")
    priority_fee = price * limit // 1_000_000
    if priority_fee > max_priority_fee_lamports:
        raise JupiterRouteError(
            f"Priority fee {priority_fee} lamports exceeds the cap "
            f"{max_priority_fee_lamports}"
        )
    return message
