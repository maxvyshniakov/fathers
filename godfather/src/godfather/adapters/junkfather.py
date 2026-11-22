"""Junkfather as a source, read-only (docs/contracts/junkfather.md): `junkfather status --json` decoded into the owned
entries, the last sweep's unowned processes and resources, the procs summary, the Processes page's rows and the joins
of an entry to a request and to a lane. Godfather reads no file of Junkfather's; reap and extend run its CLI."""
from __future__ import annotations

import datetime
from typing import Any, Mapping, Optional, Union, cast

from .. import sources as _sources
from ..model import (Entry, Lane, ProcessRow, Processes, ProcessTableRow, Resource, Separator, Unowned,
                     UnownedResource)
from ..text import minutes
from . import decoding as _decoding
from . import lanefather as _lanefather

TAG = "junk"
CAPABILITIES_VERSIONS = (1,)
DASHBOARD_VERSIONS = (1,)
SNAPSHOT_VERSIONS = (1, 2)  # 2 adds `cpu` and a worktree's repo, branch, age and size; both read alike
ORDER = ("running", "orphan", "expired")  # owned entries on the page: running first, then orphan, then expired
RowKey = tuple[Any, ...]  # ("entry", id) · ("pid", pid, start) · ("resource", kind, id)
Decoded = Optional[Mapping[str, Any]]  # a decoded snapshot as the screen keeps it (Processes), or none
Row = Union[Entry, Unowned, UnownedResource]


def actions(capabilities: Optional[dict[str, Any]]) -> tuple[str, ...]:
    return _lanefather.actions(capabilities)


def show_supported(_: Optional[dict[str, Any]]) -> bool:
    return False


def unsupported(capabilities: dict[str, Any]) -> Optional[str]:
    """Why a capabilities answer names no status snapshot this Godfather reads, None when it names one."""
    block = _lanefather.dashboard_block(capabilities)
    if capabilities.get("v") not in CAPABILITIES_VERSIONS or block.get("v") not in DASHBOARD_VERSIONS:
        return "no dashboard contract; update junkfather"
    snapshot: Any = block.get("snapshot")
    if not isinstance(snapshot, dict) or not isinstance(cast(dict[str, Any], snapshot).get("argv"), list) \
            or not cast(dict[str, Any], snapshot)["argv"]:
        return "no status snapshot; update junkfather"
    versions: Any = cast(dict[str, Any], snapshot).get("versions")
    if not isinstance(versions, list) or not any(v in SNAPSHOT_VERSIONS for v in cast(list[Any], versions)):
        return "its status snapshot needs a newer godfather"
    return None


def state_of(entry: Mapping[str, Any]) -> str:
    """An owned entry as the page tells it: orphan once its owner is gone, expired once its TTL passed and not
    `no_ttl` (not reaped yet), else running."""
    if entry.get("owner_alive") is False:
        return "orphan"
    if not entry.get("no_ttl") and entry.get("ttl") is not None and (entry.get("ttl_left") or 0) <= 0:
        return "expired"
    return "running"


def _entry(raw: dict[str, Any]) -> Entry:
    resource = _decoding.as_object(raw.get("resource")) or None
    argv: Any = raw.get("argv")
    processes: Any = raw.get("processes")
    owner_alive: Any = raw.get("owner_alive")
    held: Optional[Resource] = {"kind": _decoding.str_or_none(resource.get("kind")),
                                "id": _decoding.str_or_none(resource.get("id")),
                                "name": _decoding.str_or_none(resource.get("name"))} if resource else None
    found: Entry = {"id": str(raw["id"]), "key": ("entry", str(raw["id"])), "kind": "entry",
             "phase": _decoding.text(raw.get("state")) or "running", "owner": _decoding.text(raw.get("owner")) or "?",
             "session": _decoding.text(raw.get("session")), "agent": _decoding.text(raw.get("agent")),
             "lane": _decoding.text(raw.get("lane")),
             "owner_alive": owner_alive if isinstance(owner_alive, bool) else None,
             "age": _decoding.number(raw.get("age")) or 0.0, "ttl": _decoding.number(raw.get("ttl")),
             "ttl_left": _decoding.number(raw.get("ttl_left")) or 0.0,
             "no_ttl": _decoding.flag(raw.get("no_ttl")) is True, "keep": _decoding.text(raw.get("keep")),
             "pid": _decoding.pid(raw.get("pid")), "pgid": _decoding.pid(raw.get("pgid")),
             "processes": processes if isinstance(processes, int) else None,
             "label": _decoding.text(raw.get("label")), "mode": _decoding.text(raw.get("mode")),
             "cwd": _decoding.text(raw.get("cwd")),
             "argv": [str(part) for part in cast(list[Any], argv)] if isinstance(argv, list) else [],
             "log": _decoding.text(raw.get("log")),
             "resource": held, "state": "running"}
    found["state"] = state_of(found)
    return found


