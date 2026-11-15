"""Write installed Python package license metadata as JSON."""

import json
from importlib.metadata import distributions


def main() -> None:
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

    print(json.dumps(records, indent=2))


if __name__ == "__main__":
    main()
