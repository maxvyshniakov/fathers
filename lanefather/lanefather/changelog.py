"""The installed release's CHANGELOG.md as entries, for `lanefather changelog --json` (Godfather's what's new,
fathers#102 and #104; docs/api.md). Standard library and no other lanefather module, so the command stays fast.

  {"v": 1, "tool": "lanefather", "version": "0.1.0", "source": "<the CHANGELOG.md read, or null>",
   "entries": [{"version", "date", "section", "text"}, ...]}

Entries are newest first, as the file keeps them: one per bullet of every `### <section>`, an entry's indented
continuation lines joined by one space, markdown as written. A dated release is `## x.y.z[+build] - YYYY-MM-DD`.
The `## Unreleased` entries come first, as {"version": null, "date": null, "unreleased": true, "source": <the
installed commit, or null in a checkout>}, so a reader can tell news since the commit it last saw.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import sys
from typing import Any

RELEASE = re.compile(r"^## (\d+\.\d+\.\d+\S*) - (\d{4}-\d\d-\d\d)\s*$")
VERSION = re.compile(r"\d+\.\d+\.\d+")
PACKAGE = Path(__file__).resolve().parent


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version.split("-", maxsplit=1)[0].split("+", maxsplit=1)[0]))


def entries(text: str, since: str | None = None, commit: str | None = None) -> list[dict[str, Any]]:
    """Every entry of Unreleased and of every dated release, newest first. Anything before the first release heading
    and under other headings is left out. `since`: only the releases newer than that version, and Unreleased."""
    found: list[dict[str, Any]] = []
    release: dict[str, Any] | None = None
    section: str | None = None
    bulleted = False  # an entry started under the current section: continuation lines join it
    floor = version_key(since) if since else None
    for line in text.splitlines():
        if line.startswith("## "):
            heading = RELEASE.match(line)
            if line.strip() == "## Unreleased":
                release = {"version": None, "date": None, "unreleased": True, "source": commit}
            elif heading and (floor is None or version_key(heading[1]) > floor):
                release = {"version": heading[1], "date": heading[2]}
            else:
                release = None
            section, bulleted = None, False
        elif release is None:
            continue
        elif line.startswith("### "):
            section, bulleted = line[4:].strip(), False
        elif line.startswith("- ") and section:
            found.append({**release, "section": section, "text": line[2:].strip()})
            bulleted = True
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


def report(since: str | None = None) -> dict[str, Any]:
    from . import __version__
    found = path()
    try:
        text: str = found.read_text(encoding="utf-8")
        source: str | None = str(found.resolve())
    except (OSError, UnicodeDecodeError):
        text, source = "", None
    return {"v": 1, "tool": "lanefather", "version": __version__, "source": source,
            "entries": entries(text, since, installed_commit())}


def command(args: Any) -> int:
    if args.since is not None and not VERSION.fullmatch(args.since):
        print(f"lanefather: --since takes a version like 0.1.0, not {args.since!r}", file=sys.stderr)
        return 2
    answer = report(args.since)
    if args.json:
        print(json.dumps(answer, sort_keys=True))
        return 0
    for entry in answer["entries"]:
        print(f"{entry['version'] or 'Unreleased'} {entry['section']}: {entry['text']}")
    return 0