def decode(snapshot: Any) -> Processes:
    """`junkfather status --json` as the Processes page reads it. SnapshotError for another version or shape."""
    if not isinstance(snapshot, dict):
        raise _sources.SnapshotError("junkfather status --json printed no object")
    found = cast(dict[str, Any], snapshot)
    if found.get("v") not in SNAPSHOT_VERSIONS:
        raise _sources.SnapshotError(f"junkfather status --json version {found.get('v')!r} needs a newer godfather")
    if not isinstance(found.get("entries"), list):
        raise _sources.SnapshotError("junkfather status --json has no entries list")
    # A row without the field that names it is the source's notice, never dropped quietly: a renamed field would
    # hide work while the source looked healthy (fathers#142 review)
    entries: list[Entry] = []
    for raw in _decoding.as_list(found["entries"]):
        entry = _decoding.as_object(raw)
        if _decoding.text(entry.get("id")) is None:
            raise _sources.SnapshotError("an entry has no id; update junkfather")
        entries.append(_entry(entry))
    unowned: list[Unowned] = []
    for raw in cast(list[Any], found.get("unowned") or []):
        item = _decoding.as_object(raw)
        pid = _decoding.pid(item.get("pid"))
        if pid is None:
            raise _sources.SnapshotError("an unowned process has no pid; update junkfather")
        unowned.append({"kind": "unowned", "key": ("pid", pid, str(item.get("start") or "")), "pid": pid,
                        "start": str(item.get("start") or ""), "cmd": _decoding.text(item.get("cmd")) or "?",
                        "cwd": _decoding.text(item.get("cwd")), "root": _decoding.text(item.get("root")),
                        "why": _decoding.text(item.get("why")) or "", "age": _decoding.number(item.get("age")),
                        "alive": _decoding.flag(item.get("alive"))})
    resources: list[UnownedResource] = []
    for raw in cast(list[Any], found.get("unowned_resources") or []):
        item = _decoding.as_object(raw)
        kind, ident = _decoding.text(item.get("kind")), _decoding.text(_decoding.str_or_none(item.get("id")))
        if kind is None or ident is None:
            raise _sources.SnapshotError("an unowned resource has no kind or id; update junkfather")
        resources.append({"kind": "resource", "key": ("resource", kind, ident), "resource_kind": kind, "id": ident,
                          "name": _decoding.text(item.get("name")), "why": _decoding.text(item.get("why")) or "",
                          "branch": _decoding.text(item.get("branch")), "repo": _decoding.text(item.get("repo")),
                          "age": _decoding.number(item.get("age")), "size": _decoding.number(item.get("size"))})
    sweep = _decoding.as_object(found.get("sweep"))
    return {"time": _lanefather.parse_time(found.get("time")), "entries": entries, "unowned": unowned,
            "unowned_resources": resources, "sweep": {"time": _decoding.text(sweep.get("time")),
            "result": sweep.get("result")},
            "errors": [str(error) for error in cast(list[Any], found.get("errors") or [])]}


def entries(decoded: Decoded) -> list[Entry]:
    """The decoded snapshot's owned entries (decode() checked them)."""
    return cast(list[Entry], (decoded or {}).get("entries") or [])


def unowned_processes(decoded: Decoded) -> list[Unowned]:
    """The last sweep's unowned processes."""
    return cast(list[Unowned], (decoded or {}).get("unowned") or [])


def unowned_resources(decoded: Decoded) -> list[UnownedResource]:
    """The last sweep's unowned non-process resources."""
    return cast(list[UnownedResource], (decoded or {}).get("unowned_resources") or [])


def unowned(decoded: Decoded) -> list[Union[Unowned, UnownedResource]]:
    """The last sweep's unowned processes, then its unowned resources."""
    return [*unowned_processes(decoded), *unowned_resources(decoded)]


def summary(decoded: Decoded) -> dict[str, int]:
    """The procs summary in SLOTS' bottom border: owned entries running (processes and resources alike), then those
    whose owner is gone, those past their TTL, live unowned processes and unowned non-process resources from the last
    sweep (dead sweep leftovers with `alive` false are excluded)."""
    owned = entries(decoded)
    live = [item for item in unowned_processes(decoded) if item["alive"] is not False]
    return {"count": len(owned), "orphan": sum(1 for entry in owned if entry["state"] == "orphan"),
            "expired": sum(1 for entry in owned if entry["state"] == "expired"),
            "unowned": len(live), "resources": len(unowned_resources(decoded))}


