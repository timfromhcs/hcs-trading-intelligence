"""Package a tested desktop build and write its SHA-256 checksum."""

import argparse
import platform
import shutil
from hashlib import file_digest
from pathlib import Path

_NAME = "HCS-Trading-Intelligence"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--onefile", action="store_true")
    args = parser.parse_args()
    if args.onefile:
        archive = Path("dist") / "desktop-single" / f"{_NAME}.exe"
    else:
        desktop = Path("dist") / "desktop"
        system = platform.system().lower()
        arch = platform.machine().lower()
        archive = Path(
            shutil.make_archive(
                str(desktop / f"{_NAME}-{system}-{arch}"), "gztar", desktop, _NAME
            )
        )
    with archive.open("rb") as handle:
        checksum = file_digest(handle, "sha256").hexdigest()
    archive.with_name(archive.name + ".sha256").write_text(
        f"{checksum}  {archive.name}\n", encoding="ascii"
    )
    print(f"{checksum}  {archive}")


if __name__ == "__main__":
    main()
