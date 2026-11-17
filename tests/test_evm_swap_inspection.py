"""Versioned allowlists, EIP-55/keccak vectors and real EIP-1559 byte inspection."""

import time

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from hcs_trader.evm_swap import (
    EVM_ALLOWLIST_VERSION,
    SELECTOR_APPROVE,
    SELECTOR_BALANCE_OF,
    SELECTOR_DECIMALS,
    SELECTOR_UR_EXECUTE,
    EVMCalldataTamperingError,
    EVMSwapAdapter,
    EVMSwapError,
    allowlist_for_chain,
    decode_universal_router_execute,
    is_valid_eth_address,
    keccak256,
    rlp_decode,
    to_checksum_address,
)

_ROUTER = "0x3fC91A3afd70395Cd496C647d5a6CC9D4B2b7FAD"
_SUSPICIOUS_ROUTER = "0x000000000000000000000000000000000000dEaD"
_SUSPICIOUS_TOKEN = "0x1111111111111111111111111111111111111111"
_SEPOLIA_USDC = "0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238"
_MAINNET_USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
_SIGNER = "0x" + "ab" * 20


# --- W10: chain-keyed, versioned allowlists ----------------------------------------


def test_allowlist_is_versioned_and_chain_keyed() -> None:
    mainnet = allowlist_for_chain(1)
    sepolia = allowlist_for_chain(11155111)
    assert mainnet.version == sepolia.version == EVM_ALLOWLIST_VERSION
    assert mainnet.token_addresses.isdisjoint(sepolia.token_addresses)
    usdc = mainnet.token(_MAINNET_USDC)
    assert usdc is not None and usdc.decimals == 6
    assert EVMSwapAdapter(chain_id=1).allowlist_version == EVM_ALLOWLIST_VERSION


def test_sepolia_token_rejected_on_mainnet_and_vice_versa() -> None:
    mainnet = EVMSwapAdapter(chain_id=1)
    sepolia = EVMSwapAdapter(chain_id=11155111)
    sepolia.validate_token(_SEPOLIA_USDC)
    mainnet.validate_token(_MAINNET_USDC)
    with pytest.raises(EVMSwapError, match="not on the audited allowlist"):
        mainnet.validate_token(_SEPOLIA_USDC)
    with pytest.raises(EVMSwapError, match="not on the audited allowlist"):
        sepolia.validate_token(_MAINNET_USDC)


def test_unknown_chain_and_unpinned_sepolia_router_fail_closed() -> None:
    for chain_id in (137, 11155111):
        with pytest.raises(EVMSwapError, match="not on the audited allowlist"):
            EVMSwapAdapter(chain_id=chain_id).validate_router(_ROUTER)
    assert allowlist_for_chain(137).tokens == ()


def test_explicit_allowlist_override_replaces_defaults() -> None:
    adapter = EVMSwapAdapter(chain_id=1, allowed_routers=frozenset())
    with pytest.raises(EVMSwapError):
        adapter.validate_router(_ROUTER)


def test_router_code_hash_pin_fails_closed_unless_matching() -> None:
    with pytest.raises(EVMSwapError, match="No code hash is pinned"):
        EVMSwapAdapter(chain_id=1).validate_router_code_hash(_ROUTER, "0x" + "11" * 32)
    pinned = EVMSwapAdapter(
        chain_id=1, router_code_hashes=((_ROUTER.lower(), "0x" + "11" * 32),)
    )
    pinned.validate_router_code_hash(_ROUTER, "0x" + "11" * 32)
    with pytest.raises(EVMSwapError, match="does not match the pinned hash"):
        pinned.validate_router_code_hash(_ROUTER, "0x" + "22" * 32)


# --- Keccak / EIP-55 / RLP external vectors ----------------------------------------


def test_keccak256_known_vectors() -> None:
    assert (
        keccak256(b"").hex()
        == "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
    )
    assert keccak256(b"approve(address,uint256)")[:4] == SELECTOR_APPROVE
    assert keccak256(b"balanceOf(address)")[:4] == SELECTOR_BALANCE_OF
    assert keccak256(b"decimals()")[:4] == SELECTOR_DECIMALS
    assert keccak256(b"execute(bytes,bytes[],uint256)")[:4] == SELECTOR_UR_EXECUTE
    assert len(keccak256(b"a" * 300)) == 32  # multi-block input


