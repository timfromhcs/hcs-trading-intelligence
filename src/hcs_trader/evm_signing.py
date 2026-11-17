"""Chain-valid EVM (EIP-1559) transaction building, independent decoding and signing.

Scope (KL-002): Ethereum Sepolia (chainId 11155111) only by default. Mainnet is
disabled unless ``EvmSigningPolicy.allow_mainnet`` is explicitly set (default off)
and mainnet routers/tokens are supplied by the operator.

Two transaction shapes are supported, both with ``value == 0`` and an empty
access list:

* ``approve(spender, amount)`` on an allowlisted ERC-20 (``to`` must be the plan's
  ``token_in_mint``), spender in the router allowlist, ``amount == amountIn``,
  never unlimited.
* Uniswap SwapRouter02 ``multicall(uint256 deadline, bytes[] data)`` wrapping
  exactly one ``exactInputSingle`` call. The wrapper is required because
  SwapRouter02's bare ``exactInputSingle`` carries no deadline, and the plan's
  deadline must be bound on-chain.

The decoder never trusts the builder: it re-parses the raw bytes with strict RLP
and strict ABI decoding, and re-encodes calldata to reject non-canonical bytes.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import rlp  # type: ignore[import-untyped]
from eth_abi.abi import decode as abi_decode
from eth_abi.abi import encode as abi_encode
from eth_account import Account
from eth_utils.address import to_checksum_address
from eth_utils.crypto import keccak

from hcs_trader.domain.signing import (
    SignedTransaction,
    SignerPolicyViolation,
    SignerTamperingError,
    ValidatedTransactionPlan,
)

SEPOLIA_CHAIN_ID = 11155111
MAINNET_CHAIN_ID = 1

# Verified against Uniswap's official deployment docs
# (developers.uniswap.org -> v3 Ethereum deployments, Ethereum Sepolia column).
SEPOLIA_SWAP_ROUTER02 = "0x3bFA4769FB09eefC5a80d6E87c3B9C650f7Ae48E"
# Uniswap docs "Wrapped Native Token" for Sepolia.
SEPOLIA_WETH = "0xfFf9976782d46CC05630D1f6eBAb18b2324d6B14"
# Circle's published Sepolia USDC (also in the existing evm_swap registry).
SEPOLIA_USDC = "0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238"

DEFAULT_ROUTERS = frozenset({SEPOLIA_SWAP_ROUTER02.lower()})
DEFAULT_TOKENS = frozenset({SEPOLIA_WETH.lower(), SEPOLIA_USDC.lower()})

SELECTOR_APPROVE = bytes.fromhex("095ea7b3")
SELECTOR_MULTICALL = bytes.fromhex("5ae401dc")  # multicall(uint256,bytes[])
# exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))
SELECTOR_EXACT_INPUT_SINGLE = bytes.fromhex("04e45aaf")

_SINGLE_TYPES = "(address,address,uint24,address,uint256,uint256,uint160)"
ALLOWED_FEE_TIERS = frozenset({100, 500, 3000, 10000})
MAX_UINT256 = 2**256 - 1
UNLIMITED_ALLOWANCE_THRESHOLD = 2**255
_ADDR = re.compile(r"^0x[a-fA-F0-9]{40}$")


@dataclass(frozen=True, slots=True)
class EvmSigningPolicy:
    """Immutable EVM-specific signing envelope (in addition to SignerPolicy)."""

    allow_mainnet: bool = False
    allowed_routers: frozenset[str] = DEFAULT_ROUTERS
    allowed_tokens: frozenset[str] = DEFAULT_TOKENS
    max_gas_limit: int = 400_000
    allowed_fee_tiers: frozenset[int] = field(default=ALLOWED_FEE_TIERS)

    def allowed_chain_numbers(self) -> frozenset[int]:
        if self.allow_mainnet:
            return frozenset({SEPOLIA_CHAIN_ID, MAINNET_CHAIN_ID})
        return frozenset({SEPOLIA_CHAIN_ID})


@dataclass(frozen=True, slots=True)
class DecodedEvmTx:
    kind: str  # "approve" | "swap"
    chain_id: int
    nonce: int
    max_priority_fee_per_gas: int
    max_fee_per_gas: int
    gas_limit: int
    to: str  # lowercase 0x address
    data: bytes


def _addr(value: str, what: str) -> str:
    if not _ADDR.match(value):
        raise SignerTamperingError(f"{what} is not a valid EVM address: {value!r}")
    return value.lower()


def _chain_number(plan: ValidatedTransactionPlan) -> int:
    ns, _, ref = plan.canonical_chain_id.partition(":")
    if ns != "eip155" or not ref.isdigit():
        raise SignerPolicyViolation(
            f"EVM signer requires eip155 chain id, got '{plan.canonical_chain_id}'"
        )
    return int(ref)


def _to_hex(addr: bytes) -> str:
    return "0x" + addr.hex()


# --------------------------------------------------------------------------- build


def encode_approve_calldata(spender: str, amount: int) -> bytes:
    if not 0 < amount < UNLIMITED_ALLOWANCE_THRESHOLD:
        raise SignerPolicyViolation("Approval amount must be positive and bounded")
    return SELECTOR_APPROVE + abi_encode(
        ["address", "uint256"], [_addr(spender, "spender"), amount]
    )


def encode_swap_calldata(
    *,
    token_in: str,
    token_out: str,
    fee_tier: int,
    recipient: str,
    amount_in: int,
    amount_out_minimum: int,
    deadline_ts: int,
) -> bytes:
    inner = SELECTOR_EXACT_INPUT_SINGLE + abi_encode(
        [_SINGLE_TYPES],
        [
            (
                _addr(token_in, "tokenIn"),
                _addr(token_out, "tokenOut"),
                fee_tier,
                _addr(recipient, "recipient"),
                amount_in,
                amount_out_minimum,
                0,
            )
        ],
    )
    return SELECTOR_MULTICALL + abi_encode(
        ["uint256", "bytes[]"], [deadline_ts, [inner]]
    )


def build_unsigned_eip1559(
    *,
    chain_id: int,
    nonce: int,
    max_priority_fee_per_gas: int,
    max_fee_per_gas: int,
    gas_limit: int,
    to: str,
    data: bytes,
) -> bytes:
    """Serialize an unsigned type-2 payload: ``0x02 || rlp([...])`` (value=0)."""
    fields = [
        chain_id,
        nonce,
        max_priority_fee_per_gas,
        max_fee_per_gas,
        gas_limit,
        bytes.fromhex(_addr(to, "to")[2:]),
        0,
        data,
        [],
    ]
    return bytes(b"\x02" + rlp.encode(fields))


def build_swap_payload(
    plan: ValidatedTransactionPlan,
    *,
    fee_tier: int,
    gas_limit: int,
    max_fee_per_gas: int,
    max_priority_fee_per_gas: int,
) -> bytes:
    """Build the unsigned swap payload strictly from the typed plan."""
    if plan.deadline is None or plan.nonce is None:
        raise SignerPolicyViolation("EVM swap plan requires deadline and nonce")
    if plan.token_in_mint is None or plan.token_out_mint is None:
        raise SignerPolicyViolation("EVM swap plan requires token_in/token_out")
    data = encode_swap_calldata(
        token_in=plan.token_in_mint,
        token_out=plan.token_out_mint,
        fee_tier=fee_tier,
        recipient=plan.signer_address,
        amount_in=plan.amount_in_base_units,
        amount_out_minimum=plan.min_amount_out_base_units,
        deadline_ts=int(plan.deadline.timestamp()),
    )
    return build_unsigned_eip1559(
        chain_id=_chain_number(plan),
        nonce=plan.nonce,
        max_priority_fee_per_gas=max_priority_fee_per_gas,
        max_fee_per_gas=max_fee_per_gas,
        gas_limit=gas_limit,
        to=plan.destination_id,
        data=data,
    )


def build_approve_payload(
    plan: ValidatedTransactionPlan,
    *,
    spender: str,
    gas_limit: int,
    max_fee_per_gas: int,
    max_priority_fee_per_gas: int,
) -> bytes:
    """Build the unsigned exact-amount approve payload (to == plan.destination_id)."""
    if plan.nonce is None:
        raise SignerPolicyViolation("EVM approve plan requires nonce")
    return build_unsigned_eip1559(
        chain_id=_chain_number(plan),
        nonce=plan.nonce,
        max_priority_fee_per_gas=max_priority_fee_per_gas,
        max_fee_per_gas=max_fee_per_gas,
        gas_limit=gas_limit,
        to=plan.destination_id,
        data=encode_approve_calldata(spender, plan.amount_in_base_units),
    )


# --------------------------------------------------------------------------- decode


def _be_int(raw: Any, what: str) -> int:
    if not isinstance(raw, bytes):
        raise SignerTamperingError(f"{what} is not an RLP scalar")
    if len(raw) > 1 and raw[0] == 0:
        raise SignerTamperingError(f"{what} has non-canonical leading zero")
    if len(raw) > 32:
        raise SignerTamperingError(f"{what} exceeds 256 bits")
    return int.from_bytes(raw, "big")


def _decode_raw(payload: bytes) -> tuple[int, int, int, int, int, str, bytes]:
    if len(payload) < 2 or payload[0] != 0x02:
        raise SignerTamperingError("Payload is not an unsigned EIP-1559 (type 2) tx")
    try:
        items = rlp.decode(payload[1:])
    except Exception as err:
        raise SignerTamperingError(f"Payload RLP is invalid: {err}") from err
    if not isinstance(items, list) or len(items) != 9:
        raise SignerTamperingError("EIP-1559 unsigned payload must have 9 RLP fields")
    chain_id = _be_int(items[0], "chainId")
    nonce = _be_int(items[1], "nonce")
    prio = _be_int(items[2], "maxPriorityFeePerGas")
    max_fee = _be_int(items[3], "maxFeePerGas")
    gas = _be_int(items[4], "gasLimit")
    to_raw, value_raw, data, access = items[5], items[6], items[7], items[8]
    if not isinstance(to_raw, bytes) or len(to_raw) != 20:
        raise SignerTamperingError("Contract creation / malformed 'to' is forbidden")
    if _be_int(value_raw, "value") != 0:
        raise SignerPolicyViolation("Non-zero ETH value is not permitted")
    if not isinstance(data, bytes):
        raise SignerTamperingError("data is not bytes")
    if access != []:
        raise SignerPolicyViolation("Access lists are not permitted")
    # Canonical re-encoding rejects any trailing / non-minimal encoding.
    if b"\x02" + rlp.encode(items) != payload:
        raise SignerTamperingError("Payload is not canonically encoded")
    return chain_id, nonce, prio, max_fee, gas, _to_hex(to_raw), data


def decode_evm_payload(
    plan: ValidatedTransactionPlan,
    payload: bytes,
    policy: EvmSigningPolicy,
) -> DecodedEvmTx:
    """Independently decode ``payload`` and verify it matches ``plan`` exactly.

    Raises only ``SignerTamperingError`` / ``SignerPolicyViolation`` subclasses of
    ``SignerError`` for any input, including arbitrary bytes.
    """
    plan_chain = _chain_number(plan)
    if plan_chain not in policy.allowed_chain_numbers():
        raise SignerPolicyViolation(
            f"EVM chain {plan_chain} is not enabled (mainnet requires explicit flag)"
        )
    chain_id, nonce, prio, max_fee, gas, to, data = _decode_raw(payload)
    if chain_id != plan_chain:
        raise SignerTamperingError(
            f"Payload chainId {chain_id} does not match plan chain {plan_chain}"
        )
    if plan.nonce is None or nonce != plan.nonce:
        raise SignerTamperingError(f"Payload nonce {nonce} does not match plan")
    if to != _addr(plan.destination_id, "plan destination"):
        raise SignerTamperingError("Payload 'to' does not match plan destination")
    if gas <= 0 or gas > policy.max_gas_limit:
        raise SignerPolicyViolation(f"Gas limit {gas} outside (0, policy max]")
    if prio > max_fee:
        raise SignerPolicyViolation("maxPriorityFeePerGas exceeds maxFeePerGas")
    if max_fee * gas > plan.max_network_fee_base_units:
        raise SignerPolicyViolation("Worst-case network fee exceeds plan cap")
    if prio > plan.max_priority_fee_base_units:
        raise SignerPolicyViolation("Priority fee exceeds plan cap")

    selector = data[:4]
    if selector == SELECTOR_APPROVE:
        _verify_approve(plan, policy, to, data)
        kind = "approve"
    elif selector == SELECTOR_MULTICALL:
        _verify_swap(plan, policy, to, data)
        kind = "swap"
    else:
        raise SignerTamperingError(f"Unexpected selector '{selector.hex()}'")
    return DecodedEvmTx(kind, chain_id, nonce, prio, max_fee, gas, to, data)


def _verify_approve(
    plan: ValidatedTransactionPlan, policy: EvmSigningPolicy, to: str, data: bytes
) -> None:
    if plan.token_in_mint is None or to != _addr(plan.token_in_mint, "token_in"):
        raise SignerTamperingError("approve must target the plan's token_in contract")
    if to not in policy.allowed_tokens:
        raise SignerPolicyViolation("approve target token is not allowlisted")
    if len(data) != 68:
        raise SignerTamperingError("approve calldata must be exactly 68 bytes")
    try:
        spender, amount = abi_decode(["address", "uint256"], data[4:])
    except Exception as err:
        raise SignerTamperingError(f"approve calldata undecodable: {err}") from err
    if encode_raw(SELECTOR_APPROVE, ["address", "uint256"], [spender, amount]) != data:
        raise SignerTamperingError("approve calldata is not canonical")
    if amount >= UNLIMITED_ALLOWANCE_THRESHOLD or amount == MAX_UINT256:
        raise SignerPolicyViolation("Unlimited token approval is forbidden")
    if amount != plan.amount_in_base_units:
        raise SignerTamperingError("approve amount does not equal plan amountIn")
    if spender.lower() not in policy.allowed_routers:
        raise SignerPolicyViolation("approve spender is not an allowlisted router")


def encode_raw(selector: bytes, types: list[str], values: list[Any]) -> bytes:
    return selector + abi_encode(types, values)


def _verify_swap(
    plan: ValidatedTransactionPlan, policy: EvmSigningPolicy, to: str, data: bytes
) -> None:
    if to not in policy.allowed_routers:
        raise SignerPolicyViolation("swap target is not an allowlisted router")
    if plan.deadline is None:
        raise SignerPolicyViolation("EVM swap plan requires a deadline")
    if plan.token_in_mint is None or plan.token_out_mint is None:
        raise SignerPolicyViolation("EVM swap plan requires token_in/token_out")
    try:
        deadline_ts, calls = abi_decode(["uint256", "bytes[]"], data[4:])
    except Exception as err:
        raise SignerTamperingError(f"multicall calldata undecodable: {err}") from err
    if (
        encode_raw(SELECTOR_MULTICALL, ["uint256", "bytes[]"], [deadline_ts, calls])
        != data
    ):
        raise SignerTamperingError("multicall calldata is not canonical")
    if deadline_ts != int(plan.deadline.timestamp()):
        raise SignerTamperingError("swap deadline does not match plan deadline")
    if len(calls) != 1:
        raise SignerTamperingError("multicall must wrap exactly one call")
    inner = calls[0]
    if inner[:4] != SELECTOR_EXACT_INPUT_SINGLE:
        raise SignerTamperingError("inner call is not exactInputSingle")
    try:
        (params,) = abi_decode([_SINGLE_TYPES], inner[4:])
    except Exception as err:
        raise SignerTamperingError(f"exactInputSingle undecodable: {err}") from err
    if SELECTOR_EXACT_INPUT_SINGLE + abi_encode([_SINGLE_TYPES], [params]) != inner:
        raise SignerTamperingError("exactInputSingle calldata is not canonical")
    token_in, token_out, fee, recipient, amount_in, amount_out_min, sqrt_limit = params
    if token_in.lower() != _addr(plan.token_in_mint, "token_in"):
        raise SignerTamperingError("tokenIn does not match plan")
    if token_out.lower() != _addr(plan.token_out_mint, "token_out"):
        raise SignerTamperingError("tokenOut does not match plan")
    if token_in.lower() not in policy.allowed_tokens:
        raise SignerPolicyViolation("tokenIn is not allowlisted")
    if token_out.lower() not in policy.allowed_tokens:
        raise SignerPolicyViolation("tokenOut is not allowlisted")
    if token_in.lower() == token_out.lower():
        raise SignerTamperingError("tokenIn equals tokenOut")
    if recipient.lower() != _addr(plan.signer_address, "signer"):
        raise SignerTamperingError("recipient is not the signer address")
    if amount_in != plan.amount_in_base_units:
        raise SignerTamperingError("amountIn does not match plan")
    if amount_out_min != plan.min_amount_out_base_units:
        raise SignerTamperingError("amountOutMinimum does not match plan")
    if fee not in policy.allowed_fee_tiers:
        raise SignerPolicyViolation(f"pool fee tier {fee} is not allowed")
    if sqrt_limit != 0:
        raise SignerPolicyViolation("sqrtPriceLimitX96 must be 0")


# --------------------------------------------------------------------------- sign


def sign_evm_payload(
    plan: ValidatedTransactionPlan,
    decoded: DecodedEvmTx,
    private_key: bytes,
    at: datetime,
) -> SignedTransaction:
    """Sign a payload already verified by :func:`decode_evm_payload`.

    The transaction dict is rebuilt from the *decoded* fields, the key is bound to
    ``plan.signer_address`` and the recovered signer is re-checked after signing.
    """
    if len(private_key) != 32:
        raise SignerTamperingError("EVM private key must be 32 bytes")
    account = Account.from_key(private_key)
    if account.address.lower() != plan.signer_address.lower():
        raise SignerTamperingError("Keystore key does not control plan signer address")
    tx: dict[str, Any] = {
        "type": 2,
        "chainId": decoded.chain_id,
        "nonce": decoded.nonce,
        "maxPriorityFeePerGas": decoded.max_priority_fee_per_gas,
        "maxFeePerGas": decoded.max_fee_per_gas,
        "gas": decoded.gas_limit,
        "to": to_checksum_address(decoded.to),
        "value": 0,
        "data": decoded.data,
        "accessList": [],
    }
    signed = account.sign_transaction(tx)
    raw = bytes(signed.raw_transaction)
    if Account.recover_transaction(raw).lower() != plan.signer_address.lower():
        raise SignerTamperingError("Recovered signer does not match plan signer")
    return SignedTransaction(
        plan_hash=plan.plan_hash,
        raw_transaction_hex=raw.hex(),
        tx_hash=keccak(raw).hex(),
        signed_at=at,
    )
