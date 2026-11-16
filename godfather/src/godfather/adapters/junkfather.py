"""Junkfather as a source, read-only (docs/contracts/junkfather.md): `junkfather status --json` decoded into the owned
entries, the last sweep's unowned processes and resources, the procs summary, the Processes page's rows and the joins
of an entry to a request and to a lane. Godfather reads no file of Junkfather's; reap and extend run its CLI."""
from __future__ import annotations

import datetime
from typing import Any, Optional, cast

from .. import sources as _sources
from ..text import minutes
from . import lanefather as _lanefather

TAG = "junk"
CAPABILITIES_VERSIONS = (1,)
DASHBOARD_VERSIONS = (1,)
SNAPSHOT_VERSIONS = (1, 2)  # 2 adds `cpu` and a worktree's repo, branch, age and size; both read alike
ORDER = ("running", "orphan", "expired")  # owned entries on the page: running first, then orphan, then expired
RowKey = tuple[Any, ...]  # ("entry", id) · ("pid", pid, start) · ("resource", kind, id)


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


def _object(value: Any) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _number(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _pid(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _text(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def state_of(entry: dict[str, Any]) -> str:
    """An owned entry as the page tells it: orphan once its owner is gone, expired once its TTL passed (not reaped
    yet), else running."""
    if entry.get("owner_alive") is False:
        return "orphan"
    if entry.get("ttl") is not None and (entry.get("ttl_left") or 0) <= 0:
        return "expired"
    return "running"


def _entry(raw: dict[str, Any]) -> dict[str, Any]:
    resource = _object(raw.get("resource")) or None
    argv: Any = raw.get("argv")
    found = {"id": str(raw["id"]), "key": ("entry", str(raw["id"])), "kind": "entry",
             "phase": _text(raw.get("state")) or "running", "owner": _text(raw.get("owner")) or "?",
             "session": _text(raw.get("session")), "agent": _text(raw.get("agent")), "lane": _text(raw.get("lane")),
             "owner_alive": raw.get("owner_alive") if isinstance(raw.get("owner_alive"), bool) else None,
             "age": _number(raw.get("age")) or 0.0, "ttl": _number(raw.get("ttl")),
             "ttl_left": _number(raw.get("ttl_left")) or 0.0, "keep": _text(raw.get("keep")),
             "pid": _pid(raw.get("pid")), "pgid": _pid(raw.get("pgid")),
             "processes": raw.get("processes") if isinstance(raw.get("processes"), int) else None,
             "label": _text(raw.get("label")), "mode": _text(raw.get("mode")), "cwd": _text(raw.get("cwd")),
             "argv": [str(part) for part in cast(list[Any], argv)] if isinstance(argv, list) else [],
             "log": _text(raw.get("log")),
             "resource": {key: (str(resource[key]) if resource.get(key) is not None else None)
                          for key in ("kind", "id", "name")} if resource else None}
    found["state"] = state_of(found)
    return found


def decode(snapshot: Any) -> dict[str, Any]:
    """`junkfather status --json` as the Processes page reads it. SnapshotError for another version or shape."""
    if not isinstance(snapshot, dict):
        raise _sources.SnapshotError("junkfather status --json printed no object")
    found = cast(dict[str, Any], snapshot)
    if found.get("v") not in SNAPSHOT_VERSIONS:
        raise _sources.SnapshotError(f"junkfather status --json version {found.get('v')!r} needs a newer godfather")
    if not isinstance(found.get("entries"), list):
        raise _sources.SnapshotError("junkfather status --json has no entries list")
    entries = [_entry(_object(raw)) for raw in cast(list[Any], found["entries"])
               if isinstance(_object(raw).get("id"), str)]
    unowned: list[dict[str, Any]] = []
    for raw in cast(list[Any], found.get("unowned") or []):
        item = _object(raw)
        pid = _pid(item.get("pid"))
        if pid is None:
            continue
        unowned.append({"kind": "unowned", "key": ("pid", pid, str(item.get("start") or "")), "pid": pid,
                        "start": str(item.get("start") or ""), "cmd": _text(item.get("cmd")) or "?",
                        "cwd": _text(item.get("cwd")), "root": _text(item.get("root")),
                        "why": _text(item.get("why")) or "", "age": _number(item.get("age")),
                        "alive": item.get("alive") if isinstance(item.get("alive"), bool) else None})
    resources: list[dict[str, Any]] = []
    for raw in cast(list[Any], found.get("unowned_resources") or []):
        item = _object(raw)
        kind, ident = _text(item.get("kind")), _text(str(item.get("id"))) if item.get("id") is not None else None
        if kind is None or ident is None:
            continue
        resources.append({"kind": "resource", "key": ("resource", kind, ident), "resource_kind": kind, "id": ident,
                          "name": _text(item.get("name")), "why": _text(item.get("why")) or "",
                          "branch": _text(item.get("branch")), "repo": _text(item.get("repo")),
                          "age": _number(item.get("age")), "size": _number(item.get("size"))})
    sweep = _object(found.get("sweep"))
    return {"time": _lanefather.parse_time(found.get("time")), "entries": entries, "unowned": unowned,
            "unowned_resources": resources, "sweep": {"time": _text(sweep.get("time")), "result": sweep.get("result")},
            "errors": [str(error) for error in cast(list[Any], found.get("errors") or [])]}


def entries(decoded: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], (decoded or {}).get("entries") or [])


def unowned(decoded: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    """The last sweep's unowned processes, then its unowned resources."""
    found = decoded or {}
    return [*cast(list[dict[str, Any]], found.get("unowned") or []),
            *cast(list[dict[str, Any]], found.get("unowned_resources") or [])]


def summary(decoded: Optional[dict[str, Any]]) -> dict[str, int]:
    """The procs summary in SLOTS' bottom border: owned entries running (processes and resources alike), then those
    whose owner is gone, those past their TTL, and what the last sweep found unowned."""
    owned = entries(decoded)
    return {"count": len(owned), "orphan": sum(1 for entry in owned if entry["state"] == "orphan"),
            "expired": sum(1 for entry in owned if entry["state"] == "expired"), "unowned": len(unowned(decoded))}


def page_rows(decoded: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    """PROCESSES' rows: owned entries (running, then orphan, then expired), then, after a separator, the unowned
    processes and the unowned resources the last sweep reported."""
    owned = sorted(entries(decoded), key=lambda entry: ORDER.index(entry["state"]))
    others = unowned(decoded)
    return [*owned, *([{"kind": "separator"}] if owned and others else []), *others]


def find(decoded: Optional[dict[str, Any]], key: Any) -> Optional[dict[str, Any]]:
    return next((row for row in page_rows(decoded) if row.get("key") == tuple(key or ())), None)


def label_of(row: dict[str, Any]) -> str:
    """What a row is called: its label, else its resource's name, else its command."""
    if row["kind"] == "resource":
        return row.get("name") or row["id"]
    if row["kind"] == "unowned":
        return str(row["cmd"])
    resource = _object(row.get("resource"))
    argv = cast(list[str], row.get("argv") or [])
    return str(row.get("label") or resource.get("name") or " ".join(argv) or row["id"])


def short_id(value: str, width: int = 8) -> str:
    return value if len(value) <= width else value[:width - 1] + "…"


# The joins (SPEC 19)

def tree_pids(item: dict[str, Any]) -> set[int]:
    """The pids of a request's process tree and its holder."""
    found = {int(row[0]) for row in cast(list[Any], item.get("tree") or []) if isinstance(row[0], int)}
    if isinstance(item.get("pid"), int):
        found.add(int(item["pid"]))
    return found


def of_request(item: dict[str, Any], decoded: Optional[dict[str, Any]], lane: Optional[dict[str, Any]]) \
        -> list[dict[str, Any]]:
    """The owned entries of a request: those whose leader or group pid is in its process tree, or that belong to its
    lane (the lane the request joins to, by the lane rule below)."""
    pids = tree_pids(item)
    found: list[dict[str, Any]] = []
    for entry in entries(decoded):
        if (entry["pid"] in pids or entry["pgid"] in pids) or (lane is not None and in_lane(entry, lane)):
            found.append(entry)
    return found


def in_lane(entry: dict[str, Any], lane: dict[str, Any]) -> bool:
    """An entry belongs to a lane when its lane is the lane's id and its directory is inside the lane's worktree."""
    return entry.get("lane") == lane["id"] and _lanefather.inside(entry.get("cwd"), lane.get("worktree"))


def of_lane(lane: dict[str, Any], decoded: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    return [entry for entry in entries(decoded) if in_lane(entry, lane)]


def lease_of(entry: dict[str, Any], items: list[dict[str, Any]]) -> Optional[int]:
    """The request (its index) whose process tree holds the entry's leader or group, None where none does."""
    for index, item in enumerate(items):
        pids = tree_pids(item)
        if entry.get("pid") in pids or (entry.get("pgid") is not None and entry.get("pgid") in pids):
            return index
    return None


def lane_of(entry: dict[str, Any], lanes_decoded: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """The lane an entry belongs to, by the lane rule; None where no project's lane takes it."""
    return next((lane for lane in _lanefather.lanes(lanes_decoded) if in_lane(entry, lane)), None)


def ttl_text(entry: dict[str, Any], elapsed: float = 0.0) -> str:
    """The TTL left as a duration (`1h12m`), `expired` once it passed; a keep-for-session entry without a TTL `—`."""
    if entry.get("ttl") is None:
        return "—"
    left = float(entry.get("ttl_left") or 0) - elapsed
    return minutes(left) if left > 0 else "expired"


def elapsed(decoded: Optional[dict[str, Any]], moment: Optional[datetime.datetime]) -> float:
    """Seconds since the snapshot was taken, so ages tick between polls."""
    taken = (decoded or {}).get("time")
    if moment is None or not isinstance(taken, datetime.datetime):
        return 0.0
    return max(0.0, (moment - taken).total_seconds())