def _section(label: str, count: int, note: str = "") -> Separator:
    tail = f" · {note}" if note else ""
    return {"kind": "separator", "label": f"{label} · {count}{tail}"}


def page_rows(decoded: Decoded) -> list[ProcessTableRow]:
    """PROCESSES' rows: owned entries (running, then orphan, then expired), then labelled sections for live unowned
    processes, dead sweep leftovers (`gone since the sweep`), and unowned non-process resources."""
    owned = sorted(entries(decoded), key=lambda entry: ORDER.index(entry["state"]))
    procs = unowned_processes(decoded)
    live = [item for item in procs if item["alive"] is not False]
    gone = [item for item in procs if item["alive"] is False]
    resources = unowned_resources(decoded)
    rows: list[ProcessTableRow] = [*owned]
    if live:
        rows += [_section("unowned processes", len(live), "last sweep"), *live]
    if gone:
        rows += [_section("gone since the sweep", len(gone)), *gone]
    if resources:
        rows += [_section("unowned resources", len(resources), "last sweep"), *resources]
    return rows


def find(decoded: Decoded, key: Any) -> Optional[ProcessTableRow]:
    return next((row for row in page_rows(decoded) if row.get("key") == tuple(key or ())), None)


def label_of(row: ProcessRow) -> str:
    """What a row is called: its label, else its resource's name, else its command."""
    if row["kind"] == "resource":
        return row["name"] or row["id"]
    if row["kind"] == "unowned":
        return row["cmd"]
    resource = row["resource"]
    return row["label"] or (resource["name"] if resource else None) or " ".join(row["argv"]) or row["id"]


def short_id(value: str, width: int = 8) -> str:
    return value if len(value) <= width else value[:width - 1] + "…"


# The joins (SPEC 19)

def tree_pids(item: Mapping[str, Any]) -> set[int]:
    """The pids of a request's process tree and its holder."""
    found = {int(row[0]) for row in cast(list[Any], item.get("tree") or []) if isinstance(row[0], int)}
    if isinstance(item.get("pid"), int):
        found.add(int(item["pid"]))
    return found


def of_request(item: Mapping[str, Any], decoded: Decoded, lane: Optional[Mapping[str, Any]]) -> list[Entry]:
    """The owned entries of a request: those whose leader or group pid is in its process tree, or that belong to its
    lane (the lane the request joins to, by the lane rule below)."""
    pids = tree_pids(item)
    found: list[Entry] = []
    for entry in entries(decoded):
        if (entry["pid"] in pids or entry["pgid"] in pids) or (lane is not None and in_lane(entry, lane)):
            found.append(entry)
    return found


def in_lane(entry: Mapping[str, Any], lane: Mapping[str, Any]) -> bool:
    """An entry belongs to a lane when its lane is the lane's id and its directory is inside the lane's worktree."""
    return entry.get("lane") == lane["id"] and _lanefather.inside(entry.get("cwd"), lane.get("worktree"))


def of_lane(lane: Mapping[str, Any], decoded: Decoded) -> list[Entry]:
    return [entry for entry in entries(decoded) if in_lane(entry, lane)]


def lease_of(entry: Mapping[str, Any], items: list[dict[str, Any]]) -> Optional[int]:
    """The request (its index) whose process tree holds the entry's leader or group, None where none does."""
    for index, item in enumerate(items):
        pids = tree_pids(item)
        if entry.get("pid") in pids or (entry.get("pgid") is not None and entry.get("pgid") in pids):
            return index
    return None


def lane_of(entry: Mapping[str, Any], lanes_decoded: Decoded) -> Optional[Lane]:
    """The lane an entry belongs to, by the lane rule; None where no project's lane takes it."""
    return next((lane for lane in _lanefather.lanes(lanes_decoded) if in_lane(entry, lane)), None)


def ttl_text(entry: Mapping[str, Any], elapsed: float = 0.0) -> str:
    """The TTL left as a duration (`1h12m`), `expired` once it passed; an entry without a TTL `—`."""
    if entry.get("no_ttl") or entry.get("ttl") is None:
        return "—"
    left = float(entry.get("ttl_left") or 0) - elapsed
    return minutes(left) if left > 0 else "expired"


def elapsed(decoded: Decoded, moment: Optional[datetime.datetime]) -> float:
    """Seconds since the snapshot was taken, so ages tick between polls."""
    taken = (decoded or {}).get("time")
    if moment is None or not isinstance(taken, datetime.datetime):
        return 0.0
    return max(0.0, (moment - taken).total_seconds())
