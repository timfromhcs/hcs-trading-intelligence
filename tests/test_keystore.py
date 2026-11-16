"""Keystore KDF versioning, wallet roles, rotation and file-backed persistence."""

import ast
import json
import os
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from hcs_trader.domain.signing import KeystoreError
from hcs_trader.keystore import (
    KDF_PROFILE,
    FileKeystoreStore,
    create_keystore_record,
    decrypt_keystore_record,
    keystore_needs_upgrade,
    rotate_keystore_record,
)

_SECRET = bytes(range(32))
_ADDRESS = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
_PASS = "correct-horse-battery-staple"
PACKAGE = Path(__file__).resolve().parents[1] / "src" / "hcs_trader"


@pytest.fixture(scope="module")
def record() -> dict[str, Any]:
    return create_keystore_record("w-1", _ADDRESS, "eip155", _SECRET, _PASS)


def _legacy_record() -> dict[str, Any]:
    """A record exactly as written before KDF parameters were raised (N=2**14)."""
    salt, nonce = os.urandom(16), os.urandom(12)
    key = Scrypt(salt=salt, length=32, n=2**14, r=8, p=1).derive(_PASS.encode())
    aad = f"w-legacy:{_ADDRESS}:eip155".encode()
    return {
        "version": 1,
        "wallet_id": "w-legacy",
        "address": _ADDRESS,
        "chain_namespace": "eip155",
        "kdf": "scrypt",
        "kdf_params": {"n": 2**14, "r": 8, "p": 1, "length": 32},
        "salt_hex": salt.hex(),
        "nonce_hex": nonce.hex(),
        "ciphertext_hex": AESGCM(key).encrypt(nonce, _SECRET, aad).hex(),
    }


def test_new_records_use_strong_versioned_kdf_params(record: dict[str, Any]) -> None:
    assert record["version"] == 1
    assert record["kdf_profile"] == KDF_PROFILE == 2
    assert record["kdf_params"] == {"n": 2**17, "r": 8, "p": 1, "length": 32}
    assert record["wallet_role"] == "hot"
    assert not keystore_needs_upgrade(record)
    assert decrypt_keystore_record(record, _PASS) == _SECRET


def test_legacy_records_still_decrypt_and_are_flagged_for_upgrade() -> None:
    legacy = _legacy_record()
    assert keystore_needs_upgrade(legacy)
    assert decrypt_keystore_record(legacy, _PASS) == _SECRET
    upgraded = rotate_keystore_record(legacy, _PASS, "new-passphrase-value")
    assert not keystore_needs_upgrade(upgraded)
    assert upgraded["kdf_params"]["n"] == 2**17
    assert decrypt_keystore_record(upgraded, "new-passphrase-value") == _SECRET
    with pytest.raises(KeystoreError, match="invalid passphrase"):
        decrypt_keystore_record(upgraded, _PASS)


def test_rotation_preserves_identity_and_changes_salt(record: dict[str, Any]) -> None:
    rotated = rotate_keystore_record(record, _PASS, "another-passphrase")
    for field in ("wallet_id", "address", "chain_namespace"):
        assert rotated[field] == record[field]
    assert rotated["salt_hex"] != record["salt_hex"]
    assert rotated["nonce_hex"] != record["nonce_hex"]
    with pytest.raises(KeystoreError):
        rotate_keystore_record(record, "wrong", "another-passphrase")


@pytest.mark.parametrize(
    "params",
    [
        {"n": 2**13, "r": 8, "p": 1, "length": 32},  # below the floor
        {"n": 2**21, "r": 8, "p": 1, "length": 32},  # memory exhaustion
        {"n": 3 * 2**15, "r": 8, "p": 1, "length": 32},  # not a power of two
        {"n": 2**17, "r": 8, "p": 64, "length": 32},  # excessive parallelism
        {"n": 2**17, "r": 1, "p": 1, "length": 32},
        {"n": 2**17, "r": 8, "p": 1, "length": 16},
        {"n": True, "r": 8, "p": 1, "length": 32},
        {"n": "131072", "r": 8, "p": 1, "length": 32},
    ],
)
def test_hostile_kdf_parameters_are_rejected(
    record: dict[str, Any], params: dict[str, object]
) -> None:
    with pytest.raises(KeystoreError, match="Scrypt|Unrecognized"):
        decrypt_keystore_record({**record, "kdf_params": params}, _PASS)


def test_tampered_parameters_cannot_decrypt_even_if_in_range(
    record: dict[str, Any],
) -> None:
    tampered = {**record, "kdf_params": {**record["kdf_params"], "n": 2**18}}
    with pytest.raises(KeystoreError, match="invalid passphrase"):
        decrypt_keystore_record(tampered, _PASS)


