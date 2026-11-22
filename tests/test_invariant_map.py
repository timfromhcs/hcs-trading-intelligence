"""docs/INVARIANTS.md maps every AGENTS.md 13.2 invariant to existing tests."""

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TABLE_ROW = re.compile(r"^\|\s*(\d+)\s*\|(.+)\|(.+)\|\s*$")
TEST_REF = re.compile(r"`(tests/[\w/]+\.py)::(\w+)`")


def _agents_invariant_count() -> int:
    text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    section = text.split("### 13.2 Execution-path invariants", 1)[1]
    section = section.split("\n###", 1)[0]
    return len(re.findall(r"^\d+\. ", section, re.MULTILINE))


def _mapping() -> dict[int, list[tuple[str, str]]]:
    mapping: dict[int, list[tuple[str, str]]] = {}
    for line in (
        (ROOT / "docs" / "INVARIANTS.md").read_text(encoding="utf-8").splitlines()
    ):
        row = TABLE_ROW.match(line)
        if row:
            mapping[int(row.group(1))] = TEST_REF.findall(row.group(3))
    return mapping


def _defined_tests(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name.startswith("test_")
    }


def test_agents_md_still_lists_twelve_invariants() -> None:
    assert _agents_invariant_count() == 12


def test_every_invariant_one_to_twelve_is_mapped_to_a_test() -> None:
    mapping = _mapping()
    assert sorted(mapping) == list(range(1, 13))
    for number, refs in mapping.items():
        assert refs, f"invariant {number} has no test"
        assert len(set(refs)) == len(refs), f"invariant {number} repeats a test"


@pytest.mark.parametrize(
    ("number", "file", "name"),
    [(n, f, t) for n, refs in sorted(_mapping().items()) for f, t in refs],
    ids=lambda value: str(value),
)
def test_referenced_test_exists(number: int, file: str, name: str) -> None:
    path = ROOT / file
    assert path.is_file(), f"invariant {number}: {file} is missing"
    assert name in _defined_tests(path), f"invariant {number}: {file}::{name} not found"
