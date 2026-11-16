"""Write installed Python package license metadata as JSON; optionally gate it."""

import argparse
import json
import re
import sys
from importlib.metadata import distributions
from typing import Any

# Strong copyleft that must not ship in the distributable. LGPL is not matched.
_COPYLEFT = re.compile(r"(?<![A-Za-z])(A?GPL|SSPL|EUPL|OSL|CPAL)", re.IGNORECASE)
_EXCEPTION = re.compile(r"\bWITH\b|exception", re.IGNORECASE)
# Reviewed for the pinned build tool. See docs/THIRD_PARTY_LICENSES.md.
_REVIEWED_BUILD_TOOLS = frozenset({("pyinstaller", "6.22.3")})


def collect() -> list[dict[str, Any]]:
    records = []
    for distribution in sorted(distributions(), key=lambda item: item.name.lower()):
        metadata = distribution.metadata
        declared = metadata.get("License-Expression")
        classifiers = [
            entry
            for entry in metadata.get_all("Classifier", [])
            if entry.startswith("License ::")
        ]
        records.append(
            {
                "name": metadata["Name"],
                "version": distribution.version,
                "spdx_expression": declared,
                "legacy_classifiers": classifiers,
                "license_files": metadata.get_all("License-File", []),
            }
        )
    return records


def copyleft_violations(
    records: list[dict[str, Any]], allow: frozenset[str] = frozenset()
) -> list[str]:
    """Return names of packages declaring strong copyleft without an exception."""
    found = []
    for record in records:
        name = record["name"].lower()
        if name in allow or (name, record["version"]) in _REVIEWED_BUILD_TOOLS:
            continue
        expression = record["spdx_expression"] or ""
        alternatives = [part for part in re.split(r"\sOR\s", expression) if part]
        alternatives += record["legacy_classifiers"]
        # A dual-licensed package is acceptable when any option is non-copyleft.
        if alternatives and all(
            _COPYLEFT.search(option) and not _EXCEPTION.search(option)
            for option in alternatives
        ):
            found.append(
                f"{record['name']} {record['version']}: {' | '.join(alternatives)}"
            )
    return found


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero when a strong-copyleft license is declared",
    )
    parser.add_argument(
        "--allow", action="append", default=[], help="reviewed package to exempt"
    )
    args = parser.parse_args()
    records = collect()
    print(json.dumps(records, indent=2))
    if args.check:
        problems = copyleft_violations(
            records, frozenset(name.lower() for name in args.allow)
        )
        if problems:
            print("\n".join(problems), file=sys.stderr)
            raise SystemExit(1)


if __name__ == "__main__":
    main()
