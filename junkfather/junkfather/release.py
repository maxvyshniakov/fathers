"""Which Junkfather release runs: its package root and the commit it was installed from.

An install is share/releases/<version>-<commit> with share/current pointing at it (install.py). PACKAGE_ROOT is
resolved once, at import, so a running supervisor keeps naming the release it started from; COMMIT is read through
the unresolved path, as it always was. Standard library and no other Junkfather module."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

PACKAGE_ROOT = str(Path(__file__).resolve().parent.parent)


def commit() -> str:
    """The installed release's commit as ` (abc123def456)`, or ` (checkout)` when run from a checkout."""
    try:
        with open(os.path.join(os.path.dirname(__file__), "COMMIT"), encoding="utf-8") as handle:
            return f" ({handle.read().strip()[:12]})"
    except OSError:
        return " (checkout)"


def installed_commit() -> Optional[str]:
    """The installed release's commit (12 characters), None from a checkout."""
    found = commit().strip(" ()")
    return found if found and found != "checkout" else None


def changelog_path() -> str:
    return os.path.join(PACKAGE_ROOT, "CHANGELOG.md")
