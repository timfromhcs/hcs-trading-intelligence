"""Verify release artifacts: aggregate checksums and distribution contents."""

import argparse
import json
import re
import sys
import tarfile
from hashlib import file_digest
from pathlib import Path
from zipfile import ZipFile

# Names that must never ship inside a wheel or sdist.
_FORBIDDEN_NAME = re.compile(
    r"(\.(db|sqlite|sqlite3|gguf|pem|key|p12|pfx)(-wal|-shm)?$)"
    r"|(^|/)\.env(\.|$)"
    r"|(^|/)keystore[^/]*\.(json|toml|yaml|yml|txt|bin)$"
    r"|(^|/)id_(rsa|ed25519|ecdsa)$",
    re.IGNORECASE,
)
_SECRET_CONTENT = re.compile(
    rb"-----BEGIN [A-Z ]*PRIVATE KEY-----|\bAKIA[0-9A-Z]{16}\b|\bghp_[0-9A-Za-z]{36}\b"
)
_SUMS = "SHA256SUMS"


def sha256_of(path: Path) -> str:
    with path.open("rb") as handle:
        return file_digest(handle, "sha256").hexdigest()


def _covered(path: Path) -> bool:
    return path.is_file() and path.name != _SUMS and not path.name.endswith(".sha256")


def write_sums(directory: Path) -> Path:
    """Write an aggregate checksum file covering every artifact in the directory."""
    lines = [
        f"{sha256_of(path)}  {path.name}"
        for path in sorted(directory.iterdir())
        if _covered(path)
    ]
    if not lines:
        raise RuntimeError("No release artifacts found")
    output = directory / _SUMS
    output.write_text("\n".join(lines) + "\n", encoding="ascii")
    return output


def verify_sums(directory: Path) -> list[str]:
    """Return a list of problems; empty means every listed file matches."""
    sums = directory / _SUMS
    if not sums.is_file():
        return [f"{_SUMS} is missing"]
    problems: list[str] = []
    listed: set[str] = set()
    for line in sums.read_text(encoding="ascii").splitlines():
        digest, _, name = line.partition("  ")
        if not re.fullmatch(r"[0-9a-f]{64}", digest) or not name:
            problems.append(f"malformed checksum line: {line!r}")
            continue
        listed.add(name)
        target = directory / name
        if not target.is_file():
            problems.append(f"{name} is listed but missing")
        elif sha256_of(target) != digest:
            problems.append(f"{name} checksum mismatch")
    problems += [
        f"{path.name} is not covered by {_SUMS}"
        for path in sorted(directory.iterdir())
        if _covered(path) and path.name not in listed
    ]
    return problems


def _members(archive: Path) -> list[tuple[str, bytes]]:
    members: list[tuple[str, bytes]] = []
    if archive.suffix == ".whl":
        with ZipFile(archive) as zipped:
            members = [
                (info.filename, zipped.read(info))
                for info in zipped.infolist()
                if not info.is_dir()
            ]
    elif archive.name.endswith(".tar.gz"):
        with tarfile.open(archive, "r:gz") as tarred:
            for member in tarred.getmembers():
                extracted = tarred.extractfile(member) if member.isfile() else None
                if extracted is not None:
                    members.append((member.name, extracted.read()))
    else:
        raise ValueError(f"Unsupported distribution type: {archive.name}")
    return members


def check_distribution(archive: Path) -> list[str]:
    """Flag secret-like, model, keystore or database files inside a wheel/sdist."""
    problems: list[str] = []
    for name, content in _members(archive):
        if _FORBIDDEN_NAME.search(name):
            problems.append(f"{archive.name}: forbidden file {name}")
        elif _SECRET_CONTENT.search(content):
            problems.append(f"{archive.name}: secret-like content in {name}")
    return problems


def run_checklist(args: argparse.Namespace) -> None:
    """Report every AGENTS.md section 18 checkbox as evidenced or unevidenced."""
    from hcs_trader.live_readiness import (  # noqa: PLC0415
        evaluate_checklist,
        load_context,
    )

    report = evaluate_checklist(load_context(args.root, args.gates_report, args.dist))
    print(json.dumps(report.to_json(), indent=2))
    if not report.v1_allowed:
        print(
            f"{len(report.unmet)} section 18 item(s) unevidenced; v1.0.0 stays blocked",
            file=sys.stderr,
        )
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, nargs="?")
    parser.add_argument(
        "--write", action="store_true", help=f"write {_SUMS} before verifying"
    )
    parser.add_argument(
        "--checklist", action="store_true", help="evaluate the V1 section 18 checklist"
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--gates-report", type=Path)
    parser.add_argument("--dist", type=Path)
    args = parser.parse_args()
    if args.checklist:
        run_checklist(args)
        return
    if args.directory is None:
        parser.error("directory is required unless --checklist is given")
    if args.write:
        write_sums(args.directory)
    problems = verify_sums(args.directory)
    for archive in sorted(args.directory.iterdir()):
        if archive.name.startswith("hcs_trader-") and (
            archive.suffix == ".whl" or archive.name.endswith(".tar.gz")
        ):
            problems += check_distribution(archive)
    if problems:
        print("\n".join(problems), file=sys.stderr)
        raise SystemExit(1)
    print(f"Verified release artifacts in {args.directory}")


if __name__ == "__main__":
    main()
