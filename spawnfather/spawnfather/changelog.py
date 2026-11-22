"""The installed release's CHANGELOG.md as entries, for `spawnfather changelog --json` (Godfather's what's new;
godfather/docs/contracts/changelog.md), backed by `fathers_common.changelog` (fathers#119, fathers#117)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fathers_common.changelog import (
    entries,
    report as _report,
    valid_version,
    version_key,
)

from . import __version__
from .release import changelog_path, installed_commit

__all__ = ["contract", "entries", "text", "valid_version", "version_key"]


def contract(since: Optional[str] = None, path: Optional[str] = None,
             commit: Optional[str] = None) -> Dict[str, Any]:
    """Godfather's changelog contract reply for Spawnfather."""
    chosen = changelog_path() if path is None else path
    built = installed_commit() if commit is None and path is None else commit
    return _report("spawnfather", __version__, chosen, since=since, commit=built)


def text(found: List[Dict[str, Any]]) -> str:
    """Changelog entries for people: a heading per release (Unreleased first), then `  Section: text` lines."""
    lines: List[str] = []
    heading: Optional[str] = None
    for item in found:
        release = "Unreleased" if item.get("unreleased") else f"{item['version']} - {item['date']}"
        if release != heading:
            lines += [""] * bool(lines) + [release]
            heading = release
        lines.append(f"  {item['section']}: {item['text']}")
    return "\n".join(lines)
