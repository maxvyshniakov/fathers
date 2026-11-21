"""Shared parser and reply builder for `<tool> changelog --json` (godfather/docs/contracts/changelog.md, #119/#117)."""

from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Any

RELEASE = re.compile(r"^## \[?(\d+\.\d+\.\d+[^\]\s]*)\]? - (\d{4}-\d\d-\d\d)\s*$")
UNRELEASED = re.compile(r"^## \[?Unreleased\]?\s*$", re.IGNORECASE)
VERSION = re.compile(r"\d+\.\d+\.\d+\S*")


def version_key(version: str) -> tuple[int, ...]:
    """Numeric release key, ignoring pre-release (`-`) and build (`+`) suffixes."""
    return tuple(int(part) for part in re.findall(r"\d+", version.split("-", 1)[0].split("+", 1)[0]))


def valid_version(text: str) -> bool:
    """Whether `text` is a valid `--since` version (`x.y.z`, optionally with a pre-release or build suffix)."""
    return bool(text) and VERSION.fullmatch(text) is not None


def entries(text: str, since: str | None = None, commit: str | None = None) -> list[dict[str, Any]]:
    """Parse changelog entries newest first, following godfather/docs/contracts/changelog.md."""
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


def report(
    tool: str,
    version: str,
    path: Path | str | None,
    *,
    since: str | None = None,
    commit: str | None = None,
) -> dict[str, Any]:
    """Build the `changelog --json` v1 contract reply for `tool`."""
    if since is not None and not valid_version(since):
        raise ValueError(f"--since takes a version like 0.1.0, not {since!r}")
    chosen = str(path) if path is not None else ""
    try:
        if not chosen:
            raise FileNotFoundError
        with open(chosen, encoding="utf-8") as handle:
            body = handle.read()
        source: str | None = os.path.realpath(chosen)
    except (OSError, UnicodeDecodeError):
        body, source = "", None
    return {
        "v": 1,
        "tool": tool,
        "version": version,
        "source": source,
        "entries": entries(body, since, commit),
    }
