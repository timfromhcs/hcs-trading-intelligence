import json
import re
import shutil
import subprocess  # noqa: S404
import tomllib
from pathlib import Path
from zipfile import ZipFile

import pytest
import verify_release

ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN = verify_release._FORBIDDEN_NAME  # noqa: SLF001
GIT = shutil.which("git")
UV = shutil.which("uv")


def test_gitignore_blocks_secret_state() -> None:
    patterns = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for required in (".env", "*.db", "*.sqlite*"):
        assert required in patterns


@pytest.mark.skipif(GIT is None, reason="git is required")
def test_tracked_files_contain_no_secret_like_names() -> None:
    assert GIT is not None
    listing = subprocess.run(  # noqa: S603
        [GIT, "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True
    ).stdout.decode("utf-8")
    tracked = [name for name in listing.split("\0") if name]
    assert tracked
    offenders = [n for n in tracked if FORBIDDEN.search(n) and "fixtures" not in n]
    assert offenders == []
    assert not [n for n in tracked if re.search(r"\.(gguf|sqlite3?|db)$", n)]


@pytest.mark.skipif(UV is None, reason="uv is required")
def test_frontend_version_matches_python_version() -> None:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        python_version = str(tomllib.load(handle)["project"]["version"])
    frontend = json.loads((ROOT / "frontend" / "package.json").read_text("utf-8"))

    def normalize(value: str) -> str:
        return re.sub(r"-rc\.(\d+)$", r"rc\1", value)

    assert normalize(str(frontend["version"])) == normalize(python_version)


@pytest.mark.skipif(UV is None, reason="uv is required")
def test_built_wheel_contains_runtime_data(tmp_path: Path) -> None:
    assert UV is not None
    subprocess.run(  # noqa: S603
        [UV, "build", "--wheel", "--out-dir", str(tmp_path)],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    wheels = list(tmp_path.glob("hcs_trader-*.whl"))
    assert len(wheels) == 1
    with ZipFile(wheels[0]) as archive:
        names = set(archive.namelist())
    assert "hcs_trader/data/sources.json" in names
    assert "hcs_trader/ui_assets/index.html" in names
    assert not [name for name in names if name.endswith(".gguf")]


def test_built_sdist_excludes_state_and_secrets(tmp_path: Path) -> None:
    assert UV is not None
    subprocess.run(  # noqa: S603
        [UV, "build", "--sdist", "--out-dir", str(tmp_path)],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    sdists = list(tmp_path.glob("hcs_trader-*.tar.gz"))
    assert len(sdists) == 1
    assert verify_release.check_distribution(sdists[0]) == []
