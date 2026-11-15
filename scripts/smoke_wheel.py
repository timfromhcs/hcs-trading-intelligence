"""Install the built wheel offline and verify the installed CLI."""

import json
import shutil
import subprocess  # noqa: S404
from pathlib import Path
from zipfile import ZipFile


def main() -> None:
    (wheel,) = Path("dist").glob("hcs_trader-*.whl")
    with ZipFile(wheel) as archive:
        if "hcs_trader/ui_assets/index.html" not in archive.namelist():
            raise RuntimeError("Built wheel is missing its local web interface")
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv is required to install the wheel")
    output = subprocess.check_output(  # noqa: S603
        [
            uv,
            "run",
            "--offline",
            "--isolated",
            "--no-project",
            "--with",
            str(wheel.resolve()),
            "hcs-trader",
        ],
        text=True,
    )
    if json.loads(output) != {
        "mode": "PAPER",
        "execution_available": False,
        "network_enabled": False,
    }:
        raise RuntimeError("Unexpected capabilities from installed wheel")
    print(f"Verified {wheel.name}")


if __name__ == "__main__":
    main()
