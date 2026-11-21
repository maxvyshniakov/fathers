"""The installed release's CHANGELOG.md as entries, for `lanefather changelog --json` (Godfather's what's new,
fathers#102, #104 and #119; godfather/docs/contracts/changelog.md). Standard library and no other lanefather module,
so the command stays fast.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
from typing import Any

RELEASE = re.compile(r"^## \[?(\d+\.\d+\.\d+[^\]\s]*)\]? - (\d{4}-\d\d-\d\d)\s*$")
UNRELEASED = re.compile(r"^## \[?Unreleased\]?\s*$", re.IGNORECASE)
VERSION = re.compile(r"\d+\.\d+\.\d+\S*")
PACKAGE = Path(__file__).resolve().parent


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version.split("-", maxsplit=1)[0].split("+", maxsplit=1)[0]))


def valid_version(text: str) -> bool:
    return VERSION.fullmatch(text) is not None


def entries(text: str, since: str | None = None, commit: str | None = None) -> list[dict[str, Any]]:
    """Every entry of Unreleased and of every dated release, newest first. Anything before the first release heading,
    under `# ` titles or other `## ` headings, or before a `### ` section is left out. `since`: only the releases
    newer than that version, and Unreleased; a version the file has a heading for keeps the releases above that
    heading, so a build (`0.1.0+7eca44a`) counts as newer than the release below it although its number is the same."""
    headings = [m[1] for m in (RELEASE.match(line) for line in text.splitlines()) if m]
    if since and since in headings:
        newer = set(headings[:headings.index(since)])
        return [e for e in entries(text, None, commit) if e["version"] is None or e["version"] in newer]
    found: list[dict[str, Any]] = []
    release: dict[str, Any] | None = None
    section: str | None = None
    bulleted = False
    floor = version_key(since) if since else None
    for line in text.splitlines():
        if line.startswith("# "):
            release, section, bulleted = None, None, False
        elif line.startswith("## "):
            heading = RELEASE.match(line)
            if UNRELEASED.match(line):
                release = {"version": None, "unreleased": True, "date": None, "source": commit}
            elif heading and (floor is None or version_key(heading[1]) > floor):
                release = {"version": heading[1], "date": heading[2]}
            else:
                release = None
            section, bulleted = None, False
        elif release is None:
            continue
        elif line.startswith("### "):
            section, bulleted = line[4:].strip() or None, False
        elif line.startswith("- ") and section:
            item = line[2:].strip()
            if item:
                found.append({**release, "section": section, "text": item})
                bulleted = True
            else:
                bulleted = False
        elif line.startswith("  ") and line.strip() and bulleted:
            found[-1]["text"] += " " + line.strip()
        elif not line.strip():
            continue
        else:
            bulleted = False
    return found


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
    if since is not None and not valid_version(since):
        raise ValueError(f"--since takes a version like 0.1.0, not {since!r}")
    try:
        from . import __version__
    except ImportError:
        __version__ = "0.1.0"
    chosen = path() if changelog_file is None else Path(changelog_file)
    try:
        if not str(chosen):
            raise FileNotFoundError
        body: str = chosen.read_text(encoding="utf-8")
        source: str | None = os.path.realpath(chosen)
    except (OSError, UnicodeDecodeError):
        body, source = "", None
    built = installed_commit() if commit is None and changelog_file is None else commit
    return {"v": 1, "tool": "lanefather", "version": __version__, "source": source,
            "entries": entries(body, since, built)}


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
