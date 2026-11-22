"""Lanefather as a source, read-only (docs/contracts/lanefather.md): its machine-wide `lanefather status --json`
decoded into the projects and lanes the Lanes page, the lanes summary and DETAILS' `lane` row show, the joins of a
request and a process entry to a lane, and the per-lane summary DETAILS falls back to (`lanefather show ID --json`),
asked only of a Lanefather whose capabilities advertise it. No dependency on Lanefather's storage schema.

A lane is known by its project's state directory and its id, never by its id alone: two projects may each have a
lane `710-speech-paste-phase` (SPEC, Open risks 5)."""
from __future__ import annotations

import datetime
import json
from pathlib import Path
import posixpath
import re
import threading
import time
from typing import Any, Callable, Mapping, Optional, cast

from .. import sources as _sources
from ..events import dashboard_block
from ..model import Gate, Lane, LaneRow, Lanes, LaneTableRow, Project, ProjectRow
from . import decoding as _decoding

TAG = "lane"
CAPABILITIES_VERSIONS = (1,)
DASHBOARD_VERSIONS = (1,)
SNAPSHOT_VERSIONS = (1,)  # `status --json` versions this Godfather reads
SHOW_VERSIONS = (1,)  # `show --json` versions this Godfather reads
Decoded = Optional[Mapping[str, Any]]  # a decoded snapshot as the screen keeps it (Lanes), or none


def unsupported(capabilities: dict[str, Any]) -> Optional[str]:
    """Why a capabilities answer names no status snapshot this Godfather reads, None when it names one."""
    block = dashboard_block(capabilities)
    if capabilities.get("v") not in CAPABILITIES_VERSIONS or block.get("v") not in DASHBOARD_VERSIONS:
        return "no dashboard contract; update lanefather"
    snapshot: Any = block.get("snapshot")
    if not isinstance(snapshot, dict) or not isinstance(cast(dict[str, Any], snapshot).get("argv"), list) \
            or not cast(dict[str, Any], snapshot)["argv"]:
        return "no status snapshot; update lanefather"
    versions: Any = cast(dict[str, Any], snapshot).get("versions")
    if not isinstance(versions, list) or not any(v in SNAPSHOT_VERSIONS for v in cast(list[Any], versions)):
        return "its status snapshot needs a newer godfather"
    return None


def show_supported(capabilities: Optional[dict[str, Any]]) -> bool:
    """Whether the capabilities advertise `show --json` in a version this Godfather reads (`dashboard.show`)."""
    show: Any = dashboard_block(capabilities).get("show")
    versions: Any = cast(dict[str, Any], show).get("versions") if isinstance(show, dict) else None
    return isinstance(versions, list) and any(v in SHOW_VERSIONS for v in cast(list[Any], versions))


def actions(capabilities: Optional[dict[str, Any]]) -> tuple[str, ...]:
    found: Any = dashboard_block(capabilities).get("actions")
    return tuple(str(name) for name in cast(list[Any], found)) if isinstance(found, list) else ()


# The snapshot


def parse_time(value: Any) -> Optional[datetime.datetime]:
    """An RFC 3339 UTC time with or without fractions (`2026-11-12T04:26:00Z`)."""
    if not isinstance(value, str):
        return None
    for form in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.datetime.strptime(value, form).replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            continue
    return None


def _lane(raw: dict[str, Any], project: Project) -> Lane:
    ticket, receipt, review = (_decoding.as_object(raw.get(name)) for name in ("ticket", "receipt", "review"))
    logs = {str(phase): str(path) for phase, path in _decoding.as_object(raw.get("logs")).items()
            if isinstance(path, str)}
    return {
        "id": str(raw["id"]), "key": (project["state_dir"], str(raw["id"])), "project": project["project"],
        "state_dir": project["state_dir"], "repository": project.get("repository"),
        "status": _decoding.text(raw.get("status")) or "?",
        "ticket": {"number": _decoding.text(str(ticket.get("number"))) if ticket.get("number") is not None else None,
                   "url": _decoding.text(ticket.get("url"))},
        "branch": _decoding.text(raw.get("branch")), "base": _decoding.text(raw.get("base")),
        "worktree": _decoding.text(raw.get("worktree")),
        "claims": _decoding.strings(raw.get("claims")), "after": _decoding.strings(raw.get("after")),
        "created": parse_time(raw.get("created")),
        "receipt": {"status": _decoding.text(receipt.get("status")) or "unknown",
                    "tree_match": receipt.get("tree_match") if isinstance(receipt.get("tree_match"), bool) else None,
                    "path": _decoding.text(receipt.get("path"))},
        "post_merge": _decoding.text(raw.get("post_merge")),
        "review": {"status": _decoding.text(review.get("status")) or "not started",
        "open": _decoding.whole(review.get("open")),
                   "findings": _decoding.whole(review.get("findings"))},
        "logs": logs, "outcome": raw.get("outcome")}


