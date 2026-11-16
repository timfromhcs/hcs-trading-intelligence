"""Install the built wheel and sdist in clean environments and smoke-test them."""

import argparse
import json
import os
import shutil
import subprocess  # noqa: S404
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from smoke_install import UNICODE_DIRECTORY, offline_environment, probe_installed


def _latest(pattern: str) -> Path:
    found = sorted(Path("dist").glob(pattern), key=lambda p: p.stat().st_mtime)
    if not found:
        raise RuntimeError(f"No {pattern} found in dist/")
    return found[-1]


def _smoke(
    uv: str, artifact: Path, *, offline: bool, upgrade_from: Path | None = None
) -> None:
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
        if upgrade_from is not None:
            subprocess.run([*install, str(upgrade_from)], check=True)  # noqa: S603
            _check_upgrade_state(python, root, seed=True)
            # A development wheel can still have the baseline version.
            install.append("--reinstall")
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
        if upgrade_from is not None:
            _check_upgrade_state(python, root, seed=False)


def _check_upgrade_state(python: Path, root: Path, *, seed: bool) -> None:
    database = root / UNICODE_DIRECTORY / "app data" / "audit.sqlite"
    if seed:
        database.parent.mkdir(parents=True)
    command = (
        "import sys; from pathlib import Path; "
        "from hcs_trader.storage import AuditStore; "
        "store = AuditStore(Path(sys.argv[1])); store.initialize(); "
    )
    if seed:
        command += "store.set_execution_pause(True);"
    else:
        command += (
            "assert store.execution_pause().paused; "
            "assert any(event.kind == 'execution.paused' "
            "for event in store.list_audit_events());"
        )
    subprocess.run(  # noqa: S603
        [str(python), "-c", command, str(database)],
        env=offline_environment(root),
        cwd=root,
        check=True,
        timeout=30,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upgrade-from", type=Path, help="previously verified wheel")
    args = parser.parse_args()
    wheel = _latest("hcs_trader-*.whl")
    sdist = _latest("hcs_trader-*.tar.gz")
    with ZipFile(wheel) as archive:
        if "hcs_trader/ui_assets/index.html" not in archive.namelist():
            raise RuntimeError("Built wheel is missing its local web interface")
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv is required to install the wheel")
    if args.upgrade_from is not None:
        _smoke(uv, wheel, offline=True, upgrade_from=args.upgrade_from.resolve())
        print(f"Verified upgrade from {args.upgrade_from.name} to {wheel.name}")
    _smoke(uv, wheel, offline=True)
    print(f"Verified {wheel.name}")
    # An sdist install builds from source, so the build backend must be reachable.
    _smoke(uv, sdist, offline=False)
    print(f"Verified {sdist.name}")


if __name__ == "__main__":
    main()
