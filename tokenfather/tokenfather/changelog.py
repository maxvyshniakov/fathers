"""The installed release's CHANGELOG.md as entries, for `tokenfather changelog --json` (Godfather's what's new;
godfather/docs/contracts/changelog.md), backed by `fathers_common.changelog` (fathers#119, fathers#117, fathers#208).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fathers_common.changelog import entries, report, valid_version, version_key

from . import __version__

__all__ = ["changelog_path", "contract", "entries", "installed_commit", "text", "valid_version", "version_key"]

PACKAGE = Path(__file__).resolve().parent


def changelog_path() -> Path | None:
    """The release's CHANGELOG.md (installed beside the package's modules), else the checkout's; None without one."""
    for candidate in (PACKAGE / "CHANGELOG.md", PACKAGE.parent / "CHANGELOG.md"):
        if candidate.is_file():
            return candidate
    return None


def installed_commit() -> str | None:
    """The installed release's commit (12 characters, from install.py's COMMIT file), None from a checkout."""
    try:
        built = (PACKAGE / "COMMIT").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return built[:12] or None


def contract(since: str | None = None, path: Path | str | None = None, commit: str | None = None) -> dict[str, Any]:
    """Godfather's changelog contract reply for Tokenfather; `ValueError` for a `since` that is not a version."""
    chosen = changelog_path() if path is None else path
    built = installed_commit() if commit is None and path is None else commit
    return report("tokenfather", __version__, chosen, since=since, commit=built)


def text(found: list[dict[str, Any]]) -> str:
    """Changelog entries for people: a heading per release (Unreleased first), then `  Section: text` lines."""
    lines: list[str] = []
    heading: str | None = None
    for item in found:
        release = "Unreleased" if item.get("unreleased") else f"{item['version']} - {item['date']}"
        if release != heading:
            lines += [""] * bool(lines) + [release]
            heading = release
        lines.append(f"  {item['section']}: {item['text']}")
    return "\n".join(lines)
