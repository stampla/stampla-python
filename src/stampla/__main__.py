"""Allow running as ``python -m stampla``."""

import sys

from stampla.cli import main

if __name__ == "__main__":
    sys.exit(main())
