"""Keep README, API and threat-model documentation aligned with the code."""

import re
from pathlib import Path

import pytest

from hcs_trader.api import Capabilities

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
CLAIM_FILES = [ROOT / "README.md", ROOT / "CHANGELOG.md"] + sorted(DOCS.glob("*.md"))
FORBIDDEN = [
    "guaranteed profit",
    "risk free",
    "risk-free",
    "unhackable",
    "100% secure",
    "fully secure",
    "bug-free",
    "zero bugs",
    "never loses",
]
README = (ROOT / "README.md").read_text(encoding="utf-8")


def test_readme_has_the_twenty_numbered_sections() -> None:
    numbers = [int(n) for n in re.findall(r"^## (\d+)\. ", README, re.MULTILINE)]
    assert numbers == list(range(1, 21))


def test_readme_capability_table_matches_api_defaults() -> None:
    rows = dict(re.findall(r"^\| `(\w+)` \| (true|false) \|", README, re.MULTILINE))
    expected = {
        name: str(field.default).lower()
        for name, field in Capabilities.model_fields.items()
    }
    assert rows == expected


@pytest.mark.parametrize("path", CLAIM_FILES, ids=lambda p: p.name)
def test_forbidden_marketing_phrases_absent(path: Path) -> None:
    text = path.read_text(encoding="utf-8").lower()
    assert [p for p in FORBIDDEN if p in text] == []


@pytest.mark.parametrize("path", CLAIM_FILES, ids=lambda p: p.name)
def test_relative_links_resolve(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    missing = []
    for target in re.findall(r"\]\(([^)\s]+)\)", text):
        if re.match(r"^(https?:|mailto:|#)", target):
            continue
        file_part = target.split("#", 1)[0]
        if file_part and not (path.parent / file_part).exists():
            missing.append(target)
    assert missing == []


def test_every_api_route_is_documented() -> None:
    source = (ROOT / "src" / "hcs_trader" / "api.py").read_text(encoding="utf-8")
    routes = set(re.findall(r'@app\.(?:get|post)\(\s*"(/api/v1/[^"]+)"', source))
    assert "/api/v1/health" in routes
    documented = (DOCS / "API.md").read_text(encoding="utf-8")
    assert [r for r in sorted(routes) if f"`{r}`" not in documented] == []


def test_planned_endpoints_are_not_implemented() -> None:
    source = (ROOT / "src" / "hcs_trader" / "api.py").read_text(encoding="utf-8")
    documented = (DOCS / "API.md").read_text(encoding="utf-8")
    planned = documented.split("## Planned endpoints", 1)[1].split("\n## ", 1)[0]
    for path in re.findall(r"^\| `(/api/v1/[^`]+)` \|", planned, re.MULTILINE):
        assert f'"{path}"' not in source


def test_threat_model_references_existing_tests() -> None:
    text = (DOCS / "THREAT_MODEL.md").read_text(encoding="utf-8")
    referenced = set(re.findall(r"`(tests/test_\w+\.py)`", text))
    assert referenced
    assert [t for t in sorted(referenced) if not (ROOT / t).exists()] == []


def test_threat_model_does_not_pin_drifting_values() -> None:
    text = (DOCS / "THREAT_MODEL.md").read_text(encoding="utf-8")
    assert not re.search(r"schema v\d+", text, re.IGNORECASE)
    assert "$10" not in text


def test_required_docs_exist() -> None:
    for name in (
        "WALLET_SECURITY.md",
        "API.md",
        "STRATEGY_RESEARCH.md",
        "THREAT_MODEL.md",
        "DATA_SOURCES.md",
    ):
        assert (DOCS / name).is_file()
    assert len(list((DOCS / "ADR").glob("0*.md"))) >= 9


def test_data_source_registry_lists_required_fields() -> None:
    text = (DOCS / "DATA_SOURCES.md").read_text(encoding="utf-8")
    header = next(line for line in text.splitlines() if line.startswith("| Source ID"))
    for column in ("Owner", "Format", "Language", "Key", "Quota", "Terms", "Retry"):
        assert column in header