@pytest.mark.parametrize(
    "address",
    [
        "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed",
        "0xfB6916095ca1df60bB79Ce92cE3Ea74c37c5d359",
        "0xdbF03B407c01E7cD3CBea99509d93f8DDDC8C6FB",
        "0xD1220A0cf47c7B9Be7A2E6BA89F429762e7b9aDb",
    ],
)
def test_eip55_vectors(address: str) -> None:
    assert to_checksum_address(address.lower()) == address
    assert is_valid_eth_address(address)
    assert is_valid_eth_address(address.lower())
    # Swap the case of one letter: the mixed-case checksum must now fail.
    index = next(i for i, c in enumerate(address) if i > 1 and c.isalpha())
    flipped = address[:index] + address[index].swapcase() + address[index + 1 :]
    assert not is_valid_eth_address(flipped)


def test_eth_address_format_rejections() -> None:
    assert not is_valid_eth_address("0x1234")
    assert not is_valid_eth_address("5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed")
    with pytest.raises(EVMSwapError):
        to_checksum_address("0xnothex")


def test_rlp_decode_canonical_vectors() -> None:
    assert rlp_decode(bytes.fromhex("83646f67")) == b"dog"
    assert rlp_decode(bytes.fromhex("c88363617483646f67")) == [b"cat", b"dog"]
    assert rlp_decode(bytes.fromhex("820400")) == b"\x04\x00"
    assert rlp_decode(b"\xc0") == []
    text = b"Lorem ipsum dolor sit amet, consectetur adipisicing elit"
    assert rlp_decode(bytes.fromhex("b838") + text) == text


@pytest.mark.parametrize(
    "bad",
    [
        "8100",  # single byte below 0x80 must not be length-prefixed
        "b80100",  # long form for a short string
        "83646f",  # truncated
        "83646f6700",  # trailing byte
        "c883636174",  # list overruns input
    ],
)
def test_rlp_rejects_non_canonical_or_truncated(bad: str) -> None:
    with pytest.raises(EVMCalldataTamperingError):
        rlp_decode(bytes.fromhex(bad))


# --- W3: real transaction-byte decoding --------------------------------------------


