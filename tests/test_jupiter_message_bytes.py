"""Byte-level Solana message decoding and inspection with deterministic vectors."""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from hcs_trader.jupiter_swap import (
    ASSOCIATED_TOKEN_PROGRAM_ID,
    COMPUTE_BUDGET_PROGRAM_ID,
    JUPITER_V6_PROGRAM_ID,
    SPL_TOKEN_PROGRAM_ID,
    SYSTEM_PROGRAM_ID,
    JupiterRouteError,
    JupiterSwapAdapter,
    base58_decode,
    base58_encode,
    decode_solana_message,
    decode_solana_transaction,
)

_SIGNER = base58_encode(bytes(range(1, 33)))
_OTHER = base58_encode(bytes(range(40, 72)))
_THIRD = base58_encode(bytes(range(100, 132)))
_BLOCKHASH = bytes(range(200, 232))
_BAD_PROGRAM = base58_encode(bytes(range(7, 39)))


def _shortvec(value: int) -> bytes:
    out = bytearray()
    while True:
        low = value & 0x7F
        value >>= 7
        if value:
            out.append(low | 0x80)
        else:
            out.append(low)
            return bytes(out)


def _message(
    instructions: list[tuple[str, list[str], bytes]],
    *,
    signer: str = _SIGNER,
    signers: int = 1,
    version0: bool = False,
    lookups: bytes = b"\x00",
    extra_accounts: tuple[str, ...] = (_OTHER, _THIRD),
) -> bytes:
    programs = []
    for program, _, _ in instructions:
        if program not in programs:
            programs.append(program)
    keys = [signer, *extra_accounts[: max(signers - 1, 0)]]
    keys += [a for a in extra_accounts if a not in keys]
    for _, accounts, _ in instructions:
        keys += [a for a in accounts if a not in keys and a not in programs]
    keys += [p for p in programs if p not in keys]
    out = bytearray(b"\x80" if version0 else b"")
    out += bytes([signers, 0, len(programs)])
    out += _shortvec(len(keys))
    for key in keys:
        out += base58_decode(key)
    out += _BLOCKHASH
    out += _shortvec(len(instructions))
    for program, accounts, data in instructions:
        out.append(keys.index(program))
        out += _shortvec(len(accounts))
        out += bytes(keys.index(a) for a in accounts)
        out += _shortvec(len(data))
        out += data
    if version0:
        out += lookups
    return bytes(out)


def _limit(units: int) -> tuple[str, list[str], bytes]:
    return COMPUTE_BUDGET_PROGRAM_ID, [], b"\x02" + units.to_bytes(4, "little")


def _price(micro: int) -> tuple[str, list[str], bytes]:
    return COMPUTE_BUDGET_PROGRAM_ID, [], b"\x03" + micro.to_bytes(8, "little")


def _swap() -> tuple[str, list[str], bytes]:
    return JUPITER_V6_PROGRAM_ID, [_SIGNER, _OTHER], b"\xe5\x17\xcb" + b"\x00" * 20


def _good() -> list[tuple[str, list[str], bytes]]:
    return [_limit(300_000), _price(1_000), _swap()]


def test_base58_vectors_and_roundtrip() -> None:
    assert base58_decode(SYSTEM_PROGRAM_ID) == bytes(32)
    assert base58_encode(bytes(32)) == SYSTEM_PROGRAM_ID
    for program in (
        JUPITER_V6_PROGRAM_ID,
        SPL_TOKEN_PROGRAM_ID,
        COMPUTE_BUDGET_PROGRAM_ID,
    ):
        raw = base58_decode(program)
        assert len(raw) == 32
        assert base58_encode(raw) == program
    with pytest.raises(JupiterRouteError):
        base58_decode("0OIl")


def test_valid_legacy_and_v0_messages_decode_and_pass() -> None:
    adapter = JupiterSwapAdapter()
    for version0 in (False, True):
        raw = _message(_good(), version0=version0)
        decoded = adapter.inspect_message_bytes(raw, signer=_SIGNER)
        assert decoded.version == ("v0" if version0 else "legacy")
        assert decoded.account_keys[0] == _SIGNER
        assert decoded.recent_blockhash == base58_encode(_BLOCKHASH)
        assert [i.program_id for i in decoded.instructions][-1] == JUPITER_V6_PROGRAM_ID


def test_full_transaction_signature_count_must_match_header() -> None:
    adapter = JupiterSwapAdapter()
    message = _message(_good())
    tx = b"\x01" + bytes(64) + message
    count, decoded = decode_solana_transaction(tx)
    assert count == 1 and decoded.required_signatures == 1
    adapter.inspect_transaction_bytes(tx, signer=_SIGNER)
    with pytest.raises(JupiterRouteError, match="Signature count"):
        decode_solana_transaction(b"\x02" + bytes(128) + message)


def test_rejects_unapproved_program_and_missing_jupiter() -> None:
    adapter = JupiterSwapAdapter()
    with pytest.raises(JupiterRouteError, match="unapproved program"):
        adapter.inspect_message_bytes(
            _message([*_good(), (_BAD_PROGRAM, [_SIGNER], b"\x01")]), signer=_SIGNER
        )
    with pytest.raises(JupiterRouteError, match="does not invoke the Jupiter"):
        adapter.inspect_message_bytes(_message([_limit(1)]), signer=_SIGNER)
    with pytest.raises(JupiterRouteError, match="omits the signer"):
        adapter.inspect_message_bytes(
            _message([(JUPITER_V6_PROGRAM_ID, [_OTHER], b"\x01")]), signer=_SIGNER
        )


