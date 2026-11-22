"""Where Spawnfather keeps its records: one JSON file per entry, the claims the PreToolUse hook writes, the logs of
detached commands and an append-only event log, all under one directory.

  $SPAWNFATHER_STATE (falling back to $JUNKFATHER_STATE, default ~/.local/state/spawnfather)
    entries/<id>.json      one owned process group or resource (docs/reference.md, Entry)
    claims/<session>/<tool_use_id>.json   who ran a Bash command that names spawnfather, or a background one (hook.py)
    logs/<id>.log          stdout and stderr of a detached command
    events.jsonl           what was started, kept, handed over and reaped (events.py, Godfather schema v1)
    sweep.json             the last sweep: its time and result
    unowned.json           the unowned processes the last sweep reported (what `reap --pid --force` may stop)
    lock, sweep.lock       flock files
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
from pathlib import Path
import secrets
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterator, List, Mapping, Optional, cast

from fathers_common.atomic import atomic_write

if TYPE_CHECKING:
    from .entry import Entry

VERSION = 1


def env_value(primary: str, legacy: str, environ: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """Return `primary` from `environ` (`os.environ` by default) if present, else `legacy` if present, else None."""
    source = os.environ if environ is None else environ
    if primary in source:
        return source[primary]
    if legacy in source:
        return source[legacy]
    return None


def state_dir() -> Path:
    configured = os.environ.get("SPAWNFATHER_STATE") or os.environ.get("JUNKFATHER_STATE")
    if configured:
        return Path(configured).expanduser()
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "spawnfather"


def write_atomic(path: Path, data: Any) -> None:
    """Replace `path` with `data` as JSON durably via `fathers_common.atomic.atomic_write`."""
    atomic_write(path, json.dumps(data, indent=1, sort_keys=True) + "\n")


def read_json(path: Path) -> Any:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def as_dict(value: Any) -> Dict[str, Any]:
    """A JSON object as read (its values unchecked), or {} for anything else."""
    return cast(Dict[str, Any], value) if isinstance(value, dict) else {}


def as_list(value: Any) -> List[Any]:
    """A JSON array as read (its items unchecked), or [] for anything else."""
    return cast(List[Any], value) if isinstance(value, list) else []


def entry_of(data: Any) -> Optional[Entry]:
    """A JSON object read from an entry file as an Entry; anything else is not one. Its values are as the file
    holds them: readers check what they rely on, as they always did."""
    return cast("Entry", data) if isinstance(data, dict) else None


class Store:
    def __init__(self, root: Optional[Path] = None):
        self.root = root or state_dir()
        self.entries_dir = self.root / "entries"
        self.claims_dir = self.root / "claims"
        self.logs_dir = self.root / "logs"

    @contextlib.contextmanager
    def lock(self, name: str = "lock", blocking: bool = True) -> Iterator[bool]:
        """An exclusive flock; yields False when not blocking and someone else holds it."""
        self.root.mkdir(parents=True, exist_ok=True)
        with open(self.root / name, "a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def path(self, entry_id: str) -> Path:
        if not entry_id or "/" in entry_id or entry_id.startswith("."):
            raise ValueError(f"not an entry id: {entry_id!r}")
        return self.entries_dir / f"{entry_id}.json"

    def load(self, entry_id: str) -> Optional[Entry]:
        try:
            data = read_json(self.path(entry_id))
        except ValueError:
            return None
        return entry_of(data)

    def save(self, entry: Entry) -> None:
        write_atomic(self.path(entry["id"]), entry)

    def entries(self) -> List[Entry]:
        found: List[Entry] = []
        try:
            names = sorted(os.listdir(self.entries_dir))
        except OSError:
            return found
        for name in names:
            if name.endswith(".json") and not name.startswith("."):
                data = read_json(self.entries_dir / name)
                if isinstance(as_dict(data).get("id"), str):
                    found.append(cast("Entry", data))
        return sorted(found, key=lambda e: e.get("created") or 0)

    def update(self, entry_id: str, change: Callable[["Entry"], Optional[bool]]) -> Optional[Entry]:
        """Read, change and write one entry under the lock; `change` returning False writes nothing."""
        with self.lock():
            entry = self.load(entry_id)
            if entry is None:
                return None
            if change(entry) is not False:
                self.save(entry)
            return entry

    def new_id(self) -> str:
        while True:
            candidate = "j" + secrets.token_hex(3)
            if not self.path(candidate).exists():
                return candidate
