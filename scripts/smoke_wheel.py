"""Install the built wheel offline and verify the installed CLI."""

import json
import os
import shutil
import subprocess  # noqa: S404
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile


def main() -> None:
    wheels = sorted(
        Path("dist").glob("hcs_trader-*.whl"),
        key=lambda p: p.stat().st_mtime,
    )
    if not wheels:
        raise RuntimeError("No wheel found in dist/")
    wheel = wheels[-1]
    with ZipFile(wheel) as archive:
        if "hcs_trader/ui_assets/index.html" not in archive.namelist():
            raise RuntimeError("Built wheel is missing its local web interface")
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv is required to install the wheel")
    with TemporaryDirectory(prefix="hcs-wheel-") as temporary:
        environment = Path(temporary) / "venv"
        sync_env = {**os.environ, "UV_PROJECT_ENVIRONMENT": str(environment)}
        sync_env.pop("VIRTUAL_ENV", None)
        # Install the exact locked runtime dependencies into a clean environment.
        subprocess.run(  # noqa: S603
            [uv, "sync", "--locked", "--no-dev", "--no-install-project"],
            check=True,
            env=sync_env,
        )
        if os.name == "nt":
            python = environment / "Scripts" / "python.exe"
            cli = python.with_name("hcs-trader.exe")
        else:
            python = environment / "bin" / "python"
            cli = python.with_name("hcs-trader")
        subprocess.run(  # noqa: S603
            [
                uv,
                "pip",
                "install",
                "--python",
                str(python),
                "--offline",
                "--no-deps",
                str(wheel),
            ],
            check=True,
        )
        subprocess.run([uv, "pip", "check", "--python", str(python)], check=True)  # noqa: S603
        output = subprocess.check_output([str(cli)], text=True)  # noqa: S603
    if json.loads(output) != {
        "mode": "PAPER",
        "execution_available": False,
        "network_enabled": False,
    }:
        raise RuntimeError("Unexpected capabilities from installed wheel")
    print(f"Verified {wheel.name}")


if __name__ == "__main__":
    main()