def test_signer_must_be_sole_fee_payer() -> None:
    adapter = JupiterSwapAdapter()
    with pytest.raises(JupiterRouteError, match="fee payer"):
        adapter.inspect_message_bytes(_message(_good()), signer=_OTHER)
    two_signers = _message(_good(), signers=2)
    with pytest.raises(JupiterRouteError, match="only required signature"):
        adapter.inspect_message_bytes(two_signers, signer=_SIGNER)


@pytest.mark.parametrize(
    ("instruction", "error"),
    [
        (_price(10_000_000), "Priority fee"),
        (
            (COMPUTE_BUDGET_PROGRAM_ID, [], b"\x01" + bytes(8)),
            "only SetComputeUnitLimit",
        ),
        (
            (
                COMPUTE_BUDGET_PROGRAM_ID,
                [],
                b"\x02" + (2_000_000).to_bytes(4, "little"),
            ),
            "compute unit limit",
        ),
        (
            (SPL_TOKEN_PROGRAM_ID, [_OTHER, _THIRD, _SIGNER], b"\x03" + bytes(8)),
            "not allowed",
        ),
        (
            (SPL_TOKEN_PROGRAM_ID, [_OTHER, _THIRD, _SIGNER], b"\x06" + bytes(34)),
            "not allowed",
        ),
        (
            (SPL_TOKEN_PROGRAM_ID, [_OTHER, _THIRD, _SIGNER], b"\x09"),
            "CloseAccount destination",
        ),
        (
            (SYSTEM_PROGRAM_ID, [_SIGNER, _OTHER], b"\x02\x00\x00\x00" + bytes(8)),
            None,
        ),
        (
            (
                SYSTEM_PROGRAM_ID,
                [_SIGNER, _OTHER],
                b"\x02\x00\x00\x00" + (10**9).to_bytes(8, "little"),
            ),
            "exceeds the permitted",
        ),
        (
            (
                SYSTEM_PROGRAM_ID,
                [_OTHER, _SIGNER],
                b"\x02\x00\x00\x00" + (1).to_bytes(8, "little"),
            ),
            "source must be the signer",
        ),
        (
            (SYSTEM_PROGRAM_ID, [_SIGNER, _OTHER], b"\x01\x00\x00\x00" + bytes(8)),
            "only system Transfer",
        ),
    ],
)
def test_per_program_instruction_rules(
    instruction: tuple[str, list[str], bytes], error: str | None
) -> None:
    adapter = JupiterSwapAdapter()
    raw = _message([*_good(), instruction])
    if error is None:  # a zero-lamport transfer from the signer is harmless
        adapter.inspect_message_bytes(raw, signer=_SIGNER)
        return
    with pytest.raises(JupiterRouteError, match=error):
        adapter.inspect_message_bytes(raw, signer=_SIGNER)


def test_swap_lamports_bound_and_sync_native_allowed() -> None:
    adapter = JupiterSwapAdapter()
    transfer = (
        SYSTEM_PROGRAM_ID,
        [_SIGNER, _OTHER],
        b"\x02\x00\x00\x00" + (10**9).to_bytes(8, "little"),
    )
    sync = (SPL_TOKEN_PROGRAM_ID, [_OTHER], b"\x11")
    raw = _message([transfer, sync, *_good()])
    adapter.inspect_message_bytes(raw, signer=_SIGNER, max_swap_lamports=10**9)
    with pytest.raises(JupiterRouteError, match="exceeds the permitted"):
        adapter.inspect_message_bytes(raw, signer=_SIGNER, max_swap_lamports=10**9 - 1)


def test_ata_instruction_still_requires_signer_checks_from_bytes() -> None:
    adapter = JupiterSwapAdapter()
    ata = (
        ASSOCIATED_TOKEN_PROGRAM_ID,
        [_SIGNER, _OTHER, _SIGNER, "So11111111111111111111111111111111111111112",
         SYSTEM_PROGRAM_ID, SPL_TOKEN_PROGRAM_ID],
        b"\x01",
    )  # fmt: skip
    adapter.inspect_message_bytes(_message([ata, *_good()]), signer=_SIGNER)
    bad = (ata[0], [_OTHER, *ata[1][1:]], b"\x01")
    with pytest.raises(JupiterRouteError, match="payer must be the signer"):
        adapter.inspect_message_bytes(_message([bad, *_good()]), signer=_SIGNER)


def test_structural_rejections() -> None:
    good = _message(_good())
    with pytest.raises(JupiterRouteError, match="Address table lookups"):
        decode_solana_message(
            _message(_good(), version0=True, lookups=b"\x01" + bytes(34))
        )
    with pytest.raises(JupiterRouteError, match="Trailing"):
        decode_solana_message(good + b"\x00")
    with pytest.raises(JupiterRouteError, match="Unsupported message version"):
        decode_solana_message(b"\x81" + good)
    with pytest.raises(JupiterRouteError):
        decode_solana_message(b"")
    with pytest.raises(JupiterRouteError, match="packet limit"):
        decode_solana_message(good + bytes(2000))
    for cut in range(1, len(good), 17):
        with pytest.raises(JupiterRouteError):
            decode_solana_message(good[:cut])
    # A zero required-signature header is inconsistent.
    header_less = bytearray(_message([_swap()]))
    header_less[0] = 0  # zero required signatures
    with pytest.raises(JupiterRouteError, match="header"):
        decode_solana_message(bytes(header_less))


@given(st.binary(max_size=400))
@settings(max_examples=300, deadline=None)
def test_decoder_fuzz_only_raises_route_errors(blob: bytes) -> None:
    for candidate in (blob, b"\x80" + blob, b"\x01" + bytes(64) + blob):
        try:
            JupiterSwapAdapter().inspect_transaction_bytes(candidate, signer=_SIGNER)
        except JupiterRouteError:
            pass
