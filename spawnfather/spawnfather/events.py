"""The event log, `events.jsonl` in the state directory, in Godfather's event schema version 1 (fathers#117).

Backed by `fathers_common.events`: strips C0/DEL/C1 and U+2028/U+2029, caps strings to 1000 chars (400 for commands),
bounds nesting at four levels, keeps lines within 16 KiB, and rotates past 4 MiB under a bounded flock on `events.lock`.

Kinds (docs/reference.md, Events): entry.owned, entry.adopted, entry.exited, entry.gone, entry.extended, entry.handed,
entry.reaped, entry.reap_failed, resource.owned, resource.stopped, resource.stop_failed, unowned.reported,
unowned.reaped, sweep.finished, hook.failed.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

from fathers_common import events as _common_events

from . import owner as _owner

VERSION = _common_events.SCHEMA_VERSION
SOURCE = "spawnfather"
ROTATE = _common_events.ROTATE_AT
LINE_MAX = _common_events.MAX_LINE
TEXT_MAX = _common_events.MAX_STRING
COMMAND_MAX = _common_events.MAX_COMMAND
LOCK_WAIT = _common_events.LOCK_WAIT
KINDS = ("entry.owned", "entry.adopted", "entry.exited", "entry.gone", "entry.extended", "entry.handed",
         "entry.reaped", "entry.reap_failed", "resource.owned", "resource.stopped", "resource.stop_failed",
         "unowned.reported", "unowned.reaped", "sweep.finished", "hook.failed")


def now() -> str:
    return _common_events.timestamp()


def host() -> str:
    return _common_events.host_name()


def clean(value: Any, depth: int = 1, limit: int = TEXT_MAX) -> Any:
    return _common_events.clean(value, depth, limit)


def command(value: str | Sequence[str] | None) -> str:
    return _common_events.command(value)


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
    built = _common_events.build_event(SOURCE, kind, subject_, actor, details, level=level)
    return _common_events.encode_event(built, compact=False).decode("utf-8")


def write(root: Path, kind: str, subject_: Mapping[str, Any], actor: Mapping[str, Any],
          details: Optional[Mapping[str, Any]] = None, level: Optional[str] = None,
          max_bytes: int = ROTATE) -> None:
    built = _common_events.build_event(SOURCE, kind, subject_, actor, details or {}, level=level)
    _common_events.append_event(root / "events.jsonl", built, rotate_at=max_bytes, lock_wait=LOCK_WAIT)
