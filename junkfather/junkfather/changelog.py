"""The installed release's CHANGELOG.md as entries, for `junkfather changelog --json` (Godfather's what's new;
docs/reference.md, Changelog). Standard library and no other junkfather module, so the command stays fast."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

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
