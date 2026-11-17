import re
import shutil
import subprocess  # noqa: S404
from pathlib import Path

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
