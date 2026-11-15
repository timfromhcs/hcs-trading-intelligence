"""Archive a tested native desktop folder and write its SHA-256 checksum."""

import platform
import shutil
from hashlib import file_digest
from pathlib import Path

_NAME = "HCS-Trading-Intelligence"


def main() -> None:
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
    archive.with_suffix(".gz.sha256").write_text(
        f"{checksum}  {archive.name}\n", encoding="ascii"
    )
    print(f"{checksum}  {archive}")


if __name__ == "__main__":
    main()
