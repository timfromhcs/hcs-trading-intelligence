"""Encrypted software keystore using memory-hard KDF and authenticated encryption."""

import os
from datetime import UTC, datetime
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from hcs_trader.domain.signing import KeystoreError

_SCRYPT_N = 16384
_SCRYPT_R = 8
_SCRYPT_P = 1
_KEY_LEN = 32
_SALT_BYTES = 16
_NONCE_BYTES = 12


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    if not passphrase:
        raise KeystoreError("Passphrase cannot be empty")
    kdf = Scrypt(
        salt=salt,
        length=_KEY_LEN,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
    )
    return kdf.derive(passphrase.encode("utf-8"))


def create_keystore_record(
    wallet_id: str,
    address: str,
    chain_namespace: str,
    secret_bytes: bytes,
    passphrase: str,
) -> dict[str, Any]:
    """Encrypt private key material using Scrypt KDF and AES-256-GCM."""
    if not secret_bytes:
        raise KeystoreError("Secret material cannot be empty")
    salt = os.urandom(_SALT_BYTES)
    derived_key = _derive_key(passphrase, salt)
    nonce = os.urandom(_NONCE_BYTES)
    aesgcm = AESGCM(derived_key)
    # Associated data binds wallet_id and address to prevent cut-and-paste substitution
    associated_data = f"{wallet_id}:{address}:{chain_namespace}".encode()
    ciphertext = aesgcm.encrypt(nonce, secret_bytes, associated_data)

    return {
        "version": 1,
        "wallet_id": wallet_id,
        "address": address,
        "chain_namespace": chain_namespace,
        "kdf": "scrypt",
        "kdf_params": {
            "n": _SCRYPT_N,
            "r": _SCRYPT_R,
            "p": _SCRYPT_P,
            "length": _KEY_LEN,
        },
        "salt_hex": salt.hex(),
        "nonce_hex": nonce.hex(),
        "ciphertext_hex": ciphertext.hex(),
        "created_at": datetime.now(UTC).isoformat(),
    }


def decrypt_keystore_record(record: dict[str, Any], passphrase: str) -> bytes:
    """Decrypt and verify private key material."""
    try:
        if record.get("version") != 1 or record.get("kdf") != "scrypt":
            raise KeystoreError("Unsupported keystore format or KDF algorithm")
        salt = bytes.fromhex(record["salt_hex"])
        nonce = bytes.fromhex(record["nonce_hex"])
        ciphertext = bytes.fromhex(record["ciphertext_hex"])
        params = record.get("kdf_params", {})
        if (
            params.get("n") != _SCRYPT_N
            or params.get("r") != _SCRYPT_R
            or params.get("p") != _SCRYPT_P
            or params.get("length") != _KEY_LEN
        ):
            raise KeystoreError("Unrecognized Scrypt KDF parameters")
    except (KeyError, ValueError, TypeError) as err:
        raise KeystoreError(f"Malformed keystore record: {err}") from err

    derived_key = _derive_key(passphrase, salt)
    aesgcm = AESGCM(derived_key)
    meta = f"{record['wallet_id']}:{record['address']}:{record['chain_namespace']}"
    associated_data = meta.encode()
    try:
        return aesgcm.decrypt(nonce, ciphertext, associated_data)
    except InvalidTag as err:
        raise KeystoreError(
            "Decryption failed: invalid passphrase or corrupted keystore"
        ) from err
