#!/usr/bin/env python3
"""Install one tool from this checkout: python3 install.py PACKAGE [ARGS...]

PACKAGE is slotfather, lanefather, junkfather or godfather. The package's own installer, PACKAGE/install.py, does
the work on the interpreter running this script; ARGS go to it unchanged (`python3 install.py slotfather --help`).
Each installer copies only its own package into an immutable release and swaps its command in one rename, so the
tools install and upgrade independently: a CI runner can install Slotfather alone.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys

PACKAGES = ("slotfather", "lanefather", "junkfather", "godfather")


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in PACKAGES:
        print(__doc__.strip(), file=sys.stderr)
        return 0 if argv[:1] in (["-h"], ["--help"]) else 2
    installer = Path(__file__).resolve().parent / argv[0] / "install.py"
    os.execv(sys.executable, [sys.executable, str(installer), *argv[1:]])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
