"""`slotfather changelog --json [--since VERSION]` (fathers#103, #119, #117): the installed release's CHANGELOG.md as
data for Godfather's what's new (godfather/docs/contracts/changelog.md), backed by `fathers_common.changelog`."""
from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any, cast

from fathers_common.changelog import (
    entries as _entries,
    report as _report,
    valid_version as _valid_version,
    version_key as _version_key,
)

from . import config as _config


def valid_version(text: str) -> bool:
    return _valid_version(text)


def version_key(version: str) -> tuple[int, ...]:
    if not _valid_version(version):
        raise _config.SlotError(f"--since takes a version like 0.1.0, not {version!r}")
    return _version_key(version)


def entries(text: str, since: str | None = None, commit: str | None = None) -> list[dict[str, Any]]:
    if since is not None:
        version_key(since)
    return _entries(text, since, commit)


def parse(text: str, commit: str | None = None) -> list[dict[str, Any]]:
    return _entries(text, None, commit)


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
    built = installed_commit() if commit is None and path is None else commit
    return _report("slotfather", TOOL_VERSION, chosen, since=since, commit=built)


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
