"""Build a native desktop application with the local UI embedded."""

import argparse
import subprocess  # noqa: S404
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAME = "HCS-Trading-Intelligence"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--onefile", action="store_true", help="Build a single-file executable"
    )
    args = parser.parse_args()
    layout = "desktop-single" if args.onefile else "desktop"
    assets = ROOT / "src" / "hcs_trader" / "ui_assets"
    skills = ROOT / "src" / "hcs_trader" / "skills"
    sources = ROOT / "src" / "hcs_trader" / "data" / "sources.json"
    if not sources.is_file():
        raise SystemExit("Build the source registry before building the desktop app.")
    if not (assets / "index.html").is_file():
        raise SystemExit("Build the frontend before building the desktop application.")

    subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--onefile" if args.onefile else "--onedir",
            "--name",
            NAME,
            "--distpath",
            str(ROOT / "dist" / layout),
            "--workpath",
            str(ROOT / "build" / layout),
            "--specpath",
            str(ROOT / "build" / layout),
            "--copy-metadata",
            "hcs-trader",
            "--add-data",
            f"{assets}:hcs_trader/ui_assets",
            "--add-data",
            f"{skills}:hcs_trader/skills",
            "--add-data",
            f"{sources}:hcs_trader/data",
            str(ROOT / "scripts" / "desktop_entry.py"),
        ],
        cwd=ROOT,
        check=True,
    )


if __name__ == "__main__":
    main()
