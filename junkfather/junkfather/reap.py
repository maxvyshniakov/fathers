"""Ending an entry: find its processes by its recorded process group, verify who they are, then SIGTERM, a grace
period and SIGKILL.

Which processes belong to an entry:
- its leader (the command junkfather started or the pid it adopted), when the pid still has the recorded start
  time and command (same command line, or the same program when a process renamed its arguments);
- with a verified leader of its own group, every process in that group: a live leader pins the group id;
- without a verified leader, only the processes recorded as `known` members (pid and start time, refreshed while
  the entry runs), never the group id alone: a group id outlives its leader and can be taken again;
- the descendants (by parent pid in the same table) of all of these, for the children that left the group.

Only this user's processes are signalled, never pid 1, junkfather itself or its ancestors, and each signal goes
to a pid whose start time was just read again, so a reused pid is never signalled.
"""
from __future__ import annotations

import contextlib
import os
import signal
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from . import events as _events
from . import procs as _procs
from .state import Entry, Store

GRACE = 10.0  # seconds between SIGTERM and SIGKILL
STOPPED = ("stopped", "killed", "gone")


def command_matches(now: str, seen: str) -> bool:
    if now == seen:
        return True
    first = lambda text: os.path.basename((text.split() or [""])[0])  # noqa: E731
    return bool(first(now)) and first(now) == first(seen)


def protected(table: _procs.Table) -> Set[int]:
    me = os.getpid()
    return {0, 1, me, *table.ancestors(me)}


def members(entry: Entry, table: _procs.Table, uid: Optional[int] = None, force: bool = False
            ) -> Tuple[List[_procs.Proc], str]:
    """The entry's processes that may be signalled, and how they were found: "leader", "known", or why there are
    none ("gone", "pid reused", "command changed")."""
    uid = os.getuid() if uid is None else uid
    leader = table.get(entry.get("pid"))
    found: Dict[int, _procs.Proc] = {}
    how = "gone"
    if leader is not None and leader.start == _procs.norm(entry.get("start") or ""):
        if force or command_matches(leader.cmd, entry.get("seen") or ""):
            found[leader.pid] = leader
            how = "leader"
            if entry.get("group") and leader.pgid == leader.pid == entry.get("pgid"):
                for proc in table.group(leader.pgid):
                    found[proc.pid] = proc
        else:
            how = "command changed"
    elif leader is not None:
        how = "pid reused"
    if not found:
        known = {(int(pid), _procs.norm(start)) for pid, start in entry.get("known") or []}
        for proc in table.procs.values():
            if (proc.pid, proc.start) in known:
                found[proc.pid] = proc
        if found:
            how = "known"
    for pid in list(found):
        for proc in table.descendants(pid):
            found.setdefault(proc.pid, proc)
    skip = protected(table)
    chosen = [p for p in found.values() if p.uid == uid and p.pid not in skip]
    return sorted(chosen, key=lambda p: p.pid), how


def snapshot(entry: Entry, table: _procs.Table) -> List[List[Any]]:
    """The entry's current members as [pid, start] pairs, for `known`."""
    found, _ = members(entry, table)
    return [[p.pid, p.start] for p in found]


def stop(targets: List[_procs.Proc], grace: float = GRACE,
         look: Optional[Callable[[List[int]], Dict[int, str]]] = None,
         kill: Callable[[int, int], None] = os.kill, now: Callable[[], float] = time.monotonic,
         pause: Callable[[float], None] = time.sleep) -> Dict[str, Any]:
    """SIGTERM to each target whose start time is unchanged, SIGKILL to those still there after `grace` seconds.
    Returns {"result": stopped|killed|gone|failed, "signalled": [...], "left": [...]}."""
    look = look or _procs.identities
    expected = {p.pid: p.start for p in targets}

    def remaining(pids: List[int]) -> List[int]:
        here = look(pids)
        return [pid for pid in pids if here.get(pid) == expected[pid]]

    live = remaining(list(expected))
    if not live:
        return {"result": "gone", "signalled": [], "left": []}
    signalled = []
    for pid in live:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            kill(pid, signal.SIGTERM)
            signalled.append(pid)
    deadline = now() + grace
    left = remaining(live)
    while left and now() < deadline:
        pause(0.1)
        left = remaining(left)
    if not left:
        return {"result": "stopped", "signalled": signalled, "left": []}
    for pid in remaining(left):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            kill(pid, signal.SIGKILL)
    deadline = now() + 3
    while left and now() < deadline:
        pause(0.05)
        left = remaining(left)
    return {"result": "failed" if left else "killed", "signalled": signalled, "left": left}


def reap(store: Store, entry_id: str, reason: str, grace: float = GRACE, dry_run: bool = False,
         force: bool = False, table: Optional[_procs.Table] = None, stopper=stop,
         actor: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """End one running entry. Returns {"id", "result", "reason", "pids", "how"}; result "skipped" when the entry
    is not running (someone else reaped it, it exited), "would-stop" in a dry run."""
    entry = store.load(entry_id)
    if entry is None:
        return {"id": entry_id, "result": "unknown", "reason": reason, "pids": [], "how": ""}
    if entry.get("state") not in ("running", "starting"):
        return {"id": entry_id, "result": "skipped", "reason": f"already {entry.get('state')}", "pids": [], "how": ""}
    try:
        table = table or _procs.Table.read()
    except _procs.ProcError as error:
        return {"id": entry_id, "result": "failed", "reason": f"{reason}; {error}", "pids": [], "how": ""}
    targets, how = members(entry, table, force=force)
    pids = [p.pid for p in targets]
    if dry_run:
        return {"id": entry_id, "result": "would-stop" if targets else "gone", "reason": reason, "pids": pids,
                "how": how}

    def mark(e: Entry) -> Optional[bool]:
        if e.get("state") not in ("running", "starting"):
            return False
        e["state"] = "reaping"
        e["reaping"] = {"reason": reason, "at": time.time(), "by": os.getpid()}
        return None

    marked = store.update(entry_id, mark)
    if marked is None or marked.get("state") != "reaping" or (marked.get("reaping") or {}).get("by") != os.getpid():
        return {"id": entry_id, "result": "skipped", "reason": "another reaper has it", "pids": [], "how": how}
    outcome = stopper(targets, grace) if targets else {"result": "gone", "signalled": [], "left": []}

    def finish(e: Entry) -> None:
        done = outcome["result"] in STOPPED
        e["state"] = "reaped" if done else "running"
        e.pop("reaping", None)
        if done:
            e["ended"] = time.time()
            e["end"] = {"reason": reason, "result": outcome["result"], "how": how, "pids": pids}
        else:
            e["last_error"] = {"at": time.time(), "reason": reason, "left": outcome["left"]}

    store.update(entry_id, finish)
    signals = {"stopped": ["TERM"], "killed": ["TERM", "KILL"], "failed": ["TERM", "KILL"]}.get(outcome["result"], [])
    done = outcome["result"] in STOPPED
    _events.write(store.root, "entry.reaped" if done else "entry.reap_failed", _events.subject(entry),
                  actor or _events.caller(), {"reason": reason, "result": outcome["result"], "how": how,
                                              "signals": signals, "pids": pids, "left": outcome["left"]},
                  None if done else "error")
    return {"id": entry_id, "result": outcome["result"], "reason": reason, "pids": pids, "how": how}
