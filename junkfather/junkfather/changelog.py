"""The installed release's CHANGELOG.md as entries, for `junkfather changelog --json` (Godfather's what's new;
godfather/docs/contracts/changelog.md). Standard library and release.py only, so the command stays fast."""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Tuple

from . import __version__
from .release import changelog_path, installed_commit

RELEASE = re.compile(r"^## \[?(\d+\.\d+\.\d+[^\]\s]*)\]? - (\d{4}-\d\d-\d\d)\s*$")
UNRELEASED = re.compile(r"^## \[?Unreleased\]?\s*$", re.IGNORECASE)
VERSION = re.compile(r"\d+\.\d+\.\d+\S*")


def version_key(version: str) -> Tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version.split("-", 1)[0].split("+", 1)[0]))


def valid_version(text: str) -> bool:
    return VERSION.fullmatch(text) is not None


def entries(text: str, since: Optional[str] = None, commit: Optional[str] = None) -> List[Dict[str, Any]]:
    """Every entry newest first as the file keeps them: Unreleased ones first, as
    {version: None, unreleased: True, date: None, source: commit, section, text} (`commit` is the installed commit,
    None from a checkout), then every dated release's as {version, date, section, text}. Anything before the first
    release heading, under `# ` titles or other `## ` headings, or before a `### ` section is left out; an entry's
    two-space-indented continuation lines join it with one space while unindented text ends continuation. `since`:
    releases above a matching heading in the file, else releases with a higher numeric version, plus Unreleased."""
    headings = [m[1] for m in (RELEASE.match(line) for line in text.splitlines()) if m]
    if since and since in headings:
        newer = set(headings[:headings.index(since)])
        return [e for e in entries(text, None, commit) if e["version"] is None or e["version"] in newer]
    found: List[Dict[str, Any]] = []
    release: Optional[Dict[str, Any]] = None
    section: Optional[str] = None
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


def contract(since: Optional[str] = None, path: Optional[str] = None,
             commit: Optional[str] = None) -> Dict[str, Any]:
    """Godfather's changelog contract: {v, tool, version, source, entries: [{version, date, section, text}]},
    newest first, the Unreleased entries first with version null, `unreleased` true and the installed commit as
    their `source`; no changelog in the release gives no entries and source null. ValueError for a `since` that is
    not a version."""
    if since is not None and not valid_version(since):
        raise ValueError(f"--since takes a version like 0.1.0, not {since!r}")
    chosen = changelog_path() if path is None else path
    try:
        if not chosen:
            raise FileNotFoundError
        with open(chosen, encoding="utf-8") as handle:
            body = handle.read()
        source: Optional[str] = os.path.realpath(chosen)
    except (OSError, UnicodeDecodeError):
        body, source = "", None
    built = installed_commit() if commit is None and path is None else commit
    return {"v": 1, "tool": "junkfather", "version": __version__, "source": source,
            "entries": entries(body, since, built)}


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
