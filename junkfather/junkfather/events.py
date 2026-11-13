"""The event log, `events.jsonl` in the state directory, in Godfather's event schema version 1.

One UTF-8 JSON object per line, at most 16 KiB: `v`, `time` (RFC 3339 UTC with milliseconds), `host`, `source`
(`junkfather`), `kind`, `subject`, `actor`, `details`, optional `level`. Each line goes out in one `write` on a file
opened with O_APPEND, mode 0600; past 4 MiB the log becomes `events.jsonl.1` under an flock on `events.lock`. An
event is written after the change it describes, and one that cannot be written never fails the action.

Kinds (docs/reference.md, Events): entry.owned, entry.adopted, entry.exited, entry.extended, entry.handed,
entry.reaped, entry.reap_failed, resource.owned, resource.stopped, resource.stop_failed, unowned.reported,
unowned.reaped, sweep.finished, hook.failed.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import re
import socket
import time
from typing import Any, Dict, Mapping, Optional

from . import owner as _owner
from . import text as _text

VERSION = 1
SOURCE = "junkfather"
ROTATE = 4 << 20
LINE_MAX = 16 << 10
TEXT_MAX = 400
KINDS = ("entry.owned", "entry.adopted", "entry.exited", "entry.extended", "entry.handed", "entry.reaped",
         "entry.reap_failed", "resource.owned", "resource.stopped", "resource.stop_failed", "unowned.reported",
         "unowned.reaped", "sweep.finished", "hook.failed")
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def now() -> str:
    return str(_text.iso(time.time()))


def host() -> str:
    return socket.gethostname().split(".")[0].lower() or "localhost"


def clean(value: Any, depth: int = 0) -> Any:
    """No control characters, strings cut to TEXT_MAX, nesting cut at four levels."""
    if isinstance(value, str):
        return CONTROL.sub(" ", value)[:TEXT_MAX]
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    if depth >= 3:
        return clean(json.dumps(value, sort_keys=True, default=str), depth)
    if isinstance(value, Mapping):
        return {clean(str(k)): clean(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v, depth + 1) for v in value]
    return clean(str(value), depth)


def subject(entry: Mapping[str, Any]) -> Dict[str, Any]:
    found: Dict[str, Any] = {"type": "entry", "id": str(entry.get("id"))}
    if entry.get("label"):
        found["label"] = str(entry["label"])
    if isinstance(entry.get("pid"), int):
        found["pid"] = entry["pid"]
    return found


def actor_of(owner: Optional[Mapping[str, Any]], via: Optional[str] = None) -> Dict[str, Any]:
    """An owner record as the actor: an agent (session, subagent, lane) or a process."""
    owner = owner or {}
    if owner.get("kind") in ("claude", "codex") and owner.get("session"):
        found: Dict[str, Any] = {"type": "agent", "session": str(owner["session"])}
        if owner.get("agent"):
            found["agent"] = str(owner["agent"])
    else:
        found = {"type": "process"}
    if isinstance(owner.get("pid"), int):
        found["pid"] = owner["pid"]
    if owner.get("lane"):
        found["lane"] = str(owner["lane"])
    via = via or godfather()
    if via:
        found["via"] = via
    return found


def tool(via: str) -> Dict[str, Any]:
    return {"type": "tool", "name": SOURCE, "via": via}


def godfather() -> Optional[str]:
    return "godfather" if os.environ.get("GODFATHER_ACTOR") else None


def caller(via: Optional[str] = None) -> Dict[str, Any]:
    """Whoever runs this command, from the environment (no process table): an agent or this process's parent."""
    return actor_of(_owner.from_env(os.environ), via)


def line(kind: str, subject_: Mapping[str, Any], actor: Mapping[str, Any], details: Mapping[str, Any],
         level: Optional[str] = None) -> str:
    event: Dict[str, Any] = {"v": VERSION, "time": now(), "host": host(), "source": SOURCE, "kind": kind,
                             "subject": clean(subject_), "actor": clean(actor), "details": clean(details)}
    if level and level != "info":
        event["level"] = level
    text = json.dumps(event, sort_keys=True, ensure_ascii=False) + "\n"
    if len(text.encode("utf-8")) > LINE_MAX:
        event["details"] = {"truncated": True}
        text = json.dumps(event, sort_keys=True, ensure_ascii=False) + "\n"
    return text


def write(root: Path, kind: str, subject_: Mapping[str, Any], actor: Mapping[str, Any],
          details: Optional[Mapping[str, Any]] = None, level: Optional[str] = None) -> None:
    try:
        text = line(kind, subject_, actor, details or {}, level).encode("utf-8")
        root.mkdir(parents=True, exist_ok=True)
        path = root / "events.jsonl"
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        if size + len(text) > ROTATE:
            with open(root / "events.lock", "a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                try:
                    if path.stat().st_size + len(text) > ROTATE:
                        os.replace(path, root / "events.jsonl.1")
                except OSError:
                    pass
        descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(descriptor, text)
        finally:
            os.close(descriptor)
    except Exception:  # noqa: BLE001 - an event that cannot be written never fails the action
        pass
