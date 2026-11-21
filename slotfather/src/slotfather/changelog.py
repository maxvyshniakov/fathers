"""`slotfather changelog --json [--since VERSION]` (fathers#103, fathers#119): the installed release's CHANGELOG.md as
data for Godfather's what's new (godfather/docs/contracts/changelog.md): {"v": 1, "tool", "version", "source",
"entries": [{"version", "date", "section", "text"}]}, newest first, one entry per `- ` bullet under a `### ` section in
file order. Installs come from main, so the Unreleased entries come first with `version` and `date` null,
`unreleased: true` and `source` set to the installed commit (null from a checkout). `--since VERSION` keeps releases
newer than VERSION, and Unreleased. Read-only; a missing or unreadable changelog gives no entries."""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import re
from typing import Any, cast

from . import config as _config

RELEASE = re.compile(r"^## \[?(\d+\.\d+\.\d+[^\]\s]*)\]? - (\d{4}-\d\d-\d\d)\s*$")
UNRELEASED = re.compile(r"^## \[?Unreleased\]?\s*$", re.IGNORECASE)
VERSION = re.compile(r"\d+\.\d+\.\d+\S*")


def version_key(version: str) -> tuple[int, ...]:
    if not valid_version(version):
        raise _config.SlotError(f"--since takes a version like 0.1.0, not {version!r}")
    return tuple(int(part) for part in re.findall(r"\d+", version.split("-", 1)[0].split("+", 1)[0]))


def valid_version(text: str) -> bool:
    return bool(text) and VERSION.fullmatch(text) is not None


def entries(text: str, since: str | None = None, commit: str | None = None) -> list[dict[str, Any]]:
    """Every entry of Unreleased and of every dated release, newest first, following godfather/docs/contracts/changelog.md."""
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


def parse(text: str, commit: str | None = None) -> list[dict[str, Any]]:
    return entries(text, None, commit)


def installed_commit() -> str | None:
    """The installed release's commit from version.json beside the package; None when run from a checkout."""
    metadata = Path(__file__).with_name("version.json")
    with contextlib.suppress(OSError, ValueError, UnicodeDecodeError):
        data: object = json.loads(metadata.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            commit = cast(dict[str, Any], data).get("commit")
            if isinstance(commit, str) and commit:
                return commit
    return None


def changelog(since: str | None = None, path: str | None = None,
              commit: str | None = None) -> dict[str, Any]:
    if since is not None:
        version_key(since)
    from .capabilities import changelog as changelog_path
    from .version import VERSION as TOOL_VERSION
    chosen = changelog_path() if path is None else path
    try:
        if not chosen:
            raise FileNotFoundError
        body = Path(chosen).read_text(encoding="utf-8")
        source: str | None = os.path.realpath(chosen)
    except (OSError, UnicodeDecodeError):
        body, source = "", None
    built = installed_commit() if commit is None and path is None else commit
    return {"v": 1, "tool": "slotfather", "version": TOOL_VERSION, "source": source,
            "entries": entries(body, since, built)}


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