def _rlp_len(length: int, offset: int) -> bytes:
    if length < 56:
        return bytes([offset + length])
    encoded = length.to_bytes((length.bit_length() + 7) // 8, "big")
    return bytes([offset + 55 + len(encoded)]) + encoded


def _rlp(item: object) -> bytes:
    if isinstance(item, int):
        item = item.to_bytes((item.bit_length() + 7) // 8, "big") if item else b""
    if isinstance(item, bytes):
        if len(item) == 1 and item[0] < 0x80:
            return item
        return _rlp_len(len(item), 0x80) + item
    assert isinstance(item, list)
    body = b"".join(_rlp(i) for i in item)
    return _rlp_len(len(body), 0xC0) + body


def _tx(
    *,
    chain_id: int = 1,
    to: str = _ROUTER,
    data: bytes = b"",
    value: int = 0,
    gas: int = 200_000,
    max_fee: int = 20 * 10**9,
    priority: int = 10**9,
    access_list: list[object] | None = None,
    signed: bool = True,
) -> bytes:
    fields: list[object] = [
        chain_id,
        7,
        priority,
        max_fee,
        gas,
        bytes.fromhex(to[2:]),
        value,
        data,
        access_list or [],
    ]
    if signed:
        fields += [1, 2**200, 2**201]
    return b"\x02" + _rlp(fields)


def _word(value: int) -> bytes:
    return value.to_bytes(32, "big")


def _ur_execute(
    *,
    recipient: str = _SIGNER,
    deadline: int,
    min_out: int = 1_000,
    commands: bytes = b"\x00",
) -> bytes:
    swap_input = (
        _word(int(recipient, 16))
        + _word(10**18)
        + _word(min_out)
        + _word(160)
        + _word(1)
        + _word(0)
    )
    inputs = [swap_input] * len(commands)
    cmd_block = _word(len(commands)) + commands.ljust(32, b"\x00")
    elements = b""
    offsets = []
    cursor = 32 * len(inputs)
    for payload in inputs:
        offsets.append(_word(cursor))
        elements += _word(len(payload)) + payload
        cursor += 32 + len(payload)
    array_block = _word(len(inputs)) + b"".join(offsets) + elements
    head = _word(96) + _word(96 + len(cmd_block)) + _word(deadline)
    return SELECTOR_UR_EXECUTE + head + cmd_block + array_block


def test_inspect_transaction_accepts_valid_swap_and_decodes_fields() -> None:
    now = int(time.time())
    adapter = EVMSwapAdapter(chain_id=1)
    data = _ur_execute(deadline=now + 300)
    tx = adapter.inspect_transaction(
        _tx(data=data),
        current_timestamp=now,
        expected_signer=_SIGNER,
        min_amount_out=1_000,
    )
    assert tx.chain_id == 1
    assert tx.to == _ROUTER.lower()
    assert tx.signed and tx.gas_limit == 200_000
    unsigned = adapter.inspect_transaction(
        _tx(data=data, signed=False), current_timestamp=now, expected_signer=_SIGNER
    )
    assert not unsigned.signed


def test_inspect_transaction_exact_approval_and_tampering() -> None:
    now = int(time.time())
    adapter = EVMSwapAdapter(chain_id=1)
    approve = adapter.build_exact_approval_calldata(_ROUTER, 5_000)
    adapter.inspect_transaction(
        _tx(to=_MAINNET_USDC, data=approve),
        current_timestamp=now,
        expected_signer=_SIGNER,
        expected_approval=(_ROUTER, 5_000),
    )
    with pytest.raises(EVMCalldataTamperingError):
        adapter.inspect_transaction(
            _tx(to=_MAINNET_USDC, data=approve),
            current_timestamp=now,
            expected_signer=_SIGNER,
            expected_approval=(_ROUTER, 4_999),
        )
    with pytest.raises(EVMSwapError):  # approving an unlisted token contract
        adapter.inspect_transaction(
            _tx(to=_SUSPICIOUS_TOKEN, data=approve),
            current_timestamp=now,
            expected_signer=_SIGNER,
            expected_approval=(_ROUTER, 5_000),
        )
    with pytest.raises(EVMCalldataTamperingError, match="must not carry value"):
        adapter.inspect_transaction(
            _tx(to=_MAINNET_USDC, data=approve, value=1),
            current_timestamp=now,
            expected_signer=_SIGNER,
            expected_approval=(_ROUTER, 5_000),
            expected_value=1,
        )


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"chain_id": 11155111}, "Wrong chain ID"),
        ({"to": _SUSPICIOUS_ROUTER}, "not on the audited allowlist"),
        ({"gas": 900_000}, "Gas limit"),
        ({"max_fee": 500 * 10**9}, "Max fee per gas"),
        ({"max_fee": 0, "priority": 0}, "Max fee per gas"),
        ({"max_fee": 10**9, "priority": 2 * 10**9}, "Priority fee exceeds"),
        ({"value": 5}, "does not match expected"),
        ({"access_list": [[b"\x01" * 20, []]]}, "Access lists"),
    ],
)
def test_inspect_transaction_rejects_bad_envelopes(
    overrides: dict[str, object], error: str
) -> None:
    now = int(time.time())
    kwargs: dict[str, object] = {"data": _ur_execute(deadline=now + 300), **overrides}
    with pytest.raises(EVMSwapError, match=error):
        EVMSwapAdapter(chain_id=1).inspect_transaction(
            _tx(**kwargs),  # type: ignore[arg-type]
            current_timestamp=now,
            expected_signer=_SIGNER,
        )


