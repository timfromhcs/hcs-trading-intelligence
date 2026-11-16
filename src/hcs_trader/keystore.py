"""Encrypted software keystore using memory-hard KDF and authenticated encryption.

Records keep format ``version`` 1 and carry their own scrypt parameters, which
are read back on decryption. New records use N=2**17 (128 MiB); records written
with the earlier N=2**14 profile still decrypt and can be upgraded with
:func:`rotate_keystore_record`. Only wallets
with the ``hot`` role may hold encrypted signing material: cold and watch-only
wallets are refused at creation and at decryption (AGENTS.md 0.2).
"""

import hashlib
import json
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from hcs_trader.domain.signing import KeystoreError
from hcs_trader.domain.wallet_port import SIGNABLE_WALLET_ROLES, WalletRole

KEYSTORE_VERSION = 1
KDF_PROFILE = 2  # 1 = scrypt N=2**14 (legacy, no field), 2 = scrypt N=2**17

_LEGACY_SCRYPT_N = 2**14
_SCRYPT_N = 2**17
_SCRYPT_R = 8
_SCRYPT_P = 1
_KEY_LEN = 32
_SALT_BYTES = 16
_NONCE_BYTES = 12

# Bounds applied to parameters read from a record so a hostile file can neither
# downgrade the KDF nor exhaust memory.
_MIN_N = _LEGACY_SCRYPT_N
_MAX_N = 2**20
_MAX_P = 4
_MAX_RECORD_BYTES = 65_536
_RECORD_FIELDS = (
    "version",
    "wallet_id",
    "address",
    "chain_namespace",
    "kdf",
    "kdf_params",
    "salt_hex",
    "nonce_hex",
    "ciphertext_hex",
)


