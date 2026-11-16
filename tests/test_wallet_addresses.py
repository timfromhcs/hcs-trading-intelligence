"""Offline address validation against published and generated vectors."""

import hashlib

import pytest

from hcs_trader.wallet_addresses import (
    has_offline_profile,
    validate_evm_address,
    validate_utxo_address,
)

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_HASH160 = bytes(range(20))


def _b58check(version: int, payload: bytes) -> str:
    body = bytes([version]) + payload
    raw = body + hashlib.sha256(hashlib.sha256(body).digest()).digest()[:4]
    number = int.from_bytes(raw, "big")
    out = ""
    while number:
        number, rem = divmod(number, 58)
        out = _B58[rem] + out
    return "1" * (len(raw) - len(raw.lstrip(b"\x00"))) + out


def _polymod(values: list[int]) -> int:
    generator = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    check = 1
    for value in values:
        top = check >> 25
        check = (check & 0x1FFFFFF) << 5 ^ value
        for i in range(5):
            check ^= generator[i] if (top >> i) & 1 else 0
    return check


def _to_5bit(data: bytes) -> list[int]:
    acc, bits, out = 0, 0, []
    for byte in data:
        acc = (acc << 8) | byte
        bits += 8
        while bits >= 5:
            bits -= 5
            out.append((acc >> bits) & 31)
    if bits:
        out.append((acc << (5 - bits)) & 31)
    return out


def _bech32(hrp: str, version: int, program: bytes) -> str:
    constant = 1 if version == 0 else 0x2BC830A3
    data = [version, *_to_5bit(program)]
    expanded = [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]
    mod = _polymod(expanded + data + [0] * 6) ^ constant
    checksum = [(mod >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(_CHARSET[d] for d in data + checksum)


def test_published_bitcoin_vectors() -> None:
    cases = {
        "1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2": "p2pkh",
        "3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy": "p2sh",
        "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4": "p2wpkh",
        "bc1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qccfmv3": "p2wsh",
        "bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqzk5jj0": "p2tr",
    }
    for address, kind in cases.items():
        assert validate_utxo_address("bitcoin", address).kind == kind
    upper = "BC1QW508D6QEJXTDG4Y5R3ZARVARY0C5XW7KV8F3T4"
    assert validate_utxo_address("bitcoin", upper).normalized == upper.lower()
    signet = "tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx"
    assert validate_utxo_address("bitcoin", signet, "signet").kind == "p2wpkh"


@pytest.mark.parametrize(
    "address",
    [
        "1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN3",  # base58 checksum
        "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t5",  # bech32 checksum
        "bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqh2y7hd",  # v1, bech32
        "bc1Qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",  # mixed case
        "tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx",  # testnet hrp on mainnet
        "bc1",  # too short
        "",
        " 1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2",
        "0OIl" * 8,
    ],
)
def test_invalid_bitcoin_addresses(address: str) -> None:
    with pytest.raises(ValueError):
        validate_utxo_address("bitcoin", address)


def test_litecoin_and_digibyte_prefixes_are_chain_specific() -> None:
    ltc = _b58check(0x30, _HASH160)
    ltc_p2sh = _b58check(0x32, _HASH160)
    ltc_legacy_p2sh = _b58check(0x05, _HASH160)
    dgb = _b58check(0x1E, _HASH160)
    dgb_p2sh = _b58check(0x3F, _HASH160)
    btc = _b58check(0x00, _HASH160)
    assert ltc.startswith("L") and dgb.startswith("D")
    assert validate_utxo_address("litecoin", ltc).kind == "p2pkh"
    assert validate_utxo_address("litecoin", ltc_p2sh).kind == "p2sh"
    assert validate_utxo_address("litecoin", ltc_legacy_p2sh).kind == "p2sh"
    assert validate_utxo_address("digibyte", dgb).kind == "p2pkh"
    assert validate_utxo_address("digibyte", dgb_p2sh).kind == "p2sh"
    # A Bitcoin address is not a Litecoin or DigiByte address, and vice versa.
    for chain, address in (
        ("litecoin", btc),
        ("digibyte", btc),
        ("litecoin", dgb),
        ("digibyte", ltc),
        ("bitcoin", ltc),
        ("bitcoin", dgb),
    ):
        with pytest.raises(ValueError, match="version byte"):
            validate_utxo_address(chain, address)


def test_litecoin_and_digibyte_bech32() -> None:
    program = _HASH160
    ltc = _bech32("ltc", 0, program)
    dgb = _bech32("dgb", 0, program)
    taproot = _bech32("dgb", 1, bytes(range(32)))
    assert validate_utxo_address("litecoin", ltc).kind == "p2wpkh"
    assert validate_utxo_address("digibyte", dgb).kind == "p2wpkh"
    assert validate_utxo_address("digibyte", taproot).kind == "p2tr"
    with pytest.raises(ValueError):
        validate_utxo_address("litecoin", dgb)
    with pytest.raises(ValueError):
        validate_utxo_address("bitcoin", ltc)
    with pytest.raises(ValueError):
        validate_utxo_address("digibyte", _bech32("dgb", 0, bytes(19)))  # bad length
    with pytest.raises(ValueError):
        validate_utxo_address("digibyte", _bech32("dgb", 2, bytes(20)))  # version 2


def test_unknown_profile_is_refused_not_guessed() -> None:
    assert has_offline_profile("bitcoin", "mainnet")
    assert not has_offline_profile("litecoin", "regtest")
    with pytest.raises(ValueError, match="No offline address profile"):
        validate_utxo_address("litecoin", _b58check(0x6F, _HASH160), "regtest")


def test_evm_address_validation_normalizes_to_lowercase() -> None:
    info = validate_evm_address("0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed")
    assert info.normalized == "0x5aaeb6053f3e94c9b9a09f33669435e7ef1beaed"
    with pytest.raises(ValueError, match="EIP-55"):
        validate_evm_address("0x5AAeb6053F3E94C9b9A09f33669435E7Ef1BeAed")
