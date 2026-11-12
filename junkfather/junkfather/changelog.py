"""The installed release's CHANGELOG.md as entries, for `junkfather changelog --json` (Godfather's what's new;
docs/reference.md, Changelog). Standard library and no other junkfather module, so the command stays fast."""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

RELEASE = re.compile(r"^## (\d+\.\d+\.\d+\S*) - (\d{4}-\d\d-\d\d)\s*$")
VERSION = re.compile(r"\d+\.\d+\.\d+")


def version_key(version: str) -> Tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version.split("-")[0].split("+")[0]))


def entries(text: str, since: Optional[str] = None) -> List[Dict[str, str]]:
    """Every entry of every dated release, newest first as the file keeps them: {version, date, section, text}.
    Unreleased and anything before the first release heading are left out; an entry's indented continuation lines
    join it with one space; markdown stays as written. `since`: only releases newer than that version."""
    found: List[Dict[str, str]] = []
    release: Optional[Tuple[str, str]] = None
    section: Optional[str] = None
    bulleted = False  # an entry started under the current section: continuation lines join it
    floor = version_key(since) if since else None
    for line in text.splitlines():
        if line.startswith("## "):
            heading = RELEASE.match(line)
            release = (heading[1], heading[2]) if heading else None
            if release and floor is not None and version_key(release[0]) <= floor:
                release = None
            section, bulleted = None, False
        elif release is None:
            continue
        elif line.startswith("### "):
            section, bulleted = line[4:].strip(), False
        elif line.startswith("- ") and section:
            found.append({"version": release[0], "date": release[1], "section": section, "text": line[2:].strip()})
            bulleted = True
        elif line.startswith("  ") and line.strip() and bulleted:
            found[-1]["text"] += " " + line.strip()
    return found


def valid_version(text: str) -> bool:
    return VERSION.fullmatch(text) is not None
