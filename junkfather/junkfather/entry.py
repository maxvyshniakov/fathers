"""The entry lifecycle: its states and every allowed transition (docs/reference.md, Entry).

  starting --started--> running --begin_reap--> reaping --end_reap--> reaped
     |                  |  ^  ^                    |  |
     +--failed--> failed|  |  +----end_reap--------+  +--release_stuck--> running (its reaper died)
                        |  +--revived-- gone (the supervisor's child still runs)
                        +--exited--> exited, +--gone--> gone

Process entries and resource entries (`mode: "resource"`) go through the same transitions. Each one reads, checks and
writes the entry under the store's lock, and returns the entry as written, or None when its precondition no longer
held (someone else moved the entry first), in which case nothing is written. No other module writes a state.
"""
from __future__ import annotations

import os
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import procs as _procs
from .state import Entry, Store, VERSION

STARTING = "starting"
RUNNING = "running"
REAPING = "reaping"
EXITED = "exited"
REAPED = "reaped"
GONE = "gone"
FAILED = "failed"

STATES = (STARTING, RUNNING, REAPING, EXITED, REAPED, GONE, FAILED)
LIVE = (RUNNING, REAPING)  # a resource is held: no second entry may own it
OPEN = (RUNNING, STARTING)  # reap, hand and extend take it
ACTIVE = (RUNNING, STARTING, REAPING)  # list and status show it; the supervisor keeps watching it


def new(store: Store, argv: List[str], ttl: float, keep: str, label: Optional[str], owner: Dict[str, Any],
        mode: str, cwd: str, now: Optional[float] = None) -> Entry:
    """A starting entry, not yet saved. `owner["claim_note"]` moves to the entry."""
    now = time.time() if now is None else now
    return {"v": VERSION, "id": store.new_id(), "label": label, "argv": argv, "cwd": cwd, "created": now,
            "ttl": ttl, "expires": now + ttl, "keep": keep, "owner": owner, "mode": mode, "state": STARTING,
            "pid": None, "pgid": None, "start": None, "seen": None, "group": True, "known": [], "known_seen": [],
            "handed": [],
            "supervisor": None, "log": None, "claim_note": owner.pop("claim_note", "")}


def known_fields(found: Sequence[_procs.Proc]) -> Dict[str, List[List[Any]]]:
    """An entry's `known` members (pid, start) and, beside them, the command each showed (`known_seen`)."""
    return {"known": [[p.pid, p.start] for p in found], "known_seen": [[p.pid, p.start, p.cmd] for p in found]}


def _move(store: Store, entry_id: str, allowed: Callable[[Entry], bool], change: Callable[[Entry], None]
          ) -> Optional[Entry]:
    """Apply `change` when `allowed` holds for the entry as read under the lock; the entry, or None."""
    applied = []

    def step(e: Entry) -> Optional[bool]:
        if not allowed(e):
            return False
        change(e)
        applied.append(True)
        return None

    entry = store.update(entry_id, step)
    return entry if applied else None


def _from(*states: str) -> Callable[[Entry], bool]:
    return lambda e: e.get("state") in states


# Starting

def save_starting(store: Store, entry: Entry) -> None:
    """Record a new entry (from `new`) before its command starts."""
    with store.lock():
        store.save(entry)


def started(store: Store, entry_id: str, leader: _procs.Proc, supervisor: Dict[str, Any]) -> Optional[Entry]:
    """starting -> running, with the leader's identity: it leads a group of its own, and is its only known member."""
    return _move(store, entry_id, _from(STARTING), lambda e: e.update(
        state=RUNNING, pid=leader.pid, pgid=leader.pid, start=leader.start, seen=leader.cmd, supervisor=supervisor,
        **known_fields([leader])))


def failed(store: Store, entry_id: str, error: str, now: Optional[float] = None, start_error: bool = False
           ) -> Optional[Entry]:
    """starting -> failed: its command could not start, its identity could not be read, or it never started.
    `start_error`: the command's own start failed, whose error replaces a failure the sweep recorded meanwhile
    ("never started"), as it always did."""
    at = time.time() if now is None else now
    allowed = _from(STARTING, FAILED) if start_error else _from(STARTING)
    return _move(store, entry_id, allowed, lambda e: e.update(state=FAILED, ended=at, error=error))


def adopted(store: Store, entry: Entry, leader: _procs.Proc, members: Sequence[_procs.Proc]) -> Entry:
    """A new entry (from `new`) for a process that already runs, saved as running: its group when it leads one."""
    entry.update(state=RUNNING, pid=leader.pid, pgid=leader.pgid, start=leader.start, seen=leader.cmd,
                 group=leader.pgid == leader.pid, **known_fields(members))
    with store.lock():
        store.save(entry)
    return entry


def resource_owned(store: Store, entry: Entry, resource: Dict[str, Any], holds: Callable[[Entry], bool]
                   ) -> Tuple[Optional[Entry], Optional[str]]:
    """A new entry (from `new`) for a resource that runs now, saved as running; refused (None and the id of the
    holder) when a live entry holds that resource already."""
    entry.update(state=RUNNING, group=False, resource=resource)
    with store.lock():
        holder = next((e["id"] for e in store.entries() if e.get("state") in LIVE and holds(e)), None)
        if holder:
            return None, holder
        store.save(entry)
    return entry, None


