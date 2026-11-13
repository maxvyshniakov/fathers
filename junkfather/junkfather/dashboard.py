"""What Junkfather shows a dashboard (Godfather; docs/reference.md, Dashboard contract): a row per entry
(`list --json`), the snapshot (`status --json`) and the capabilities (`capabilities --json`). Their shapes only ever
gain fields."""
from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional, TypedDict

from . import __version__
from . import release as _release
from . import entry as _entry
from . import events as _events
from . import owner as _owner
from . import procs as _procs
from . import reap as _reap
from . import resources as _resources
from . import settings as _settings
from .entry import Entry
from .state import Store, as_dict, as_list, read_json
from .text import duration, iso

STATUS_VERSION = 1


class Row(TypedDict):
    """One entry as `list --json` and `status --json` show it."""
    id: str
    state: Optional[str]
    owner: str
    session: Optional[str]
    agent: Optional[str]
    lane: Optional[str]
    owner_alive: Optional[bool]
    age: float
    ttl: Optional[float]
    ttl_left: float
    keep: Optional[str]
    pid: Optional[int]
    pgid: Optional[int]
    processes: Optional[int]
    label: Optional[str]
    mode: Optional[str]
    cwd: Optional[str]
    argv: Optional[List[str]]
    log: Optional[str]
    end: Optional[Dict[str, Any]]
    exit: Optional[int]
    resource: Optional[Dict[str, Any]]  # {kind, id, name} for a resource entry


class UnownedProcess(TypedDict):
    pid: Any
    start: Any
    cmd: Any
    cwd: Any
    root: Any
    why: Any
    age: Optional[float]
    alive: Optional[bool]


class UnownedResource(TypedDict):
    kind: Any
    id: Any
    name: Any
    why: Any


class LastSweep(TypedDict):
    time: Optional[str]
    result: Any


class Status(TypedDict):
    """`status --json` (docs/reference.md, Dashboard contract). Values read back from unowned.json and sweep.json
    are as the last sweep wrote them, hence Any."""
    v: int
    host: str
    time: str
    entries: List[Row]
    unowned: List[UnownedProcess]
    unowned_resources: List[UnownedResource]
    sweep: LastSweep
    errors: List[str]


def row(entry: Entry, table: Optional[_procs.Table], now: float) -> Row:
    """One entry as `list --json` and `status --json` show it."""
    alive = None
    resource = entry.get("resource") if entry.get("mode") == "resource" else None
    if table is not None and entry.get("state") in _entry.ACTIVE and resource is None:
        alive = len(_reap.members(entry, table)[0])
    owner = entry.get("owner") or {}
    return {"id": entry["id"], "state": entry.get("state"), "owner": _owner.describe(owner),
            "session": owner.get("session"), "agent": owner.get("agent"), "lane": owner.get("lane"),
            "owner_alive": _owner.alive(owner, table) if entry.get("state") in _entry.ACTIVE else None,
            "age": now - float(entry.get("created") or now), "ttl": entry.get("ttl"),
            "ttl_left": float(entry.get("expires") or now) - now, "keep": entry.get("keep"),
            "pid": entry.get("pid"), "pgid": entry.get("pgid"), "processes": alive, "label": entry.get("label"),
            "mode": entry.get("mode"), "cwd": entry.get("cwd"), "argv": entry.get("argv"), "log": entry.get("log"),
            "end": entry.get("end"), "exit": entry.get("exit"),
            "resource": {k: resource.get(k) for k in ("kind", "id", "name")} if isinstance(resource, dict) else None}


def rows(store: Store, table: Optional[_procs.Table], now: float, everything: bool = False) -> List[Row]:
    """The active entries' rows (with `everything`, finished ones too)."""
    return [row(e, table, now) for e in store.entries() if everything or e.get("state") in _entry.ACTIVE]


def listing(store: Store, everything: bool = False) -> Dict[str, Any]:
    """`list --json`."""
    now = time.time()
    return {"v": 1, "at": now, "entries": rows(store, _procs.read_or_none(), now, everything)}


