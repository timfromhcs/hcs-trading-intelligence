"""Native bundle entry point with a dashboard-first default."""

import sys

from hcs_trader.__main__ import main

if __name__ == "__main__":
    if len(sys.argv) == 1:
        sys.argv.extend(["serve", "--open-browser"])
    main()
