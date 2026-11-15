"""`python -m proctor`: the `proctor` command (the Linux container runs proctor this way)."""

from __future__ import annotations

import sys

from proctor import cli

if __name__ == "__main__":
    sys.exit(cli.main())
