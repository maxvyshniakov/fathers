"""The installed release's CHANGELOG.md as entries, for `junkfather changelog --json` (Godfather's what's new;
docs/reference.md, Changelog), and which release is installed. Standard library and no other junkfather module, so
the command stays fast."""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Tuple

from . import __version__

PACKAGE_DIR = os.path.dirname(os.path.realpath(__file__))

RELEASE = re.compile(r"^## (\d+\.\d+\.\d+\S*) - (\d{4}-\d\d-\d\d)\s*$")
VERSION = re.compile(r"\d+\.\d+\.\d+")


def version_key(version: str) -> Tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version.split("-")[0].split("+")[0]))


def entries(text: str, since: Optional[str] = None, commit: Optional[str] = None) -> List[Dict[str, Any]]:
    """Every entry newest first as the file keeps them: Unreleased ones first, as
    {version: None, unreleased: True, date: None, source: commit, section, text} (installs come from main, so
    Unreleased is what is installed; `commit` is the installed one, None from a checkout), then every dated
    release's as {version, date, section, text}. Anything before the first release heading is left out; an entry's
    indented continuation lines join it with one space; markdown stays as written. `since`: only releases newer than
    that version, and the Unreleased entries, which are newer than any."""
    found: List[Dict[str, Any]] = []
    release: Optional[Dict[str, Any]] = None
    section: Optional[str] = None
    bulleted = False  # an entry started under the current section: continuation lines join it
    floor = version_key(since) if since else None
    for line in text.splitlines():
        if line.startswith("## "):
            heading = RELEASE.match(line)
            release = {"version": heading[1], "date": heading[2]} if heading else None
            if line[3:].strip().strip("[]").lower() == "unreleased":
                release = {"version": None, "unreleased": True, "date": None, "source": commit}
            elif release and floor is not None and version_key(release["version"]) <= floor:
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
    return found


def valid_version(text: str) -> bool:
    return VERSION.fullmatch(text) is not None



def commit() -> str:
    """The installed release's commit as ` (abc123def456)`, or ` (checkout)` when run from a checkout."""
    try:
        with open(os.path.join(PACKAGE_DIR, "COMMIT"), encoding="utf-8") as handle:
            return f" ({handle.read().strip()[:12]})"
    except OSError:
        return " (checkout)"


def installed_commit() -> Optional[str]:
    """The installed release's commit (12 characters), None from a checkout."""
    found = commit().strip(" ()")
    return found if found and found != "checkout" else None


def changelog_path() -> str:
    return os.path.join(os.path.dirname(PACKAGE_DIR), "CHANGELOG.md")


def contract(since: Optional[str] = None) -> Dict[str, Any]:
    """Godfather's changelog contract: {v, tool, version, source, entries: [{version, date, section, text}]},
    newest first, the Unreleased entries first with version null, `unreleased` true and the installed commit as
    their `source`; no changelog in the release gives no entries and source null. ValueError for a `since` that is
    not a version."""
    if since is not None and not valid_version(since):
        raise ValueError(f"--since takes a version like 0.1.0, not {since!r}")
    path = changelog_path()
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        source: Optional[str] = os.path.realpath(path)
    except (OSError, UnicodeDecodeError):
        text, source = "", None
    return {"v": 1, "tool": "junkfather", "version": __version__, "source": source,
            "entries": entries(text, since, installed_commit())}


def text(found: List[Dict[str, Any]]) -> str:
    """Changelog entries for people: a heading per release (Unreleased first), then `  Section: text` lines."""
    lines: List[str] = []
    heading = None
    for item in found:
        release = "Unreleased" if item.get("unreleased") else f"{item['version']} - {item['date']}"
        if release != heading:
            lines += [""] * bool(lines) + [release]
            heading = release
        lines.append(f"  {item['section']}: {item['text']}")
    return "\n".join(lines)
