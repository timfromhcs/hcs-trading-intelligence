"""Chain-valid Solana message decoding, policy verification and signing.

The signer receives serialized Solana ``VersionedMessage`` bytes (legacy or v0),
decodes them independently with ``solders`` and checks them against the
``ValidatedTransactionPlan`` and an immutable ``SolanaMessagePolicy`` before a
``Keypair`` (which exists only inside ``sign_solana_message``) signs them.

Fail-closed rules: only System transfer, SPL Token ``TransferChecked`` (plus
``SyncNative``/``CloseAccount`` when Jupiter is allowed), ATA
``CreateIdempotent`` and two Compute Budget instructions are understood; every
other program or instruction variant is rejected. ATA creations must derive
correctly and be bound to the plan (destination for plain SPL transfers; the
signer's own wallet and the plan mints for Jupiter). Jupiter route instruction
data is opaque, so for Jupiter swaps the signer binds the program id, the sole
signer and fee payer, fees, System transfers (only wrap-SOL funding of the
signer's own WSOL account) and ATA creations, but cannot decode swap amounts,
token movements or output accounts.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from datetime import datetime

from solders.compute_budget import ID as COMPUTE_BUDGET_ID
from solders.keypair import Keypair
from solders.message import (
    Message,
    MessageV0,
    from_bytes_versioned,
    to_bytes_versioned,
)
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.system_program import ID as SYSTEM_PROGRAM_ID
from solders.token.associated import get_associated_token_address
from solders.transaction import VersionedTransaction

from hcs_trader.domain.signing import (
    SignedTransaction,
    SignerPolicyViolation,
    SignerTamperingError,
    ValidatedTransactionPlan,
)

TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
ATA_PROGRAM_ID = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
JUPITER_V6_PROGRAM_ID = Pubkey.from_string(
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
)
WSOL_MINT = "So11111111111111111111111111111111111111112"

LAMPORTS_PER_SIGNATURE = 5000
_SYSTEM_TRANSFER = 2
_SPL_CLOSE_ACCOUNT = 9
_SPL_TRANSFER_CHECKED = 12
_SPL_SYNC_NATIVE = 17
_ATA_CREATE_IDEMPOTENT = 1
_CB_SET_UNIT_LIMIT = 2
_CB_SET_UNIT_PRICE = 3
_MAX_MESSAGE_BYTES = 1232
_MAX_ATA_CREATIONS = 4


class SolanaMessageError(SignerTamperingError):
    """The Solana message violates structure, allowlist or plan binding."""


@dataclass(frozen=True, slots=True)
class SolanaMessagePolicy:
    allow_jupiter: bool = False
    max_compute_unit_limit: int = 1_400_000
    max_instructions: int = 16


@dataclass(frozen=True, slots=True)
class DecodedSolanaMessage:
    fee_payer: str
    system_transfers: tuple[tuple[str, str, int], ...]  # (from, to, lamports)
    ata_creations: tuple[tuple[str, str, str], ...]  # (ata, owner, mint)
    spl_transfers: tuple[tuple[str, str, str, int, int], ...]  # src,mint,dst,amt,dec
    uses_jupiter: bool
    priority_fee_lamports: int
    base_fee_lamports: int
    programs: frozenset[str]

    @property
    def total_fee_lamports(self) -> int:
        return self.base_fee_lamports + self.priority_fee_lamports


def _reject(msg: str) -> SolanaMessageError:
    return SolanaMessageError(f"Solana message rejected: {msg}")


def _bind_jupiter_accounts(
    plan: ValidatedTransactionPlan,
    signer: str,
    sys_transfers: list[tuple[str, str, int]],
    ata_creations: list[tuple[str, str, str]],
) -> None:
    """Constrain the non-opaque parts of a Jupiter message to the signer.

    System transfers are only the standard wrap-SOL funding of the signer's own
    wrapped-SOL account, up to the plan amount, and only for a WSOL input. ATA
    creations may only be for the signer's own wallet and the plan's mints, so
    the signer cannot be made to fund accounts owned by anyone else.
    """
    if sys_transfers:
        if plan.token_in_mint != WSOL_MINT:
            raise _reject("System transfer in a Jupiter swap needs a WSOL input")
        wsol_ata = str(
            get_associated_token_address(
                Pubkey.from_string(signer), Pubkey.from_string(WSOL_MINT)
            )
        )
        if any(to != wsol_ata for _f, to, _a in sys_transfers):
            raise _reject("System transfer recipient is not the signer's WSOL account")
        if sum(a for _f, _t, a in sys_transfers) > plan.amount_in_base_units:
            raise _reject("outgoing SOL exceeds plan amount")
    mints = {m for m in (plan.token_in_mint, plan.token_out_mint) if m}
    for _ata, owner, mint in ata_creations:
        if owner != signer or mint not in mints:
            raise _reject("ATA creation is not bound to the signer and plan mints")


def decode_solana_message(  # noqa: C901 - one linear allowlist walk
    message_bytes: bytes,
    plan: ValidatedTransactionPlan,
    policy: SolanaMessagePolicy,
) -> DecodedSolanaMessage:
    """Decode ``message_bytes`` and verify it against ``plan`` and ``policy``."""
    if not message_bytes or len(message_bytes) > _MAX_MESSAGE_BYTES:
        raise _reject("message empty or larger than the 1232-byte packet limit")
    try:
        msg = from_bytes_versioned(message_bytes)
    except Exception as err:
        raise _reject(f"cannot decode message: {err}") from err
    if not isinstance(msg, Message | MessageV0):
        raise _reject("unsupported message type")
    if to_bytes_versioned(msg) != message_bytes:
        raise _reject("non-canonical message encoding (trailing or altered bytes)")

    keys = list(msg.account_keys)
    signer = plan.signer_address
    if not keys or str(keys[0]) != signer:
        raise _reject("plan signer is not the fee payer")
    if msg.header.num_required_signatures != 1:
        raise _reject("message requires signatures other than the signer")
    uses_alts = isinstance(msg, MessageV0) and len(msg.address_table_lookups) > 0
    if not msg.instructions:
        raise _reject("message has no instructions")
    if len(msg.instructions) > policy.max_instructions:
        raise _reject("too many instructions")

    sys_transfers: list[tuple[str, str, int]] = []
    spl_transfers: list[tuple[str, str, str, int, int]] = []
    ata_creations: list[tuple[str, str, str]] = []
    programs: set[str] = set()
    uses_jupiter = False
    cu_limit: int | None = None
    cu_price = 0

    def acct(indexes: bytes, i: int) -> str:
        if i >= len(indexes):
            raise _reject("instruction missing required account")
        idx = indexes[i]
        if idx >= len(keys):
            raise _reject("instruction references a lookup-table account")
        return str(keys[idx])

    for ix in msg.instructions:
        if ix.program_id_index >= len(keys):
            raise _reject("program id outside static account keys")
        prog = keys[ix.program_id_index]
        programs.add(str(prog))
        data = bytes(ix.data)
        accts = bytes(ix.accounts)
        if prog == COMPUTE_BUDGET_ID:
            if data[:1] == bytes([_CB_SET_UNIT_LIMIT]) and len(data) == 5:
                cu_limit = struct.unpack("<I", data[1:])[0]
            elif data[:1] == bytes([_CB_SET_UNIT_PRICE]) and len(data) == 9:
                cu_price = struct.unpack("<Q", data[1:])[0]
            else:
                raise _reject("unsupported compute budget instruction")
        elif prog == SYSTEM_PROGRAM_ID:
            if (
                len(data) != 12
                or struct.unpack("<I", data[:4])[0] != _SYSTEM_TRANSFER
                or len(accts) != 2
            ):
                raise _reject("unsupported System Program instruction")
            lamports = struct.unpack("<Q", data[4:])[0]
            sys_transfers.append((acct(accts, 0), acct(accts, 1), lamports))
        elif prog == TOKEN_PROGRAM_ID:
            kind = data[:1]
            if kind == bytes([_SPL_TRANSFER_CHECKED]) and len(data) == 10:
                amount = struct.unpack("<Q", data[1:9])[0]
                src, mint, dst, auth = (acct(accts, i) for i in range(4))
                if auth != signer:
                    raise _reject("SPL transfer authority is not the signer")
                spl_transfers.append((src, mint, dst, amount, data[9]))
            elif kind == bytes([_SPL_SYNC_NATIVE]) and len(data) == 1:
                if not policy.allow_jupiter:
                    raise _reject("SyncNative only allowed in Jupiter swaps")
            elif kind == bytes([_SPL_CLOSE_ACCOUNT]) and len(data) == 1:
                if not policy.allow_jupiter:
                    raise _reject("CloseAccount only allowed in Jupiter swaps")
                if acct(accts, 1) != signer or acct(accts, 2) != signer:
                    raise _reject("CloseAccount destination/owner is not the signer")
            else:
                raise _reject("unsupported SPL Token instruction")
        elif prog == ATA_PROGRAM_ID:
            if data != bytes([_ATA_CREATE_IDEMPOTENT]):
                raise _reject("only ATA CreateIdempotent is allowed")
            if acct(accts, 0) != signer:
                raise _reject("ATA funder is not the signer")
            ata, owner, mint = acct(accts, 1), acct(accts, 2), acct(accts, 3)
            try:
                derived = get_associated_token_address(
                    Pubkey.from_string(owner), Pubkey.from_string(mint)
                )
            except ValueError as err:
                raise _reject("ATA owner or mint is not a valid key") from err
            if str(derived) != ata:
                raise _reject("ATA address is not derived from its owner and mint")
            if acct(accts, 4) != str(SYSTEM_PROGRAM_ID) or acct(accts, 5) != str(
                TOKEN_PROGRAM_ID
            ):
                raise _reject("ATA create uses unexpected system/token program")
            ata_creations.append((ata, owner, mint))
        elif prog == JUPITER_V6_PROGRAM_ID:
            if not policy.allow_jupiter:
                raise _reject("Jupiter program not allowed by policy")
            uses_jupiter = True
        else:
            raise _reject(f"unknown program {prog}")

    if any(frm != signer for frm, _to, _amt in sys_transfers):
        raise _reject("System transfer source is not the signer")
    if uses_alts and not uses_jupiter:
        raise _reject("address table lookups only allowed for Jupiter swaps")
    if len(ata_creations) > _MAX_ATA_CREATIONS:
        raise _reject("too many ATA creations")

    # Fee bounds
    if cu_price and cu_limit is None:
        raise _reject("compute unit price requires an explicit compute unit limit")
    if cu_limit is not None and cu_limit > policy.max_compute_unit_limit:
        raise _reject("compute unit limit exceeds policy")
    priority = -(-((cu_limit or 0) * cu_price) // 1_000_000)
    base = LAMPORTS_PER_SIGNATURE
    if priority > plan.max_priority_fee_base_units:
        raise _reject("priority fee exceeds plan cap")
    if base + priority > plan.max_network_fee_base_units:
        raise _reject("network fee exceeds plan cap")

    # Plan binding
    if uses_jupiter:
        if plan.destination_id != str(JUPITER_V6_PROGRAM_ID):
            raise _reject("plan destination is not the Jupiter program")
        if spl_transfers:
            raise _reject("SPL transfers not allowed inside a Jupiter swap message")
        _bind_jupiter_accounts(plan, signer, sys_transfers, ata_creations)
    elif bool(sys_transfers) == bool(spl_transfers):
        raise _reject("message must contain exactly one kind of transfer")
    elif sys_transfers:
        if len(sys_transfers) != 1:
            raise _reject("exactly one System transfer allowed")
        if ata_creations:
            raise _reject("ATA creation not allowed with a System transfer")
        _f, to, amt = sys_transfers[0]
        if to != plan.destination_id or amt != plan.amount_in_base_units:
            raise _reject("System transfer does not match plan")
    else:
        if len(spl_transfers) != 1:
            raise _reject("exactly one SPL transfer allowed")
        _s, mint, dst, amt, _d = spl_transfers[0]
        if (
            dst != plan.destination_id
            or amt != plan.amount_in_base_units
            or mint != plan.token_in_mint
        ):
            raise _reject("SPL transfer does not match plan")
        for ata, _owner, ata_mint in ata_creations:
            # The only account the signer may fund is the transfer destination.
            if ata != plan.destination_id or ata_mint != plan.token_in_mint:
                raise _reject("ATA creation is not bound to the plan destination")

    return DecodedSolanaMessage(
        fee_payer=signer,
        system_transfers=tuple(sys_transfers),
        ata_creations=tuple(ata_creations),
        spl_transfers=tuple(spl_transfers),
        uses_jupiter=uses_jupiter,
        priority_fee_lamports=priority,
        base_fee_lamports=base,
        programs=frozenset(programs),
    )


def sign_solana_message(
    message_bytes: bytes,
    plan: ValidatedTransactionPlan,
    policy: SolanaMessagePolicy,
    seed: bytes,
    at: datetime,
) -> SignedTransaction:
    """Verify then sign ``message_bytes`` with a keypair built from ``seed``."""
    decode_solana_message(message_bytes, plan, policy)
    if len(seed) != 32:
        raise SignerTamperingError("Solana private key must be 32 raw seed bytes")
    keypair = Keypair.from_seed(seed)
    if str(keypair.pubkey()) != plan.signer_address:
        raise SignerPolicyViolation("Keystore key does not match plan signer address")
    msg = from_bytes_versioned(message_bytes)
    signature: Signature = keypair.sign_message(message_bytes)
    if not signature.verify(keypair.pubkey(), message_bytes):
        raise SignerTamperingError("Produced signature failed verification")
    tx = VersionedTransaction.populate(msg, [signature])
    return SignedTransaction(
        plan_hash=plan.plan_hash,
        raw_transaction_hex=bytes(tx).hex(),
        tx_hash=str(signature),
        signed_at=at,
    )


def message_digest(message_bytes: bytes) -> str:
    return hashlib.sha256(message_bytes).hexdigest()
