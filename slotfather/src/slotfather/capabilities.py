"""`slotfather capabilities --json`: which of the dashboard contracts this Slotfather supports (Godfather's discovery).

Each feature is named only once this release has it; a reader hides what is missing. docs/api.md has the shape.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import version as _version

VERSION = 1  # this reply's own version
DASHBOARD_VERSION = 1  # the `dashboard` block below
STATUS_VERSIONS = [2]  # `status --json --full`: the status version; its `dashboard` block carries board.BLOCK_VERSION


def changelog() -> str | None:
    """The CHANGELOG.md shipped with this package (install.py copies it into the release), else the checkout's."""
    here = Path(__file__).resolve()
    for folder in (here.parent, here.parents[2]):
        if (folder / "CHANGELOG.md").is_file():
            return str(folder / "CHANGELOG.md")
    return None


def capabilities() -> dict[str, Any]:
    dashboard: dict[str, Any] = {
        "v": DASHBOARD_VERSION, "tag": "slot",
        "snapshot": {"argv": ["status", "--json", "--full"], "versions": STATUS_VERSIONS, "poll": 1},
        "actions": ["reprioritize"]}
    found = changelog()
    if found is not None:
        dashboard["changelog"] = found
    return {"v": VERSION, "tool": "slotfather", "version": _version.VERSION, "dashboard": dashboard}
