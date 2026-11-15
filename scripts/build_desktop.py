"""Build a native onedir application with the local UI embedded."""

import subprocess  # noqa: S404
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAME = "HCS-Trading-Intelligence"


def main() -> None:
    assets = ROOT / "src" / "hcs_trader" / "ui_assets"
    if not (assets / "index.html").is_file():
        raise SystemExit("Build the frontend before building the desktop application.")

    subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--onedir",
            "--name",
            NAME,
            "--distpath",
            str(ROOT / "dist" / "desktop"),
            "--workpath",
            str(ROOT / "build" / "desktop"),
            "--specpath",
            str(ROOT / "build" / "desktop"),
            "--copy-metadata",
            "hcs-trader",
            "--add-data",
            f"{assets}:hcs_trader/ui_assets",
            str(ROOT / "scripts" / "desktop_entry.py"),
        ],
        cwd=ROOT,
        check=True,
    )


if __name__ == "__main__":
    main()