def decode(snapshot: Any) -> Lanes:
    """`lanefather status --json` as the Lanes page reads it: {"time", "projects": [...]}, each project with its lanes
    (their `key`, project and state directory set), or its `error`. SnapshotError for another version or shape."""
    if not isinstance(snapshot, dict):
        raise _sources.SnapshotError("lanefather status --json printed no object")
    found = cast(dict[str, Any], snapshot)
    if found.get("v") not in SNAPSHOT_VERSIONS:
        raise _sources.SnapshotError(f"lanefather status --json version {found.get('v')!r} needs a newer godfather")
    projects: Any = found.get("projects")
    if not isinstance(projects, list):
        raise _sources.SnapshotError("lanefather status --json has no projects list")
    decoded: list[Project] = []
    for raw in cast(list[Any], projects):
        item = _decoding.as_object(raw)
        state_dir = _decoding.text(item.get("state_dir"))
        if state_dir is None:  # a project is known by its state directory: without one, nothing of it can be joined
            raise _sources.SnapshotError("a project has no state_dir; update lanefather")
        name = _decoding.text(item.get("project")) or posixpath.basename(state_dir.rstrip("/")) or state_dir
        project: Project = {"project": name, "state_dir": state_dir,
                            "repository": _decoding.text(item.get("repository")),
                            "lanes": []}
        if "error" in item:
            project["error"] = str(item.get("error") or "unreadable")
            decoded.append(project)
            continue
        gate = _decoding.as_object(item.get("gate"))
        cap: Any = item.get("cap")
        found_gate: Optional[Gate] = {"status": _decoding.text(gate.get("status")),
                                      "sha": _decoding.text(gate.get("sha")),
                                      "at": parse_time(gate.get("at"))} if gate else None
        project.update(cap=cap if isinstance(cap, int) else None, active=_decoding.whole(item.get("active")),
                       planned=_decoding.whole(item.get("planned")), gate=found_gate)
        lanes: list[Lane] = []
        for value in _decoding.as_list(item.get("lanes")):
            lane = _decoding.as_object(value)
            if _decoding.text(lane.get("id")) is None:  # never dropped quietly: a renamed field would hide work
                raise _sources.SnapshotError("a lane has no id; update lanefather")
            lanes.append(_lane(lane, project))
        project["lanes"] = lanes
        decoded.append(project)
    return {"time": parse_time(found.get("time")), "projects": decoded}


def projects(decoded: Decoded) -> list[Project]:
    """The decoded snapshot's projects, readable or not (decode() checked them)."""
    return cast(list[Project], (decoded or {}).get("projects") or [])


def lanes(decoded: Decoded) -> list[Lane]:
    """Every lane of every readable project, in the snapshot's order."""
    return [lane for project in projects(decoded) for lane in project["lanes"]]


def find(decoded: Decoded, key: Any) -> Optional[Lane]:
    return next((lane for lane in lanes(decoded) if lane["key"] == tuple(key or ())), None)


def reviewing(lane: Lane) -> bool:
    """A lane whose review runs or has findings not resolved yet."""
    return lane["review"]["status"] == "running" or lane["review"]["open"] > 0


def summary(decoded: Decoded) -> dict[str, Any]:
    """The lanes summary in SLOTS' bottom border: lanes active and planned, reviews, and whether any project's newest
    batch gate is red."""
    readable = [project for project in projects(decoded) if "error" not in project]
    found = lanes(decoded)
    return {"active": sum(project.get("active", 0) for project in readable),
            "planned": sum(project.get("planned", 0) for project in readable),
            "reviews": sum(1 for lane in found if reviewing(lane)),
            "red": any((project.get("gate") or {}).get("status") == "red" for project in readable)}


