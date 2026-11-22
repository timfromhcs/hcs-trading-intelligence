"""scripts/check_coverage_floors.py: parsing, enforcement and the floor table."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import check_coverage_floors as floors  # noqa: E402

REPORT = """<?xml version="1.0" ?>
<coverage><packages><package><classes>
<class filename="src\\hcs_trader\\risk.py"><lines>
<line number="1" hits="1"/><line number="2" hits="1"/><line number="3" hits="0"/>
<line number="4" hits="1"/>
</lines></class>
<class filename="src/hcs_trader/domain/news.py"><lines/></class>
<class filename="other/thing.py"><lines><line number="1" hits="0"/></lines></class>
</classes></package></packages></coverage>
"""


def test_measure_reads_line_hits_and_normalises_paths(tmp_path: Path) -> None:
    report = tmp_path / "coverage.xml"
    report.write_text(REPORT, encoding="utf-8")
    measured = floors.measure(report)
    assert measured == {"risk.py": 75.0, "domain/news.py": 100.0}


def test_check_flags_low_and_missing_modules() -> None:
    measured = {"a.py": 89.9, "b.py": 90.0}
    problems = floors.check(measured, {"a.py": 90, "b.py": 90, "c.py": 50})
    assert len(problems) == 2
    assert any("a.py" in p and "89.90%" in p for p in problems)
    assert any("c.py" in p and "missing" in p for p in problems)


def test_every_domain_module_has_a_floor() -> None:
    domain = ROOT / "src" / "hcs_trader" / "domain"
    expected = {
        f"domain/{p.name}" for p in domain.glob("*.py") if p.name != "__init__.py"
    }
    assert expected <= set(floors.FLOORS)


def test_floors_never_exceed_target_and_cover_core_modules() -> None:
    assert all(0 < value <= 100 for value in floors.FLOORS.values())
    for module in (
        "risk.py",
        "signing.py",
        "evm_signing.py",
        "solana_signing.py",
        "utxo_psbt.py",
        "spot_execution.py",
    ):
        assert module in floors.FLOORS