def _derive_key(passphrase: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    if not passphrase:
        raise KeystoreError("Passphrase cannot be empty")
    kdf = Scrypt(salt=salt, length=_KEY_LEN, n=n, r=r, p=p)
    return kdf.derive(passphrase.encode("utf-8"))


def _is_power_of_two(value: int) -> bool:
    return value > 0 and value & (value - 1) == 0


def _kdf_params_for(record: dict[str, Any]) -> tuple[int, int, int]:
    """Return validated (n, r, p) for a record, or raise KeystoreError."""
    version = record.get("version")
    params = record.get("kdf_params")
    if record.get("kdf") != "scrypt" or not isinstance(params, dict):
        raise KeystoreError("Unsupported keystore format or KDF algorithm")
    n = params.get("n")
    r = params.get("r")
    p = params.get("p")
    if (
        not isinstance(n, int)
        or not isinstance(r, int)
        or not isinstance(p, int)
        or isinstance(n, bool)
        or isinstance(r, bool)
        or isinstance(p, bool)
        or params.get("length") != _KEY_LEN
        or r != _SCRYPT_R
    ):
        raise KeystoreError("Unrecognized Scrypt KDF parameters")
    if version != KEYSTORE_VERSION:
        raise KeystoreError("Unsupported keystore format or KDF algorithm")
    if not _is_power_of_two(n) or not _MIN_N <= n <= _MAX_N:
        raise KeystoreError("Scrypt cost is outside the accepted range")
    if not 1 <= p <= _MAX_P:
        raise KeystoreError("Scrypt parallelism is outside the accepted range")
    return n, r, p


def _record_role(record: dict[str, Any]) -> str:
    # Version 1 records predate roles and could only be created for hot wallets.
    return str(record.get("wallet_role", "hot"))


def create_keystore_record(
    wallet_id: str,
    address: str,
    chain_namespace: str,
    secret_bytes: bytes,
    passphrase: str,
    *,
    wallet_role: WalletRole = "hot",
) -> dict[str, Any]:
    """Encrypt private key material using Scrypt KDF and AES-256-GCM."""
    if wallet_role not in SIGNABLE_WALLET_ROLES:
        raise KeystoreError(
            f"Wallet role '{wallet_role}' may not hold signing material; "
            "cold and watch-only wallets stay outside the application"
        )
    if not secret_bytes:
        raise KeystoreError("Secret material cannot be empty")
    salt = os.urandom(_SALT_BYTES)
    derived_key = _derive_key(passphrase, salt, _SCRYPT_N, _SCRYPT_R, _SCRYPT_P)
    nonce = os.urandom(_NONCE_BYTES)
    aesgcm = AESGCM(derived_key)
    # Associated data binds wallet_id and address to prevent cut-and-paste substitution
    associated_data = f"{wallet_id}:{address}:{chain_namespace}".encode()
    ciphertext = aesgcm.encrypt(nonce, secret_bytes, associated_data)

    return {
        "version": KEYSTORE_VERSION,
        "wallet_id": wallet_id,
        "address": address,
        "chain_namespace": chain_namespace,
        "wallet_role": wallet_role,
        "kdf_profile": KDF_PROFILE,
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


def keystore_needs_upgrade(record: dict[str, Any]) -> bool:
    """True when a record uses an older or weaker KDF profile than the current one."""
    params = record.get("kdf_params")
    n = params.get("n") if isinstance(params, dict) else None
    return not isinstance(n, int) or n < _SCRYPT_N


def decrypt_keystore_record(record: dict[str, Any], passphrase: str) -> bytes:
    """Decrypt and verify private key material."""
    try:
        if _record_role(record) not in SIGNABLE_WALLET_ROLES:
            raise KeystoreError("Keystore record is not marked as a hot wallet")
        n, r, p = _kdf_params_for(record)
        salt = bytes.fromhex(record["salt_hex"])
        nonce = bytes.fromhex(record["nonce_hex"])
        ciphertext = bytes.fromhex(record["ciphertext_hex"])
        meta = f"{record['wallet_id']}:{record['address']}:{record['chain_namespace']}"
    except (KeyError, ValueError, TypeError) as err:
        raise KeystoreError(f"Malformed keystore record: {err}") from err

    derived_key = _derive_key(passphrase, salt, n, r, p)
    aesgcm = AESGCM(derived_key)
    try:
        return aesgcm.decrypt(nonce, ciphertext, meta.encode())
    except InvalidTag as err:
        raise KeystoreError(
            "Decryption failed: invalid passphrase or corrupted keystore"
        ) from err


def rotate_keystore_record(
    record: dict[str, Any], old_passphrase: str, new_passphrase: str
) -> dict[str, Any]:
    """Re-encrypt a record under a new passphrase and the current KDF profile.

    Also upgrades legacy records. The wallet identity is preserved; salt and
    nonce are regenerated.
    """
    secret = decrypt_keystore_record(record, old_passphrase)
    try:
        return create_keystore_record(
            record["wallet_id"],
            record["address"],
            record["chain_namespace"],
            secret,
            new_passphrase,
            wallet_role="hot",
        )
    finally:
        del secret


def _validate_record_shape(record: object) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise KeystoreError("Keystore record must be a JSON object")
    missing = [field for field in _RECORD_FIELDS if field not in record]
    if missing:
        raise KeystoreError(f"Keystore record is missing fields: {missing}")
    for field in ("wallet_id", "address", "chain_namespace"):
        if not isinstance(record[field], str) or not record[field]:
            raise KeystoreError(f"Keystore record field '{field}' is invalid")
    _kdf_params_for(record)
    return record


_NON_HEX = re.compile(r"[^0-9a-f]")


class FileKeystoreStore:
    """Directory of encrypted keystore records, one atomic JSON file per wallet.

    The store only ever sees ciphertext. File names are a hash of the wallet
    identity, so no caller-supplied string reaches the filesystem path.
    """

    def __init__(self, directory: Path) -> None:
        self._directory = Path(directory)

    @staticmethod
    def _filename(chain_namespace: str, address: str) -> str:
        digest = hashlib.sha256(f"{chain_namespace}\n{address}".encode()).hexdigest()
        return f"{digest}.json"

    def _path(self, chain_namespace: str, address: str) -> Path:
        return self._directory / self._filename(chain_namespace, address)

    def save(self, record: dict[str, Any], *, overwrite: bool = False) -> Path:
        """Atomically write a record; refuses to replace unless ``overwrite``."""
        _validate_record_shape(record)
        if _record_role(record) not in SIGNABLE_WALLET_ROLES:
            raise KeystoreError("Only hot wallet records may be stored")
        path = self._path(record["chain_namespace"], record["address"])
        if path.exists() and not overwrite:
            raise KeystoreError("A keystore record already exists for this wallet")
        self._directory.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(record, sort_keys=True, indent=2).encode()
        descriptor, temp_name = tempfile.mkstemp(
            dir=self._directory, prefix=".ks-", suffix=".tmp"
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                Path(temp_name).chmod(0o600)
            except OSError:  # pragma: no cover - platform dependent
                pass
            os.replace(temp_name, path)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise
        return path

    def load(self, chain_namespace: str, address: str) -> dict[str, Any]:
        path = self._path(chain_namespace, address)
        try:
            if path.stat().st_size > _MAX_RECORD_BYTES:
                raise KeystoreError("Keystore file is too large")
            raw = json.loads(path.read_bytes())
        except FileNotFoundError:
            raise KeystoreError("No keystore record for this wallet") from None
        except (OSError, ValueError) as err:
            raise KeystoreError(f"Keystore file is unreadable: {err}") from err
        record = _validate_record_shape(raw)
        if record["chain_namespace"] != chain_namespace or record["address"] != address:
            raise KeystoreError("Keystore file does not match the requested wallet")
        return record

    def list_wallets(self) -> list[tuple[str, str]]:
        """Return (chain_namespace, address) pairs for every readable record."""
        found: list[tuple[str, str]] = []
        if not self._directory.is_dir():
            return found
        for path in sorted(self._directory.glob("*.json")):
            if _NON_HEX.search(path.stem):
                continue
            try:
                record = _validate_record_shape(json.loads(path.read_bytes()))
            except (OSError, ValueError, KeystoreError):
                continue
            found.append((record["chain_namespace"], record["address"]))
        return found