def test_unsupported_version_kdf_and_missing_fields(record: dict[str, Any]) -> None:
    for bad in ({**record, "version": 3}, {**record, "kdf": "pbkdf2"}):
        with pytest.raises(KeystoreError, match="Unsupported"):
            decrypt_keystore_record(bad, _PASS)
    with pytest.raises(KeystoreError, match="Malformed"):
        decrypt_keystore_record({**record, "salt_hex": "zz"}, _PASS)
    with pytest.raises(KeystoreError, match="Malformed"):
        decrypt_keystore_record(
            {k: v for k, v in record.items() if k != "address"}, _PASS
        )
    with pytest.raises(KeystoreError, match="empty"):
        create_keystore_record("w", _ADDRESS, "eip155", _SECRET, "")
    with pytest.raises(KeystoreError, match="empty"):
        create_keystore_record("w", _ADDRESS, "eip155", b"", _PASS)


@pytest.mark.parametrize("role", ["cold", "watch_only", "other"])
def test_cold_and_watch_only_wallets_never_get_keystore_records(role: str) -> None:
    with pytest.raises(KeystoreError, match="may not hold signing material"):
        create_keystore_record(
            "w-cold",
            _ADDRESS,
            "eip155",
            _SECRET,
            _PASS,
            wallet_role=role,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("role", ["cold", "watch_only"])
def test_records_marked_non_hot_cannot_be_decrypted_or_stored(
    record: dict[str, Any], tmp_path: Path, role: str
) -> None:
    relabeled = {**record, "wallet_role": role}
    with pytest.raises(KeystoreError, match="not marked as a hot wallet"):
        decrypt_keystore_record(relabeled, _PASS)
    with pytest.raises(KeystoreError, match="Only hot wallet"):
        FileKeystoreStore(tmp_path).save(relabeled)


def test_file_store_roundtrip_is_atomic_and_refuses_overwrite(
    record: dict[str, Any], tmp_path: Path
) -> None:
    store = FileKeystoreStore(tmp_path / "keys")
    path = store.save(record)
    assert path.parent == tmp_path / "keys" and path.suffix == ".json"
    assert _ADDRESS not in path.name  # no caller text reaches the filename
    assert not list(path.parent.glob("*.tmp"))
    loaded = store.load("eip155", _ADDRESS)
    assert loaded == record
    assert decrypt_keystore_record(loaded, _PASS) == _SECRET
    assert store.list_wallets() == [("eip155", _ADDRESS)]
    with pytest.raises(KeystoreError, match="already exists"):
        store.save(record)
    replacement = rotate_keystore_record(record, _PASS, "rotated-passphrase")
    store.save(replacement, overwrite=True)
    assert store.load("eip155", _ADDRESS)["salt_hex"] == replacement["salt_hex"]
    assert path.read_bytes() and b"ciphertext_hex" in path.read_bytes()
    assert _SECRET.hex().encode() not in path.read_bytes()


def test_file_store_rejects_corrupt_oversized_and_swapped_files(
    record: dict[str, Any], tmp_path: Path
) -> None:
    store = FileKeystoreStore(tmp_path)
    with pytest.raises(KeystoreError, match="No keystore record"):
        store.load("eip155", _ADDRESS)
    path = store.save(record)

    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(KeystoreError, match="unreadable"):
        store.load("eip155", _ADDRESS)
    assert store.list_wallets() == []

    path.write_text(json.dumps({"version": 1}), encoding="utf-8")
    with pytest.raises(KeystoreError, match="missing fields"):
        store.load("eip155", _ADDRESS)

    path.write_bytes(b" " * 70_000)
    with pytest.raises(KeystoreError, match="too large"):
        store.load("eip155", _ADDRESS)

    # A record for another wallet copied under this wallet's filename is refused.
    other = create_keystore_record("w-2", "0x" + "11" * 20, "eip155", _SECRET, _PASS)
    path.write_text(json.dumps(other), encoding="utf-8")
    with pytest.raises(KeystoreError, match="does not match"):
        store.load("eip155", _ADDRESS)


def test_file_store_save_rejects_malformed_records(tmp_path: Path) -> None:
    store = FileKeystoreStore(tmp_path)
    with pytest.raises(KeystoreError):
        store.save({"version": 1})
    with pytest.raises(KeystoreError):
        store.save([])  # type: ignore[arg-type]
    assert list(tmp_path.iterdir()) == []


def test_only_signing_and_keystore_modules_touch_decryption() -> None:
    """Wallet adapters stay read-only: no module but signing.py decrypts."""
    for name in ("wallets.py", "wallet_addresses.py", "evm_swap.py", "jupiter_swap.py"):
        tree = ast.parse((PACKAGE / name).read_text("utf-8"))
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        } | {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        assert not {m for m in imported if m.endswith(("keystore", "signing"))}, name