# Running

def refreshed(store: Store, entry_id: str, members_of: Callable[[Entry], List[_procs.Proc]],
              code: Optional[int], now: Optional[float] = None) -> Tuple[Optional[Entry], bool, int]:
    """The supervisor's refresh of a running entry: its `known` members as `members_of` finds them now; with none
    left and its command's exit `code` known, running -> exited. Returns (the entry, whether it exited, how many
    members remain); the entry is None when it was not running or nothing was written."""
    result = {"done": False, "left": 0}

    def step(e: Entry) -> Optional[bool]:
        if e.get("state") != RUNNING:
            return False
        members = members_of(e)
        result["left"] = len(members)
        if members:
            e.update(known_fields(members))
            return None
        if code is None:
            return False
        e.update(state=EXITED, ended=time.time() if now is None else now, exit=code, known=[], known_seen=[])
        result["done"] = True
        return None

    entry = store.update(entry_id, step)
    return entry, bool(result["done"]), int(result["left"])


def known(store: Store, entry_id: str, members: Sequence[_procs.Proc]) -> Optional[Entry]:
    """A running entry's `known` members, refreshed (by a sweep when no supervisor does it)."""
    return _move(store, entry_id, _from(RUNNING), lambda e: e.update(known_fields(members)))


def gone(store: Store, entry_id: str, now: Optional[float] = None) -> Optional[Entry]:
    """running -> gone: nothing of it runs any more (a process entry also forgets its known members)."""
    at = time.time() if now is None else now

    def change(e: Entry) -> None:
        if e.get("mode") == "resource":
            e.update(state=GONE, ended=at)
        else:
            e.update(state=GONE, ended=at, known=[])
    return _move(store, entry_id, _from(RUNNING), change)


def revived(store: Store, entry_id: str) -> Optional[Entry]:
    """gone -> running: a sweep took it for ended while the supervisor's own child still runs."""
    return _move(store, entry_id, _from(GONE), lambda e: e.update(state=RUNNING, ended=None))


def extended(store: Store, entry_id: str, ttl: float, now: Optional[float] = None) -> Optional[Entry]:
    """A new TTL from now, for a running or starting entry."""
    at = time.time() if now is None else now
    return _move(store, entry_id, _from(*OPEN), lambda e: e.update(ttl=ttl, expires=at + ttl))


def handed(store: Store, entry_id: str, target: Dict[str, Any], now: Optional[float] = None
           ) -> Optional[Tuple[Entry, Dict[str, Any]]]:
    """A running or starting entry now belongs to a copy of `target` (kept for that owner), which takes the entry's
    lane when it has none; `handed` records the move. Returns the entry (its new owner in `owner`) and the previous
    owner."""
    at = time.time() if now is None else now
    before: Dict[str, Any] = {}

    def change(e: Entry) -> None:
        before.update(e.get("owner") or {})
        owner = {**target, "lane": target.get("lane") or before.get("lane")}
        e.setdefault("handed", []).append({"at": at, "from": dict(before), "to": dict(owner)})
        e["owner"], e["keep"] = owner, "owner"
    entry = _move(store, entry_id, _from(*OPEN), change)
    return None if entry is None else (entry, before)


def noted_error(store: Store, entry_id: str, error: Dict[str, Any], now: Optional[float] = None) -> Optional[Entry]:
    """A running entry's `last_error`: a reap that could not start (an unreadable process table)."""
    at = time.time() if now is None else now
    return _move(store, entry_id, _from(RUNNING), lambda e: e.update(last_error={"at": at, **error}))


# Reaping

def begin_reap(store: Store, entry_id: str, reason: str, table: Optional[_procs.Table] = None) -> Optional[int]:
    """running -> reaping, held by this process; its pid, or None when the entry no longer runs or another reaper
    has it."""
    me = os.getpid()
    if table is None:
        try:
            table = _procs.Table.read()
        except _procs.ProcError:
            table = None
    mine = ((table.get(me) if table is not None else None) or _procs.Proc(me, 0, 0, 0, "", "", "")).start
    marked = _move(store, entry_id, _from(RUNNING), lambda e: e.update(
        state=REAPING, reaping={"reason": reason, "at": time.time(), "by": me, "by_start": mine}))
    if marked is None or (marked.get("reaping") or {}).get("by") != me:
        return None
    return me


def end_reap(store: Store, entry_id: str, me: int, done: bool, end: Dict[str, Any], error: Dict[str, Any]
             ) -> Optional[Entry]:
    """reaping -> reaped (with `end`) when done, else back to running (with `last_error`); only for the reaper that
    holds it."""
    def change(e: Entry) -> None:
        e["state"] = REAPED if done else RUNNING
        e.pop("reaping", None)
        if done:
            e["ended"] = time.time()
            e["end"] = end
        else:
            e["last_error"] = {"at": time.time(), **error}
    return _move(store, entry_id, lambda e: e.get("state") == REAPING and (e.get("reaping") or {}).get("by") == me,
                 change)


def release_stuck(store: Store, entry_id: str) -> Optional[Entry]:
    """reaping -> running: its reaper died before it finished (the sweep decides that)."""
    return _move(store, entry_id, _from(REAPING), lambda e: e.update(state=RUNNING))
