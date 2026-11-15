"""Verification of operator-selected GGUF artifacts and declared metadata."""

import hmac
from hashlib import file_digest
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    alias: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,80}$")
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    license: str = Field(min_length=1, max_length=120, pattern=r"^\S.*\S$|^\S$")
    quantization: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,40}$")
    runtime_version: str = Field(min_length=1, max_length=100)


class _Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[1]
    gguf: str = Field(min_length=1, max_length=1024)
    metadata: ModelMetadata


def verify_model_manifest(path: Path) -> ModelMetadata:
    with path.open("rb") as handle:
        payload = handle.read(16_385)
    if len(payload) > 16_384:
        raise ValueError("Model manifest exceeds the supported size")
    manifest = _Manifest.model_validate_json(payload)
    gguf = path.parent / manifest.gguf
    if gguf.suffix.lower() != ".gguf" or not gguf.is_file():
        raise ValueError("The model manifest must point to a local GGUF file")
    with gguf.open("rb") as handle:
        header = handle.read(24)
        if len(header) != 24 or header[:4] != b"GGUF":
            raise ValueError("Model file does not have a GGUF header")
        handle.seek(0)
        digest = file_digest(handle, "sha256").hexdigest()
    if not hmac.compare_digest(digest, manifest.metadata.sha256):
        raise ValueError("Model file does not match the declared SHA-256")
    return manifest.metadata
