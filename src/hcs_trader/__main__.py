"""Expose the capabilities of the repository foundation."""

import argparse
import json
from importlib.metadata import version


def main() -> None:
    parser = argparse.ArgumentParser(description="HCS Trading Intelligence")
    parser.add_argument("--version", action="version", version=version("hcs-trader"))
    parser.parse_args()
    print(
        json.dumps(
            {"mode": "PAPER", "execution_available": False, "network_enabled": False}
        )
    )


if __name__ == "__main__":
    main()
