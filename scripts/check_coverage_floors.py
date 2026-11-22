"""Enforce per-module line-coverage floors from a coverage.py XML report.

The global 80% gate in pyproject.toml stays as it is; these floors are additional and
module-specific. A floor is 90 where the module already meets 90, otherwise the value
measured on 2026-11 rounded down (see docs/RELEASE.md). Raise a floor when a module
improves; never lower one to get a build green.

Usage: pytest --cov-report=xml && python scripts/check_coverage_floors.py [coverage.xml]
"""

import argparse
import sys
import xml.etree.ElementTree as ET  # noqa: S405  (our own coverage.py output)
from pathlib import Path

TARGET = 90

# Path prefix (relative to src/hcs_trader/, posix) -> floor in percent.
# A trailing "/" means every module in that package, each checked on its own.
FLOORS: dict[str, int] = {
    "risk.py": 90,
    "signing.py": 75,  # TODO: below 90, raise toward 90
    "evm_signing.py": 85,  # TODO: below 90
    "solana_signing.py": 85,  # TODO: below 90
    "utxo_psbt.py": 90,
    "spot_execution.py": 87,  # TODO: below 90
    "paper_execution.py": 90,
    "domain/assets.py": 90,
    "domain/execution.py": 90,
    "domain/lifecycle.py": 90,
    "domain/macro.py": 90,
    "domain/market_data.py": 90,
    "domain/news.py": 90,
    "domain/paper.py": 90,
    "domain/signing.py": 86,  # TODO: below 90
    "domain/wallet.py": 90,
    "domain/wallet_port.py": 90,
}


def measure(report: Path) -> dict[str, float]:
    """Return line coverage percent per file, keyed by path relative to hcs_trader/."""
    result: dict[str, float] = {}
    root = ET.parse(report).getroot()  # noqa: S314
    for cls in root.iter("class"):
        filename = (cls.get("filename") or "").replace("\\", "/")
        marker = "hcs_trader/"
        if marker not in filename:
            continue
        relative = filename.split(marker, 1)[1]
        lines = list(cls.iter("line"))
        if not lines:
            result[relative] = 100.0
            continue
        hit = sum(1 for line in lines if int(line.get("hits", "0")) > 0)
        result[relative] = 100.0 * hit / len(lines)
    return result


def check(measured: dict[str, float], floors: dict[str, int]) -> list[str]:
    problems: list[str] = []
    for module, floor in sorted(floors.items()):
        if module not in measured:
            problems.append(f"{module}: missing from coverage report")
        elif measured[module] + 1e-9 < floor:
            problems.append(f"{module}: {measured[module]:.2f}% < floor {floor}%")
    return problems


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", nargs="?", type=Path, default=Path("coverage.xml"))
    args = parser.parse_args()
    if not args.report.is_file():
        print(f"{args.report} not found; run pytest --cov-report=xml first")
        sys.exit(2)
    measured = measure(args.report)
    problems = check(measured, FLOORS)
    for module, floor in sorted(FLOORS.items()):
        value = measured.get(module)
        shown = "n/a" if value is None else f"{value:.2f}%"
        note = "" if floor >= TARGET else f"  (below {TARGET}: TODO)"
        print(f"{module:28} {shown:>8}  floor {floor}%{note}")
    if problems:
        print("\nCoverage floor violations:")
        print("\n".join(f"  {p}" for p in problems))
        sys.exit(1)
    print("\nAll per-module coverage floors met.")


if __name__ == "__main__":
    main()