# The joins (SPEC 19): never by a lane id alone across projects

def same_dir(one: Any, other: Any) -> bool:
    return isinstance(one, str) and isinstance(other, str) and bool(one) and \
        posixpath.normpath(one) == posixpath.normpath(other)


def inside(path: Any, root: Any) -> bool:
    """Whether `path` is `root` or a directory under it (both absolute)."""
    if not (isinstance(path, str) and isinstance(root, str) and path.startswith("/") and root.startswith("/")):
        return False
    path, root = posixpath.normpath(path), posixpath.normpath(root)
    return path == root or path.startswith(root.rstrip("/") + "/")


def lane_of(item: Mapping[str, Any], decoded: Decoded, cwd: Optional[str] = None) -> Optional[Lane]:
    """The lane a request runs for: its `meta.lane` in the project whose state directory its `meta.lanefather.state`
    names; with only `meta.lane`, the lane of that id whose worktree holds the request's directory (its working
    directory, else the holder's `cwd`). None otherwise."""
    context: dict[str, Any] = item.get("context") or {}
    lane_id, state_dir = context.get("lane"), context.get("lanefather.state")
    if not lane_id or decoded is None:
        return None
    candidates = [lane for lane in lanes(decoded) if lane["id"] == lane_id]
    if state_dir:
        return next((lane for lane in candidates if same_dir(lane["state_dir"], state_dir)), None)
    directory = item.get("workdir") or cwd
    return next((lane for lane in candidates if inside(directory, lane["worktree"])), None)


def leases_of(lane: Mapping[str, Any], items: list[dict[str, Any]]) -> list[int]:
    """The requests (their indexes) that run for `lane`, by the same join."""
    found: list[int] = []
    decoded: Mapping[str, Any] = {"projects": [{"lanes": [lane]}]}
    for index, item in enumerate(items):
        joined = lane_of(item, decoded)
        if joined is not None and joined["key"] == lane["key"]:
            found.append(index)
    return found


# The Lanes page's rows

def page_rows(decoded: Decoded) -> list[LaneTableRow]:
    """LANES' rows: each project an unselectable row (`Straylight · 3 of 4 lanes`, or its error), its lanes under it."""
    found: list[LaneTableRow] = []
    for project in projects(decoded):
        heading: ProjectRow = {"kind": "project", "project": project["project"], "state_dir": project["state_dir"],
                               "error": project.get("error"), "active": project.get("active", 0),
                               "cap": project.get("cap"), "count": len(project["lanes"])}
        found.append(heading)
        for lane in project["lanes"]:
            row: LaneRow = {**lane, "kind": "lane"}
            found.append(row)
    return found


def stage(lane: Mapping[str, Any]) -> str:
    """Where a lane is: its status, and after a landing its post-merge check."""
    return lane["status"] + (f" · post-merge {lane['post_merge']}" if lane.get("post_merge") else "")


def receipt_text(lane: Mapping[str, Any]) -> str:
    receipt = lane["receipt"]
    match = receipt.get("tree_match")
    return receipt["status"] + (" · stale" if match is False else " · current" if match else "")


def review_text(lane: Mapping[str, Any]) -> str:
    review = lane["review"]
    return review["status"] + (f" · {review['open']} open" if review["open"] else "")


def latest_log(lane: Mapping[str, Any]) -> Optional[tuple[str, str]]:
    """The newest run log Lanefather lists for the lane: (phase, path), by its file name's time stamp."""
    logs: dict[str, str] = lane.get("logs") or {}
    if not logs:
        return None
    return max(logs.items(), key=lambda pair: (Path(pair[1]).name.rsplit("-", 1)[-1], pair[0]))


def as_summary(lane: Mapping[str, Any]) -> dict[str, Any]:
    """A status lane in the shape of `show --json`, which DETAILS' lane facts read."""
    receipt, review = lane["receipt"], lane["review"]
    ticket = lane["ticket"]
    return {"version": 1, "id": lane["id"], "project": lane["project"], "status": lane["status"],
            "repository": None, "ticket": {"number": ticket.get("number"), "url": ticket.get("url")},
            "branch": lane["branch"], "base": lane["base"], "worktree": lane["worktree"], "claims": lane["claims"],
            "after": lane["after"],
            "checks": {"status": receipt["status"], "current": False if receipt.get("tree_match") is False else None,
                       "tree_match": receipt.get("tree_match")},
            "review": {"status": review["status"], "findings": review["findings"], "unresolved": review["open"]}}