def test_inspect_transaction_rejects_wrong_type_creation_and_garbage() -> None:
    adapter = EVMSwapAdapter(chain_id=1)
    now = int(time.time())
    with pytest.raises(EVMCalldataTamperingError, match="only EIP-1559"):
        adapter.inspect_transaction(
            b"\x01" + _tx()[1:], current_timestamp=now, expected_signer=_SIGNER
        )
    creation = b"\x02" + _rlp([1, 7, 1, 2, 3, b"", 0, b"", []])
    with pytest.raises(EVMCalldataTamperingError, match="contract creation"):
        adapter.inspect_transaction(
            creation, current_timestamp=now, expected_signer=_SIGNER
        )
    for garbage in (b"", b"\x02", b"\x02\xc0", b"\x02" + b"\xff" * 40):
        with pytest.raises(EVMCalldataTamperingError):
            adapter.inspect_transaction(
                garbage, current_timestamp=now, expected_signer=_SIGNER
            )


def test_universal_router_decode_checks_recipient_commands_deadline_minout() -> None:
    now = int(time.time())
    adapter = EVMSwapAdapter(chain_id=1)
    decoded = adapter.inspect_universal_router_execute(
        _ur_execute(deadline=now + 60),
        current_timestamp=now,
        expected_recipient=_SIGNER,
        min_amount_out=1_000,
    )
    assert decoded.commands == b"\x00" and decoded.deadline == now + 60

    with pytest.raises(EVMCalldataTamperingError, match="unexpected recipient"):
        adapter.inspect_universal_router_execute(
            _ur_execute(recipient="0x" + "cd" * 20, deadline=now + 60),
            current_timestamp=now,
            expected_recipient=_SIGNER,
        )
    with pytest.raises(EVMCalldataTamperingError, match="less than"):
        adapter.inspect_universal_router_execute(
            _ur_execute(min_out=999, deadline=now + 60),
            current_timestamp=now,
            expected_recipient=_SIGNER,
            min_amount_out=1_000,
        )
    with pytest.raises(EVMCalldataTamperingError, match="not allowlisted"):
        adapter.inspect_universal_router_execute(
            _ur_execute(commands=b"\x05", deadline=now + 60),  # TRANSFER
            current_timestamp=now,
            expected_recipient=_SIGNER,
        )
    with pytest.raises(EVMSwapError, match="expired"):
        adapter.inspect_universal_router_execute(
            _ur_execute(deadline=now - 1),
            current_timestamp=now,
            expected_recipient=_SIGNER,
        )
    adapter.inspect_universal_router_execute(  # router-internal sentinel recipient
        _ur_execute(recipient="0x" + "00" * 19 + "01", deadline=now + 60),
        current_timestamp=now,
        expected_recipient=_SIGNER,
    )


def test_universal_router_decode_rejects_truncation_and_hostile_offsets() -> None:
    now = int(time.time())
    good = _ur_execute(deadline=now + 60)
    adapter = EVMSwapAdapter(chain_id=1)
    for cut in (10, 60, 100, len(good) - 40):
        with pytest.raises(EVMCalldataTamperingError):
            adapter.inspect_universal_router_execute(
                good[:cut], current_timestamp=now, expected_recipient=_SIGNER
            )
    hostile = bytearray(good)
    hostile[4 + 32 : 4 + 64] = (2**255).to_bytes(32, "big")  # inputs offset
    with pytest.raises(EVMCalldataTamperingError):
        adapter.inspect_universal_router_execute(
            bytes(hostile), current_timestamp=now, expected_recipient=_SIGNER
        )
    with pytest.raises(EVMCalldataTamperingError, match="Unexpected selector"):
        decode_universal_router_execute(b"\xde\xad\xbe\xef" + good[4:])


@given(st.binary(max_size=300))
@settings(max_examples=200, deadline=None)
def test_transaction_decoder_only_raises_domain_errors(blob: bytes) -> None:
    for candidate in (blob, b"\x02" + blob):
        try:
            EVMSwapAdapter(chain_id=1).inspect_transaction(
                candidate, current_timestamp=0, expected_signer=_SIGNER
            )
        except EVMSwapError:
            pass


@given(st.binary(max_size=400))
@settings(max_examples=200, deadline=None)
def test_router_decoder_fuzz_only_raises_domain_errors(blob: bytes) -> None:
    try:
        decode_universal_router_execute(SELECTOR_UR_EXECUTE + blob)
    except EVMSwapError:
        pass
