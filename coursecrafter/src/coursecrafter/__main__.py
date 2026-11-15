"""`python -m coursecrafter`: the `coursecrafter` command."""

from __future__ import annotations

import sys

from coursecrafter import cli

if __name__ == "__main__":
    sys.exit(cli.main())