# `lanefather show ID --json`: DETAILS' lane summary where the status snapshot has none for the request

CACHE_SECONDS = 5.0
CACHE_LIMIT = 128
TOO_OLD = "lanefather too old"  # its capabilities advertise no `show`


class ShowCache:
    """Thread-safe bounded TTL cache for `lanefather show ID --json` replies."""

    def __init__(self, ttl: float = CACHE_SECONDS, limit: int = CACHE_LIMIT,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl = ttl
        self.limit = limit
        self.clock = clock
        self._lock = threading.Lock()
        self._entries: dict[tuple[str, str, str], tuple[float, dict[str, Any]]] = {}

    def get(self, key: tuple[str, str, str]) -> Optional[dict[str, Any]]:
        with self._lock:
            cached = self._entries.get(key)
            if cached is None:
                return None
            if self.clock() - cached[0] >= self.ttl:
                self._entries.pop(key, None)
                return None
            return dict(cached[1])

    def put(self, key: tuple[str, str, str], value: dict[str, Any]) -> None:
        with self._lock:
            if self.limit <= 0:
                return
            if key in self._entries:
                self._entries.pop(key, None)
            elif len(self._entries) >= self.limit:
                self._entries.pop(next(iter(self._entries)))
            self._entries[key] = (self.clock(), dict(value))

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


_DEFAULT_CACHE = ShowCache()


def fetch(
    context: dict[str, str],
    cwd: str = "",
    capabilities: Optional[dict[str, Any]] = None,
    executable: Optional[str] = None,
    runner: Optional[_sources.Runner] = None,
    cache: Optional[ShowCache] = None,
) -> dict[str, Any]:
    """The lane summary of a request's lane, asked only of a Lanefather whose capabilities advertise `show` (else
    `lanefather too old`); errors come back as {"error": why}."""
    lane, directory = context.get('lane', ''), context.get('lanefather.state', '')
    location = directory or cwd
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', lane) or not Path(location).is_absolute():
        return {'error': 'lane reference missing'}
    # The command location is not part of identity: an already cached response survives a transient PATH change,
    # so the cache is read before Lanefather is looked up or its capabilities checked. Reusing it asks nothing.
    store = cache if cache is not None else _DEFAULT_CACHE
    key = (location, lane, 'state-v1' if directory else 'project-v1')
    cached = store.get(key)
    if cached is not None:
        return cached
    active_runner = runner if runner is not None else _sources.default_runner()
    command = executable or active_runner.which('lanefather') or ''
    if not command:
        return {'error': 'lanefather not installed'}
    if not show_supported(capabilities):
        return {'error': TOO_OLD}
    result: dict[str, Any]
    try:
        reference = ['--state-dir', directory] if directory else ['--project-dir', cwd]
        reply = active_runner.run('lanefather', ['show', lane, *reference, '--json'], 1.5, command)
        if reply.code in (124, 126):  # a timeout, or it could not be started
            raise OSError(reply.stderr)
        if reply.code or len(reply.stdout) > 65536:
            result = {'error': 'lanefather query failed'}
        else:
            raw = json.loads(reply.stdout)
            data = cast(dict[str, Any], raw) if isinstance(raw, dict) else {}
            if data.get('version') not in SHOW_VERSIONS or data.get('id') != lane:
                result = {'error': 'unsupported lane summary'}
            elif (any(not isinstance(data.get(key, ''), (str, type(None)))
                      for key in ('project', 'status', 'branch', 'base'))
                  or any(not isinstance(data.get(key, {}), dict) for key in ('ticket', 'checks', 'review'))
                  or any(not isinstance(data.get(key, []), list) for key in ('claims', 'after'))):
                result = {'error': 'invalid lane summary'}
            else:
                result = data
    except (OSError, ValueError):
        result = {'error': 'lanefather unavailable'}
    store.put(key, result)
    return result
