"""Install the built wheel and sdist in clean environments and smoke-test them."""

import json
import os
import shutil
import subprocess  # noqa: S404
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from smoke_install import probe_installed


def _latest(pattern: str) -> Path:
    found = sorted(Path("dist").glob(pattern), key=lambda p: p.stat().st_mtime)
    if not found:
        raise RuntimeError(f"No {pattern} found in dist/")
    return found[-1]


def _smoke(uv: str, artifact: Path, *, offline: bool) -> None:
    with TemporaryDirectory(prefix="hcs-install-") as temporary:
        root = Path(temporary)
        environment = root / "venv"
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
        install = [uv, "pip", "install", "--python", str(python), "--no-deps"]
        if offline:
            install.append("--offline")
        subprocess.run([*install, str(artifact)], check=True)  # noqa: S603
        subprocess.run([uv, "pip", "check", "--python", str(python)], check=True)  # noqa: S603
        output = subprocess.check_output([str(cli)], text=True)  # noqa: S603
        if json.loads(output) != {
            "mode": "PAPER",
            "execution_available": False,
            "network_enabled": False,
        }:
            raise RuntimeError(
                f"Unexpected capabilities from installed {artifact.name}"
            )
        probe_installed(python, cli, root)


def main() -> None:
    wheel = _latest("hcs_trader-*.whl")
    sdist = _latest("hcs_trader-*.tar.gz")
    with ZipFile(wheel) as archive:
        if "hcs_trader/ui_assets/index.html" not in archive.namelist():
            raise RuntimeError("Built wheel is missing its local web interface")
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv is required to install the wheel")
    _smoke(uv, wheel, offline=True)
    print(f"Verified {wheel.name}")
    # An sdist install builds from source, so the build backend must be reachable.
    _smoke(uv, sdist, offline=False)
    print(f"Verified {sdist.name}")


if __name__ == "__main__":
    main()
