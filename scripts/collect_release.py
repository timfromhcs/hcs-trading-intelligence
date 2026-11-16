"""Collect artifacts published by the successful jobs of the current release run."""

import argparse
import json
from hashlib import file_digest
from pathlib import Path
from shutil import copyfile

# These upload names are fixed by ci.yml and release.yml.
_UPLOADS = {
    "windows": ("desktop-windows-single", ("HCS-Trading-Intelligence.exe",)),
    "macos": ("desktop-macos-latest", ("HCS-Trading-Intelligence-darwin-*.tar.gz",)),
    "linux": ("desktop-ubuntu-latest", ("HCS-Trading-Intelligence-linux-*.tar.gz",)),
    "python": ("release-python", ("hcs_trader-*.whl", "hcs_trader-*.tar.gz")),
}


def collect(downloads: Path, staging: Path, commit: str) -> None:
    """Record which smoke-tested upload supplied each staged artifact."""
    staging.mkdir()
    evidence: dict[str, dict[str, str]] = {}
    for platform, (upload, patterns) in _UPLOADS.items():
        files = evidence[platform] = {}
        for pattern in patterns:
            candidates = list((downloads / upload).glob(pattern))
            if len(candidates) != 1 or not candidates[0].is_file():
                raise ValueError(f"Expected one {pattern} in {upload}")
            source = candidates[0]
            target = staging / source.name
            copyfile(source, target)
            with target.open("rb") as handle:
                files[target.name] = file_digest(handle, "sha256").hexdigest()
    for upload, name in (
        ("desktop-windows-latest", "HCS-Trading-Intelligence-windows-*.tar.gz"),
        ("release-python", "sbom.cdx.json"),
    ):
        candidates = list((downloads / upload).glob(name))
        if len(candidates) != 1 or not candidates[0].is_file():
            raise ValueError(f"Expected one {name} in {upload}")
        copyfile(candidates[0], staging / candidates[0].name)
    (staging / "build-evidence.json").write_text(
        json.dumps({"commit": commit, "artifacts": evidence}, indent=2) + "\n",
        encoding="utf-8",
    )


# Extra assets: macOS and Linux single-file binaries and the per-platform MCPB
# bundles. They are checksummed in SHA256SUMS and attested with it, but they are
# not part of the section 18 platform evidence above.
_SINGLE_UPLOADS = (
    ("desktop-single-macos-latest", "HCS-Trading-Intelligence-macos-arm64"),
    ("desktop-single-ubuntu-latest", "HCS-Trading-Intelligence-linux-x64"),
)
_MCPB_UPLOADS = (
    "desktop-single-windows-latest",
    "desktop-single-macos-latest",
    "desktop-single-ubuntu-latest",
)


def _only(folder: Path, pattern: str) -> Path:
    candidates = list(folder.glob(pattern))
    if len(candidates) != 1 or not candidates[0].is_file():
        raise ValueError(f"Expected one {pattern} in {folder.name}")
    return candidates[0]


def collect_extras(downloads: Path, staging: Path) -> None:
    """Stage the single-file binaries and MCPB bundles next to the evidence set."""
    for upload, name in _SINGLE_UPLOADS:
        copyfile(_only(downloads / upload, "HCS-Trading-Intelligence"), staging / name)
    for upload in _MCPB_UPLOADS:
        source = _only(downloads / upload, "*.mcpb")
        copyfile(source, staging / source.name)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("downloads", type=Path)
    parser.add_argument("staging", type=Path)
    parser.add_argument("commit")
    args = parser.parse_args()
    collect(args.downloads, args.staging, args.commit)
    collect_extras(args.downloads, args.staging)


if __name__ == "__main__":
    main()
