"""Catalog policy for optional local GGUF models.

The catalog is advisory metadata only. It never downloads, unpacks or launches
anything. A model becomes usable only when the operator supplies a local GGUF
and a signed-off manifest whose SHA-256 and license are checked here.
"""

from dataclasses import dataclass

from hcs_trader.model_manifest import ModelMetadata

_ALLOWED_LICENSES = frozenset({"Apache-2.0", "MIT"})
_MAX_PARAMS_B = 9.0


class ModelNotAdmitted(ValueError):
    """The model does not satisfy the catalog's license and checksum policy."""


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    alias: str
    license: str
    max_params_b: float
    source_hint: str
    verified: bool = False
    note: str = ""


_CATALOG: tuple[CatalogEntry, ...] = (
    CatalogEntry(
        alias="LFM2.5-350M-Q4_K_M",
        license="Apache-2.0",
        max_params_b=0.35,
        source_hint="Operator-supplied GGUF; verify upstream model card and license",
        note="Windows x64 CPU research run recorded in docs/LOCAL_RESEARCH.md",
    ),
    CatalogEntry(
        alias="Qwen3-4B-GGUF",
        license="Apache-2.0",
        max_params_b=4.0,
        source_hint="Operator-supplied GGUF; verify upstream model card and license",
    ),
)


def catalog() -> tuple[CatalogEntry, ...]:
    return _CATALOG


def find(alias: str) -> CatalogEntry | None:
    return next((entry for entry in _CATALOG if entry.alias == alias), None)


def admit(entry: CatalogEntry, metadata: ModelMetadata) -> None:
    """Fail closed unless license, alias, size claim and checksum are acceptable."""
    if metadata.license not in _ALLOWED_LICENSES:
        raise ModelNotAdmitted("Model license is not in the allowlist")
    if metadata.alias != entry.alias:
        raise ModelNotAdmitted("Model alias does not match the catalog entry")
    if entry.max_params_b > _MAX_PARAMS_B:
        raise ModelNotAdmitted("Model exceeds the catalog size bound")
    if len(metadata.sha256) != 64:
        raise ModelNotAdmitted("Model SHA-256 is not fully specified")


def unverified_aliases() -> tuple[str, ...]:
    """Product aliases that cannot be matched to a verifiable public model."""
    return ("OpenJEV", "System One JEV")
