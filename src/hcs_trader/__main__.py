"""Expose the capabilities of the repository foundation."""

import argparse
from importlib.metadata import version
from pathlib import Path

import uvicorn
from platformdirs import user_data_path

from hcs_trader.api import LOCAL_PORT, LOOPBACK_HOST, Status, create_app
from hcs_trader.storage import AuditStore


def serve() -> None:
    store = AuditStore(
        user_data_path("HCS Trading Intelligence", "HCS.media") / "audit.sqlite"
    )
    uvicorn.run(
        create_app(store, Path(__file__).with_name("ui_assets")),
        host=LOOPBACK_HOST,
        port=LOCAL_PORT,
        proxy_headers=False,
        access_log=False,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="HCS Trading Intelligence")
    parser.add_argument("--version", action="version", version=version("hcs-trader"))
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("serve", help="Run the read-only local status service")
    arguments = parser.parse_args()
    if arguments.command == "serve":
        serve()
    else:
        print(Status().model_dump_json())


if __name__ == "__main__":
    main()
