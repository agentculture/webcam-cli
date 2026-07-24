"""Entry point for ``python -m webcam_cli``."""

from __future__ import annotations

import sys

from webcam_cli.cli import main

if __name__ == "__main__":
    sys.exit(main())
