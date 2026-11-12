"""`slotfather changelog --json [--since VERSION]` (fathers#103): the installed release's CHANGELOG.md as data for
Godfather's what's new: {"v": 1, "tool", "version", "source", "entries": [{"version", "date", "section", "text",
"unreleased"}]}, newest first, one entry per bullet in file order. Installs come from main, so the Unreleased
entries are what is installed: they come first, with `version` and `date` null, `unreleased: true` and `source`, the
installed commit. `--since VERSION` keeps the releases newer than VERSION, and Unreleased. Read-only; a missing or
unreadable changelog gives no entries."""
from __future__ import annotations

import contextlib
import json
from pathlib import Path
import re
from typing import Any

from . import config as _config

UNRELEASED = re.compile(r"##\s+Unreleased\s*", re.IGNORECASE)
RELEASE = re.compile(r"##\s+(\d+(?:\.\d+)*)\s+-\s+(\d{4}-\d{2}-\d{2})\s*")
SECTION = re.compile(r"###\s+(.+?)\s*")


def version_key(text: str) -> tuple[int, ...]:
    if not re.fullmatch(r"\d+(?:\.\d+)*", text or ""):
        raise _config.SlotError(f"--since takes a version such as 0.1.0, not {text!r}")
    return tuple(int(part) for part in text.split("."))


def parse(text: str, commit: str | None = None) -> list[dict[str, Any]]:
    """The entries of a changelog in the Sid-Kit format, in file order (Unreleased, then newest release first); an
    Unreleased entry carries `source`: `commit`."""
    entries: list[dict[str, Any]] = []
    release: tuple[str | None, str | None] | None = None
    section: str | None = None
    current: dict[str, Any] | None = None
    blank = False  # a blank line since the current entry's last line

    def close() -> None:
        nonlocal current
        if current is not None:
            current["text"] = " ".join(current["text"].split())
            if current["text"]:
                entries.append(current)
        current = None

    for line in text.splitlines():
        if line.startswith("## "):
            close()
            found = RELEASE.fullmatch(line)
            release = (found.group(1), found.group(2)) if found else (None, None) if UNRELEASED.fullmatch(line) \
                else None
            section = None
        elif line.startswith("### "):
            close()
            found = SECTION.fullmatch(line)
            section = found.group(1) if found else None
        elif line.startswith("# "):  # the title: what follows is no release until the next one
            close()
            release, section = None, None
        elif release is None:
            continue
        elif re.match(r"[-*] ", line):
            close()
            current = {"version": release[0], "date": release[1], "section": section, "text": line[2:],
                       "unreleased": release[0] is None}
            if release[0] is None:
                current["source"] = commit
            blank = False
        elif not line.strip():
            blank = True
        elif current is not None and (not blank or line.startswith(" ")):
            current["text"] += " " + line.strip()  # a wrapped line, or a nested list's line
            blank = False
        else:
            close()  # prose after the list belongs to no entry
    close()
    return entries


def changelog(since: str | None = None, path: str | None = None) -> dict[str, Any]:
    from .capabilities import changelog as changelog_path
    from .version import VERSION
    source = path if path is not None else changelog_path()
    try:
        text = Path(source).read_text(encoding="utf-8") if source else ""
    except (OSError, UnicodeDecodeError):
        text, source = "", None
    commit = None
    with contextlib.suppress(Exception):
        from .version import info
        commit = info().get("commit")
    entries = parse(text, commit)
    if since is not None:
        floor = version_key(since)
        entries = [e for e in entries if e["version"] is None or version_key(e["version"]) > floor]
    return {"v": 1, "tool": "slotfather", "version": VERSION, "source": source, "entries": entries}


def command(argv: list[str]) -> int:
    since = None
    rest = list(argv)
    if "--since" in rest:
        at = rest.index("--since")
        if at + 1 >= len(rest):
            raise _config.SlotError("--since takes a version")
        since = rest[at + 1]
        version_key(since)
        del rest[at:at + 2]
    if rest != ["--json"]:
        raise _config.SlotError("changelog takes --json and optionally --since VERSION")
    print(json.dumps(changelog(since), sort_keys=True))
    return 0