def status(store: Store) -> Status:
    """`status --json`: the active entries, the last sweep's unowned processes and resources, and its time and
    result."""
    now = time.time()
    table = _procs.read_or_none()
    last = _record(store, "sweep.json")
    listed = _record(store, "unowned.json")
    unowned: List[UnownedProcess] = []
    for found in as_list(listed.get("items")):
        if not isinstance(found, dict):
            continue
        item = as_dict(found)
        started = _procs.started_at(str(item.get("start") or ""))
        alive = table.alive(item.get("pid"), item.get("start")) if table is not None else None
        unowned.append({"pid": item.get("pid"), "start": item.get("start"), "cmd": item.get("cmd"),
                        "cwd": item.get("cwd"), "root": item.get("root"), "why": item.get("why"),
                        "age": None if started is None else max(0.0, now - started), "alive": alive})
    resources: List[UnownedResource] = [
        {"kind": item.get("kind"), "id": item.get("id"), "name": item.get("name"), "why": item.get("why")}
        for item in map(as_dict, filter(_is_dict, as_list(listed.get("resources"))))]
    return {"v": STATUS_VERSION, "host": _events.host(), "time": _events.now(), "entries": rows(store, table, now),
            "unowned": unowned, "unowned_resources": resources,
            "sweep": {"time": iso(last.get("at")), "result": last.get("result")},
            "errors": [] if table is not None else ["the process table cannot be read"]}


def _is_dict(value: Any) -> bool:
    return isinstance(value, dict)


def _record(store: Store, name: str) -> Dict[str, Any]:
    return as_dict(read_json(store.root / name))


def capabilities(store: Store) -> Dict[str, Any]:
    """`capabilities --json`: what this Junkfather offers a dashboard."""
    dashboard: Dict[str, Any] = {
        "v": 1, "tag": "junk", "snapshot": {"argv": ["status", "--json"], "versions": [STATUS_VERSION], "poll": 5},
        "events": {"path": str((store.root / "events.jsonl").resolve()), "versions": [_events.VERSION],
                   "kinds": list(_events.KINDS)},
        "settings": {"versions": [_settings.VERSION]}, "actions": ["reap", "extend"]}
    changelog = _release.changelog_path()
    if os.path.isfile(changelog):
        dashboard["changelog"] = changelog
    return {"v": 1, "tool": "junkfather", "version": __version__, "commit": _release.commit().strip(" ()"),
            "dashboard": dashboard}


# For people (without --json)

def rows_text(found: List[Row]) -> str:
    """Rows as the `list` table."""
    lines = [f"{'ID':8} {'STATE':8} {'OWNER':34} {'AGE':>7} {'TTL':>7} {'KEEP':7} {'PID':>6} {'PROCS':>5}  COMMAND"]
    for r in found:
        ttl = duration(r["ttl_left"]) if r["state"] in _entry.ACTIVE else "-"
        owner = r["owner"] + ("" if r["owner_alive"] is not False else " (gone)")
        what = _resources.describe(r["resource"]) if r["resource"] else " ".join(r["argv"] or [])
        command = (f"[{r['label']}] " if r["label"] else "") + what
        count = "-" if r["processes"] is None else str(r["processes"])
        lines.append(f"{r['id']:8} {str(r['state']):8} {owner[:34]:34} {duration(r['age']):>7} {ttl:>7} "
                     f"{str(r['keep']):7} {str(r['pid'] or '-'):>6} {count:>5}  {command[:80]}")
    return "\n".join(lines)


def status_text(snapshot: Status) -> str:
    """The snapshot as `status` prints it: the list table, the unowned processes and resources, a summary line."""
    lines = [rows_text(snapshot["entries"])] if snapshot["entries"] else []
    lines += [f"unowned pid {item['pid']} ({item['why']}): {str(item['cmd'])[:120]}" for item in snapshot["unowned"]]
    lines += [f"unowned {_resources.describe(item)}: {item['why']}" for item in snapshot["unowned_resources"]]
    lines += [f"error: {error}" for error in snapshot["errors"]]
    lines.append(f"{len(snapshot['entries'])} running, {len(snapshot['unowned'])} unowned, "
                 f"{len(snapshot['unowned_resources'])} unowned resources; last sweep "
                 f"{snapshot['sweep']['time'] or 'never'}")
    return "\n".join(lines)


def capabilities_text(found: Dict[str, Any]) -> str:
    board = found["dashboard"]
    return (f"junkfather {found['version']} ({found['commit']}): snapshot `junkfather "
            f"{' '.join(board['snapshot']['argv'])}` every {board['snapshot']['poll']} s; events "
            f"{board['events']['path']}; actions {', '.join(board['actions'])}; changelog "
            f"{board.get('changelog') or 'none'}")
