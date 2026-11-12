"""Another host's history kept beside this one's (fathers#97): `history export` prints this queue's history as JSON
lines, `history import HOST` merges such lines into hosts/HOST/history.jsonl under the state directory, where they
outlive the other host's rotation and its CI logs, and `stats --host HOST` reads them.

A runner host whose slotfather predates export is pulled with cat, since its history is the same JSON lines:

    ssh RUNNER 'cat ~/.local/state/slotfather/history.jsonl.1 ~/.local/state/slotfather/history.jsonl 2>/dev/null' \\
        | slotfather history import RUNNER
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import IO, Any, Iterable

from . import config as _config
from . import state as _state

HOST = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
IDENTITY = ("label", "pid", "queued", "granted", "ended", "outcome", "cmd")  # what makes one history line itself


def host_dir(host: str, directory: Path | None = None) -> Path:
    if not HOST.fullmatch(host or ""):
        raise _config.SlotError(f"a host is a lowercase name (a-z, 0-9, -), not {host!r}")
    return (directory or _config.state_dir()) / "hosts" / host


def export(out: IO[str] = sys.stdout, directory: Path | None = None) -> int:
    """This queue's history, oldest first, one JSON object a line; torn lines are left out."""
    count = 0
    for item in _state.history_items(directory):
        out.write(json.dumps(item) + "\n")
        count += 1
    return count


def identity(item: dict[str, Any]) -> str:
    return json.dumps([item.get(key) for key in IDENTITY])


def merge(host: str, lines: Iterable[str | bytes], directory: Path | None = None) -> tuple[int, int]:
    """Adds the history lines in `lines` that hosts/HOST/history.jsonl lacks, ordered by when they ended, and
    rewrites the archive whole by rename; returns (new, kept). Lines that are no JSON object are skipped."""
    directory = directory or _config.state_dir()
    _state.require_own(directory, "an import")
    target = host_dir(host, directory) / "history.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target.parent / ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        kept = list(_state.journal(target))
        seen = {identity(item) for item in kept}
        new: list[dict[str, Any]] = []
        for item in _state.objects(lines):
            key = identity(item)
            if key not in seen:
                seen.add(key)
                new.append(item)
        if new:
            merged = sorted(kept + new, key=lambda i: (_config.seconds(i.get("ended")) or 0.0))
            fd, name = tempfile.mkstemp(dir=target.parent, prefix=".history-")
            with os.fdopen(fd, "w") as handle:
                handle.writelines(json.dumps(item) + "\n" for item in merged)
            os.replace(name, target)
    return len(new), len(kept)


def command(argv: list[str]) -> int:
    """`history export` and `history import HOST [FILE]` (stdin without FILE)."""
    if argv == ["export"]:
        export()
        return 0
    if argv[:1] == ["import"] and len(argv) in (2, 3):
        host = argv[1]
        host_dir(host)
        if len(argv) == 3:
            with open(argv[2], "rb") as handle:
                new, kept = merge(host, handle.read().splitlines())
        else:
            new, kept = merge(host, sys.stdin.buffer.read().splitlines())
        print(f"{host}: {new} new history lines, {kept} kept before")
        return 0
    raise _config.SlotError("history takes N, export, or import HOST [FILE]")
