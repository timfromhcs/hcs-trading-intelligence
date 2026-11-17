"""Build a per-platform .mcpb bundle and fill the MCP registry server.json.

Subcommands:
  bundle    zip mcpb/manifest.json plus one frozen single-file binary
  registry  substitute bundle SHA-256 values into server.json (release step)
  check     validate the committed manifest and server.json (offline)
"""

import argparse
import json
import platform
import re
import stat
import tomllib
from hashlib import file_digest
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]
NAME = "HCS-Trading-Intelligence"
PLATFORM_TAGS = ("windows-x64", "macos-arm64", "linux-x64")
_PLACEHOLDER = re.compile(r"\$\{(SHA256_[A-Z0-9_]+)\}")


def project_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return str(tomllib.load(handle)["project"]["version"])


def platform_tag() -> str:
    system = {"windows": "windows", "darwin": "macos", "linux": "linux"}[
        platform.system().lower()
    ]
    machine = platform.machine().lower()
    arch = "arm64" if machine in {"arm64", "aarch64"} else "x64"
    return f"{system}-{arch}"


def load_manifest() -> dict[str, object]:
    manifest = json.loads((ROOT / "mcpb" / "manifest.json").read_text("utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("manifest.json must be an object")
    return manifest


def check_consistency() -> None:
    manifest = load_manifest()
    version = project_version()
    if manifest.get("version") != version:
        raise ValueError(f"manifest version differs from pyproject version {version}")
    server = json.loads((ROOT / "server.json").read_text("utf-8"))
    if server.get("version") != version:
        raise ValueError(
            f"server.json version differs from pyproject version {version}"
        )
    for package in server["packages"]:
        if f"/v{version}/" not in package["identifier"]:
            raise ValueError("server.json package URL does not match the version")


def build_bundle(binary: Path, output_dir: Path) -> Path:
    check_consistency()
    if not binary.is_file():
        raise FileNotFoundError(binary)
    tag = platform_tag()
    windows = tag.startswith("windows")
    entry = f"server/{NAME}" + (".exe" if windows else "")
    manifest = load_manifest()
    output_dir.mkdir(parents=True, exist_ok=True)
    bundle = output_dir / f"{NAME}-{project_version()}-{tag}.mcpb"
    with ZipFile(bundle, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "manifest.json", json.dumps(manifest, indent=2, sort_keys=False) + "\n"
        )
        info = ZipInfo(entry, date_time=(2020, 1, 1, 0, 0, 0))
        info.compress_type = ZIP_DEFLATED
        info.external_attr = (stat.S_IFREG | 0o755) << 16
        archive.writestr(info, binary.read_bytes())
    digest = _sha256(bundle)
    bundle.with_name(bundle.name + ".sha256").write_text(
        f"{digest}  {bundle.name}\n", encoding="ascii"
    )
    print(f"{digest}  {bundle}")
    return bundle


def _sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return file_digest(handle, "sha256").hexdigest()


def fill_registry(bundles: Path, output: Path) -> None:
    check_consistency()
    text = (ROOT / "server.json").read_text("utf-8")
    values: dict[str, str] = {}
    for tag in PLATFORM_TAGS:
        found = list(bundles.glob(f"{NAME}-{project_version()}-{tag}.mcpb"))
        if len(found) != 1:
            raise FileNotFoundError(f"Expected one {tag} bundle in {bundles}")
        values["SHA256_" + tag.upper().replace("-", "_")] = _sha256(found[0])

    def substitute(match: re.Match[str]) -> str:
        return values[match.group(1)]

    filled = _PLACEHOLDER.sub(substitute, text)
    document = json.loads(filled)
    for package in document["packages"]:
        if not re.fullmatch(r"[0-9a-f]{64}", package["fileSha256"]):
            raise ValueError("Unfilled or invalid fileSha256")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    bundle = commands.add_parser("bundle")
    bundle.add_argument("--binary", type=Path, required=True)
    bundle.add_argument("--output-dir", type=Path, default=ROOT / "dist" / "mcpb")
    registry = commands.add_parser("registry")
    registry.add_argument("--bundles", type=Path, required=True)
    registry.add_argument("--output", type=Path, default=ROOT / "dist" / "server.json")
    commands.add_parser("check")
    args = parser.parse_args()
    if args.command == "bundle":
        build_bundle(args.binary, args.output_dir)
    elif args.command == "registry":
        fill_registry(args.bundles, args.output)
    else:
        check_consistency()
        print("manifest.json and server.json are consistent")


if __name__ == "__main__":
    main()
