import re
from pathlib import Path

import pytest

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
FILES = sorted(WORKFLOWS.glob("*.yml"))
_USES = re.compile(r"^\s*-?\s*uses:\s*(\S+)", re.MULTILINE)


def test_workflows_exist() -> None:
    names = {path.name for path in FILES}
    assert {"ci.yml", "release.yml", "codeql.yml", "scorecard.yml"} <= names


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_actions_are_pinned_by_full_sha(path: Path) -> None:
    for reference in _USES.findall(path.read_text(encoding="utf-8")):
        if reference.startswith("./"):
            continue
        assert re.search(r"@[0-9a-f]{40}$", reference), reference


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_no_pull_request_target_and_least_privilege(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert "pull_request_target" not in text
    assert re.search(r"^permissions:", text, re.MULTILINE)
    top = text.split("\njobs:", 1)[0]
    assert "write" not in top.split("permissions:", 1)[1].split("\n\n", 1)[0]


def test_release_is_tag_triggered_and_draft_only() -> None:
    text = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    on_block = text.split("permissions:", 1)[0]
    assert "tags:" in on_block
    assert "branches:" not in on_block
    assert "pull_request" not in on_block
    assert "gh release create" in text
    assert "--draft" in text
    # The re-run of every gate must precede anything that can create a release.
    assert re.search(r"preflight:\s+needs: gates", text)
    assert re.search(r"assemble:[\s\S]{0,300}?needs: preflight", text)
    assert re.search(r"publish-draft:\s+needs: assemble", text)
    # Write access exists only on the final job.
    assert text.count("contents: write") == 1
    assert text.index("contents: write") > text.index("publish-draft:")
    assert "pypi" not in text.lower()


def test_ci_is_reusable_and_checks_lockfiles_and_licenses() -> None:
    text = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    assert "workflow_call" in text
    assert "uv lock --check" in text
    assert "license_inventory.py --check" in text
    assert "generate_sbom.py" in text
    assert "npm ci --prefix frontend" in text
