"""Ending an entry: find its processes by its recorded process group, verify who they are, then SIGTERM, a grace
period and SIGKILL.

Which processes belong to an entry:
- its leader (the command junkfather started or the pid it adopted), when the pid still has the recorded start
  time and command (same command line, or the same program when a process renamed its arguments; or the command its
  supervisor last recorded for it in `known_seen`); `--force` skips the command check, never the start time;
- with a verified leader of its own group, every process in that group: a live leader pins the group id;
- without a verified leader, only the processes recorded as `known` members (pid and start time, refreshed while
  the entry runs, with the command `known_seen` recorded for each), never the group id alone: a group id outlives
  its leader and can be taken again. A leader refused for its start time or command is never one of them, though
  `known` lists it;
- the descendants (by parent pid in the same table) of all of these, for the children that left the group.

Never a member: a process with a terminal or an agent session (Claude Code, Codex), nor anything below one (a
tmux server's panes and the agents in them), another user's process, pid 1, junkfather itself or its ancestors.
Each signal goes to a pid whose start time and command were just read again, so a reused pid is never signalled
(a start time has one-second granularity: the command is the second factor); a `ps` that cannot be read stops the
reap as `failed` and the entry keeps running.
"""
from __future__ import annotations

import contextlib
import os
import signal
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from . import entry as _entry
from . import events as _events
from . import procs as _procs
from .state import Entry, Store

GRACE = 10.0  # seconds between SIGTERM and SIGKILL
ROUNDS = 3  # reads of the entry's members: a process forked during the grace period is found by the next one
STOPPED = ("stopped", "killed", "gone")


def command_matches(now: str, seen: str) -> bool:
    if now == seen:
        return True
    first = lambda text: os.path.basename((text.split() or [""])[0])  # noqa: E731
    return bool(first(now)) and first(now) == first(seen)


def protected(table: _procs.Table) -> Set[int]:
    me = os.getpid()
    return {0, 1, me, *table.ancestors(me)}


def unsafe(proc: _procs.Proc) -> bool:
    """A process no reap may touch, nor anything below it: a terminal's or an agent session."""
    from .owner import CLAUDE, CODEX
    return bool(proc.tty) or bool(CLAUDE.search(proc.cmd) or CODEX.search(proc.cmd))


def seen_commands(entry: Entry) -> Dict[Tuple[int, str], str]:
    """(pid, start) -> the command last recorded for that member; empty for an entry written before `known_seen`."""
    seen: Dict[Tuple[int, str], str] = {}
    for item in entry.get("known_seen") or []:
        with contextlib.suppress(TypeError, ValueError):
            pid, start, cmd = item
            seen[(int(pid), _procs.norm(start))] = str(cmd)
    return seen


def members(entry: Entry, table: _procs.Table, uid: Optional[int] = None, force: bool = False, ours: bool = False
            ) -> Tuple[List[_procs.Proc], str]:
    """The entry's processes that may be signalled, and how they were found: "leader", "known", or why there are
    none ("gone", "pid reused", "command changed").

    The second factor after pid and start is the command: the leader's must match `seen` (or what `known_seen`
    last recorded for it), and a known member's what `known_seen` recorded for it (an entry without `known_seen`
    has pid and start only). `force` skips the command checks, never the start time. `ours`: the caller is the
    leader's parent and has not waited for it, so its pid cannot have been reused; only its command check is
    skipped. A leader refused for its command or start is never found again through `known`."""
    uid = os.getuid() if uid is None else uid
    leader = table.get(entry.get("pid"))
    found: Dict[int, _procs.Proc] = {}
    seen = seen_commands(entry)
    refused: Set[int] = set()
    how = "gone"
    if leader is not None and leader.start == _procs.norm(entry.get("start") or ""):
        last = seen.get((leader.pid, leader.start))
        if force or ours or command_matches(leader.cmd, entry.get("seen") or "") \
                or (last is not None and command_matches(leader.cmd, last)):
            found[leader.pid] = leader
            how = "leader"
            if entry.get("group") and leader.pgid == leader.pid == entry.get("pgid"):
                for proc in table.group(leader.pgid):
                    found[proc.pid] = proc
        else:
            how = "command changed"
            refused.add(leader.pid)
    elif leader is not None:
        how = "pid reused"
        refused.add(leader.pid)
    if not found:
        known = {(int(pid), _procs.norm(start)) for pid, start in entry.get("known") or []}
        for proc in table.procs.values():
            if (proc.pid, proc.start) not in known or proc.pid in refused:
                continue
            recorded = seen.get((proc.pid, proc.start))
            if force or recorded is None or command_matches(proc.cmd, recorded):
                found[proc.pid] = proc
        if found:
            how = "known"
    for pid in list(found):
        for proc in table.descendants(pid, prune=unsafe):
            found.setdefault(proc.pid, proc)
    skip = protected(table)
    excluded = {p.pid for p in found.values() if unsafe(p)}
    for pid in excluded:
        excluded = excluded | {p.pid for p in table.descendants(pid)}
    chosen = [p for p in found.values() if p.uid == uid and p.pid not in skip and p.pid not in excluded]
    return sorted(chosen, key=lambda p: p.pid), how


