"""The installed release's CHANGELOG.md as entries, for `lanefather changelog --json` (Godfather's what's new,
fathers#102, #104, #119 and #117; godfather/docs/contracts/changelog.md), backed by `fathers_common.changelog`.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

from fathers_common.changelog import (
    entries as _entries,
)
from fathers_common.changelog import (
    report as _report,
)
from fathers_common.changelog import (
    valid_version as _valid_version,
)
from fathers_common.changelog import (
    version_key as _version_key,
)

PACKAGE = Path(__file__).resolve().parent


def version_key(version: str) -> tuple[int, ...]:
    return _version_key(version)


def valid_version(text: str) -> bool:
    return _valid_version(text)


def entries(text: str, since: str | None = None, commit: str | None = None) -> list[dict[str, Any]]:
    return _entries(text, since, commit)


def path() -> Path:
    """The release's CHANGELOG.md beside the package (install.py copies it there), else the checkout's."""
    shipped = PACKAGE / "CHANGELOG.md"
    return shipped if shipped.is_file() else PACKAGE.parent / "CHANGELOG.md"


def installed_commit() -> str | None:
    try:
        return (PACKAGE / "COMMIT").read_text().strip() or None
    except OSError:
        return None


def report(since: str | None = None, changelog_file: str | Path | None = None,
           commit: str | None = None) -> dict[str, Any]:
    from . import __version__
    chosen = path() if changelog_file is None else Path(changelog_file)
    built = installed_commit() if commit is None and changelog_file is None else commit
    return _report("lanefather", __version__, chosen, since=since, commit=built)


def command(args: Any) -> int:
    if args.since is not None and not valid_version(args.since):
        print(f"lanefather: --since takes a version like 0.1.0, not {args.since!r}", file=sys.stderr)
        return 2
    answer = report(args.since)
    if args.json:
        print(json.dumps(answer, sort_keys=True))
        return 0
    for entry in answer["entries"]:
        print(f"{entry['version'] or 'Unreleased'} {entry['section']}: {entry['text']}")
    return 0
