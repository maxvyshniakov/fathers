"""Where junkfather keeps its records: one JSON file per entry, the claims the PreToolUse hook writes, the logs of
detached commands and an append-only event log, all under one directory.

  $JUNKFATHER_STATE (default ~/.local/state/junkfather)
    entries/<id>.json      one owned process group (docs/reference.md, Entry)
    claims/<session>/<tool_use_id>.json   who ran a Bash command that names junkfather (hook.py)
    logs/<id>.log          stdout and stderr of a detached command
    events.jsonl           what was started, kept, handed over and reaped (events.py, Godfather schema v1)
    sweep.json             the last sweep: its time and result
    unowned.json           the unowned processes the last sweep reported (what `reap --pid --force` may stop)
    lock, sweep.lock       flock files
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import os
from pathlib import Path
import secrets
import tempfile
from typing import Any, Callable, Dict, Iterator, List, Optional

VERSION = 1

Entry = Dict[str, Any]


def state_dir() -> Path:
    configured = os.environ.get("JUNKFATHER_STATE")
    if configured:
        return Path(configured).expanduser()
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "junkfather"


def write_atomic(path: Path, data: Any) -> None:
    """Replace `path` with `data` as JSON in one rename, after the new content reached the disk (fsync of the file,
    then of its directory). A write that fails leaves the old file as it was and removes its temp file. A failed
    directory fsync raises (the new content is in place but not known to be durable), except on a filesystem that
    cannot sync a directory (EINVAL, ENOTSUP)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
        try:
            json.dump(data, handle, indent=1, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            handle.close()
            with contextlib.suppress(OSError):
                os.unlink(handle.name)
            raise
    try:
        os.replace(handle.name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(handle.name)
        raise
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    except OSError as error:
        if error.errno not in (errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP):
            raise  # the rename is in place but not known to be on disk: the caller must not take it for durable
    finally:
        os.close(directory)


def read_json(path: Path) -> Any:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


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
        return data if isinstance(data, dict) else None

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
                if isinstance(data, dict) and isinstance(data.get("id"), str):
                    found.append(data)
        return sorted(found, key=lambda e: e.get("created") or 0)

    def update(self, entry_id: str, change: Callable[[Entry], Optional[bool]]) -> Optional[Entry]:
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
