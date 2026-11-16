"""Offline address validation: base58check, bech32/bech32m and EIP-55.

These checks prove an address is well-formed for a named network (checksum and
version/prefix). They never derive keys and never consult a node.
"""

import hashlib
from dataclasses import dataclass
from typing import Literal

from hcs_trader.evm_swap import is_valid_eth_address

AddressKind = Literal["p2pkh", "p2sh", "p2wpkh", "p2wsh", "p2tr", "evm"]

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32_CONST = 1
_BECH32M_CONST = 0x2BC830A3
_MAX_ADDRESS_LENGTH = 90


@dataclass(frozen=True, slots=True)
class _AddressProfile:
    p2pkh: tuple[int, ...]
    p2sh: tuple[int, ...]
    hrp: str


# Version bytes / human-readable parts. Regtest and testnets are listed only
# where the prefixes are certain; the node's validateaddress covers the rest.
_PROFILES: dict[tuple[str, str], _AddressProfile] = {
    ("bitcoin", "mainnet"): _AddressProfile((0x00,), (0x05,), "bc"),
    ("bitcoin", "signet"): _AddressProfile((0x6F,), (0xC4,), "tb"),
    ("bitcoin", "regtest"): _AddressProfile((0x6F,), (0xC4,), "bcrt"),
    # Litecoin also still accepts the legacy 0x05 P2SH prefix.
    ("litecoin", "mainnet"): _AddressProfile((0x30,), (0x32, 0x05), "ltc"),
    ("digibyte", "mainnet"): _AddressProfile((0x1E,), (0x3F,), "dgb"),
}


@dataclass(frozen=True, slots=True)
class AddressInfo:
    chain: str
    network: str
    kind: AddressKind
    normalized: str


def _b58decode(text: str) -> bytes:
    number = 0
    for char in text:
        index = _B58.find(char)
        if index < 0:
            raise ValueError("Invalid base58 character")
        number = number * 58 + index
    leading = len(text) - len(text.lstrip("1"))
    return b"\x00" * leading + number.to_bytes((number.bit_length() + 7) // 8, "big")


def _b58check_decode(text: str) -> tuple[int, bytes]:
    raw = _b58decode(text)
    if len(raw) != 25:
        raise ValueError("Base58Check address must decode to 25 bytes")
    payload, checksum = raw[:-4], raw[-4:]
    expected = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    if checksum != expected:
        raise ValueError("Base58Check checksum mismatch")
    return payload[0], payload[1:]


def _bech32_polymod(values: list[int]) -> int:
    generator = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    check = 1
    for value in values:
        top = check >> 25
        check = (check & 0x1FFFFFF) << 5 ^ value
        for i in range(5):
            check ^= generator[i] if (top >> i) & 1 else 0
    return check


def _bech32_hrp_expand(hrp: str) -> list[int]:
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def _bech32_decode(text: str) -> tuple[str, list[int], int]:
    if text != text.lower() and text != text.upper():
        raise ValueError("Mixed-case bech32")
    text = text.lower()
    if len(text) > _MAX_ADDRESS_LENGTH or "1" not in text:
        raise ValueError("Invalid bech32 length or separator")
    position = text.rindex("1")
    hrp, data_part = text[:position], text[position + 1 :]
    if not hrp or len(data_part) < 6:
        raise ValueError("Invalid bech32 structure")
    if any(ord(c) < 33 or ord(c) > 126 for c in hrp):
        raise ValueError("Invalid bech32 human-readable part")
    try:
        data = [_BECH32_CHARSET.index(c) for c in data_part]
    except ValueError:
        raise ValueError("Invalid bech32 character") from None
    constant = _bech32_polymod(_bech32_hrp_expand(hrp) + data)
    if constant not in (_BECH32_CONST, _BECH32M_CONST):
        raise ValueError("Bech32 checksum mismatch")
    return hrp, data[:-6], constant


def _convert_bits(data: list[int], from_bits: int, to_bits: int) -> list[int]:
    accumulator = 0
    bits = 0
    out: list[int] = []
    for value in data:
        if value >> from_bits:
            raise ValueError("Invalid group value")
        accumulator = (accumulator << from_bits) | value
        bits += from_bits
        while bits >= to_bits:
            bits -= to_bits
            out.append((accumulator >> bits) & ((1 << to_bits) - 1))
    if bits >= from_bits or (accumulator << (to_bits - bits)) & ((1 << to_bits) - 1):
        raise ValueError("Invalid padding")
    return out


def validate_utxo_address(
    chain: str, address: str, network: str = "mainnet"
) -> AddressInfo:
    """Validate a BTC/LTC/DGB address for a network, or raise ``ValueError``."""
    profile = _PROFILES.get((chain, network))
    if profile is None:
        raise ValueError(f"No offline address profile for {chain} {network}")
    if not address or len(address) > _MAX_ADDRESS_LENGTH or address != address.strip():
        raise ValueError("Address is empty, too long or has whitespace")
    lowered = address.lower()
    if lowered.startswith(profile.hrp + "1"):
        hrp, data, constant = _bech32_decode(address)
        if hrp != profile.hrp or not data:
            raise ValueError("Bech32 human-readable part does not match the network")
        version = data[0]
        program = bytes(_convert_bits(data[1:], 5, 8))
        if version == 0:
            if constant != _BECH32_CONST or len(program) not in (20, 32):
                raise ValueError("Invalid witness v0 program")
            kind: AddressKind = "p2wpkh" if len(program) == 20 else "p2wsh"
        elif version == 1:
            if constant != _BECH32M_CONST or len(program) != 32:
                raise ValueError("Invalid taproot program")
            kind = "p2tr"
        else:
            raise ValueError(f"Unsupported witness version {version}")
        return AddressInfo(chain, network, kind, lowered)
    prefix, payload = _b58check_decode(address)
    if len(payload) != 20:
        raise ValueError("Base58Check payload must be 20 bytes")
    if prefix in profile.p2pkh:
        return AddressInfo(chain, network, "p2pkh", address)
    if prefix in profile.p2sh:
        return AddressInfo(chain, network, "p2sh", address)
    raise ValueError(f"Address version byte 0x{prefix:02x} is not valid for {network}")


def validate_evm_address(address: str) -> AddressInfo:
    """Validate an Ethereum address (EIP-55 checksum enforced when mixed-case)."""
    if not is_valid_eth_address(address):
        raise ValueError("Invalid Ethereum address or EIP-55 checksum")
    return AddressInfo("ethereum", "any", "evm", address.lower())


def has_offline_profile(chain: str, network: str) -> bool:
    return (chain, network) in _PROFILES