def stop(targets: List[_procs.Proc], grace: float = GRACE,
         look: Optional[Callable[[List[int]], Dict[int, Tuple[str, str]]]] = None,
         kill: Callable[[int, int], None] = os.kill, now: Callable[[], float] = time.monotonic,
         pause: Callable[[float], None] = time.sleep) -> Dict[str, Any]:
    """SIGTERM to each target whose start time and command are unchanged (`look`: pid -> (start, command)), SIGKILL
    to those still there after `grace` seconds.
    Returns {"result": stopped|killed|gone|failed, "signalled": [...], "left": [...]}; failed also when the process
    table cannot be read, since then nothing says the targets ended."""
    look = look or (lambda pids: _procs.identities(pids, command=True))
    expected = {p.pid: p for p in targets}

    def remaining(pids: List[int]) -> List[int]:
        """Those of `pids` still the process that was read: same start time, and the same command or program."""
        here = look(pids)
        return [pid for pid in pids if pid in here and here[pid][0] == expected[pid].start
                and command_matches(here[pid][1], expected[pid].cmd)]

    signalled: List[int] = []
    left: List[int] = list(expected)
    try:
        live = remaining(list(expected))
        if not live:
            return {"result": "gone", "signalled": [], "left": []}
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
    except _procs.ProcError as error:
        return {"result": "failed", "signalled": signalled, "left": left, "error": str(error)}
    return {"result": "failed" if left else "killed", "signalled": signalled, "left": left}


def reap(store: Store, entry_id: str, reason: str, grace: float = GRACE, dry_run: bool = False,
         force: bool = False, table: Optional[_procs.Table] = None, stopper=stop,
         actor: Optional[Dict[str, Any]] = None, reader=None, report_failure: bool = True) -> Dict[str, Any]:
    """End one running entry. Returns {"id", "result", "reason", "pids", "how"}; result "skipped" when the entry
    is not running (someone else reaped it, it exited, it is still starting), "would-stop" in a dry run. `table`
    serves the first read only; every later round reads the machine again (`reader`). `report_failure` False: a
    failure is recorded on the entry (`last_error`) but writes no `entry.reap_failed` event (a retry in a streak)."""
    reader = reader or _procs.Table.read
    entry = store.load(entry_id)
    if entry is None:
        return {"id": entry_id, "result": "unknown", "reason": reason, "pids": [], "how": ""}
    if entry.get("mode") == "resource":
        if entry.get("state") != _entry.RUNNING:
            return {"id": entry_id, "result": "skipped", "reason": f"already {entry.get('state')}", "pids": [],
                    "how": ""}
        from . import resources
        return resources.reap(store, entry, reason, grace, dry_run, actor)
    if entry.get("state") == _entry.STARTING or (entry.get("state") == _entry.RUNNING and not entry.get("pid")):
        return {"id": entry_id, "result": "skipped", "reason": "still starting", "pids": [], "how": ""}
    if entry.get("state") != _entry.RUNNING:
        return {"id": entry_id, "result": "skipped", "reason": f"already {entry.get('state')}", "pids": [], "how": ""}
    try:
        table = table or reader()
    except _procs.ProcError as error:
        unread = {"reason": reason, "left": [], "error": str(error)}
        if not dry_run:  # like any failed reap: last_error on the entry, and the event that opens a streak
            _entry.noted_error(store, entry_id, unread)
            if report_failure:
                _events.write(store.root, "entry.reap_failed", _events.subject(entry), actor or _events.caller(),
                              {**unread, "result": "failed", "how": "", "signals": [], "pids": []}, "error")
        return {"id": entry_id, "result": "failed", "reason": f"{reason}; {error}", "pids": [], "how": ""}
    targets, how = members(entry, table, force=force)
    if dry_run:
        return {"id": entry_id, "result": "would-stop" if targets else "gone", "reason": reason,
                "pids": [p.pid for p in targets], "how": how}
    me = _entry.begin_reap(store, entry_id, reason, table)
    if me is None:
        return {"id": entry_id, "result": "skipped", "reason": "another reaper has it", "pids": [], "how": how}
    pids: List[int] = []
    outcome: Dict[str, Any] = {"result": "gone", "signalled": [], "left": []}
    results: List[str] = []
    for round_ in range(ROUNDS):
        if round_:
            try:
                table = reader()
            except _procs.ProcError as error:
                outcome = {"result": "failed", "signalled": [], "left": pids, "error": str(error)}
                results.append("failed")
                break
            targets, _ = members(entry, table, force=force)
        if not targets:
            break
        pids += [p.pid for p in targets if p.pid not in pids]
        outcome = stopper(targets, grace if round_ == 0 else min(grace, 3.0))
        results.append(outcome["result"])
        if outcome["result"] == "failed":
            break
    result = "failed" if "failed" in results else "killed" if "killed" in results else \
        "stopped" if "stopped" in results else "gone"
    outcome["result"] = result

    _entry.end_reap(store, entry_id, me, result in STOPPED,
                    {"reason": reason, "result": result, "how": how, "pids": pids},
                    {"reason": reason, "left": outcome.get("left"), "error": outcome.get("error")})
    signals = {"stopped": ["TERM"], "killed": ["TERM", "KILL"], "failed": ["TERM", "KILL"]}.get(result, [])
    done = result in STOPPED
    if not done and not report_failure:
        return {"id": entry_id, "result": result, "reason": reason, "pids": pids, "how": how}
    _events.write(store.root, "entry.reaped" if done else "entry.reap_failed", _events.subject(entry),
                  actor or _events.caller(), {"reason": reason, "result": result, "how": how, "signals": signals,
                                              "pids": pids, "left": outcome.get("left") or [],
                                              **({"error": outcome["error"]} if outcome.get("error") else {})},
                  None if done else "error")
    return {"id": entry_id, "result": result, "reason": reason, "pids": pids, "how": how}

