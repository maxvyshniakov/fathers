"""Unowned processes of a Claude Code session that is gone (fathers#168). The sweep stops one only when it is certain
both that the process came from a session and that the session is gone; anything less stays report-only, with the
reason in its report item (`session`).

The link is the process's environment. Claude Code gives each Bash call CLAUDE_CODE_SESSION_ID and CLAUDE_PID (its
own process); a shell's children inherit both and keep them after launchd adopts them, which the parent chain does
not survive. procs.environments reads them (`ps -E` on macOS, /proc on Linux). Linked: each top process of the report
item (the reported pid and the others of its group) names one and the same session, and nothing in its tree whose
environment can be read names another. A process without the variable, or whose environment cannot be read (Apple's
own programs hide theirs), has no link and is reported as before.

Gone, all of:
- Claude Code's session registry can be read, and no record in it names the session with a running process (a
  resumed session runs again under a new process);
- the process CLAUDE_PID names may no longer be the session's: it runs no more, or the pid now runs a process that
  started after the linked one (a reused pid). A process there that started no later, or one whose start cannot be
  read, may be the session's own, and keeps the item;
- and one proof: the SessionEnd hook recorded the session's end (`ended/<session>.json` in the state directory, kept
  SESSION_END_KEEP), the CLAUDE_PID process is gone as above, or the registry names the session and the process of
  every such record is gone by pid and start time.

Never stopped, whatever the session: a tree that holds a process of a Slotfather lease or runs below one (any lease:
the sweep holds none), and anything while Slotfather's leases cannot be read; a tree that holds a member, the group or
the supervisor of an active Spawnfather entry (read again right before the stop); an agent-browser daemon, whose
browser session another agent may use; and what the sweep's report already spares (safety.py: a terminal's process,
an agent session, Spawnfather and its ancestors, another user's). The stop is reap.stop's: SIGTERM only to a pid
whose start time and command were read again, SIGKILL after the grace.
"""
from __future__ import annotations

import os
from pathlib import Path
import time
from typing import Any, Callable, Dict, List, Mapping, NamedTuple, Optional, Sequence, Set, Tuple

from . import browser as _browser
from . import entry as _entry
from . import events as _events
from . import owner as _owner
from . import procs as _procs
from . import reap as _reap
from . import safety as _safety
from .defaults import SESSION_END_KEEP
from .state import Store, as_dict, as_list, read_json, write_atomic

SESSION = "CLAUDE_CODE_SESSION_ID"
SESSION_PID = "CLAUDE_PID"
Environment = Optional[Dict[str, List[str]]]
Environments = Callable[[Mapping[int, str]], Dict[int, Environment]]


# The SessionEnd record

def ended_dir(store: Store) -> Path:
    return store.root / "ended"


def _safe(session: Any) -> bool:
    return isinstance(session, str) and bool(_owner.SAFE_NAME.match(session)) and not session.startswith(".")


def record_end(store: Store, session: Any, reason: Any, now: Optional[float] = None) -> Optional[Path]:
    """What the SessionEnd hook writes: the session ended, when and why. None for an id that cannot name a file."""
    if not _safe(session):
        return None
    path = ended_dir(store) / f"{session}.json"
    write_atomic(path, {"v": 1, "session": session, "at": time.time() if now is None else now,
                        "reason": reason if isinstance(reason, str) else None})
    return path


def ended(store: Store, session: str) -> Optional[Dict[str, Any]]:
    """The SessionEnd record of `session`, or None."""
    if not _safe(session):
        return None
    data = as_dict(read_json(ended_dir(store) / f"{session}.json"))
    return data if data.get("session") == session else None


def prune(store: Store, now: float) -> None:
    """Drop the SessionEnd records older than SESSION_END_KEEP (by the time each records)."""
    directory = ended_dir(store)
    try:
        names = os.listdir(directory)
    except OSError:
        return
    for name in names:
        path = directory / name
        at = as_dict(read_json(path)).get("at")
        try:
            when = float(at) if isinstance(at, (int, float)) else path.stat().st_mtime
            if now - when > SESSION_END_KEEP:
                path.unlink()
        except OSError:
            pass


# Who holds a process

class Holders(NamedTuple):
    """What the sweep must never stop: the processes and groups of active entries, and every process of a Slotfather
    lease with what runs below it (None when the leases cannot be read, and why)."""
    entries: Set[int]
    groups: Set[int]
    leases: Optional[Set[int]]
    leases_why: str = ""


def entry_holds(store: Store, table: _procs.Table) -> Tuple[Set[int], Set[int]]:
    """The pids and process groups active entries name: their members as reap finds them, and every pid, group,
    known member and supervisor their records name (without checking start times: here more is safer)."""
    pids: Set[int] = set()
    groups: Set[int] = set()
    for entry in store.entries():
        if entry.get("state") not in _entry.ACTIVE or entry.get("mode") == "resource":
            continue
        pids.update(p.pid for p in _reap.members(entry, table)[0])
        for value in (entry.get("pid"), as_dict(entry.get("supervisor")).get("pid")):
            if isinstance(value, int):
                pids.add(value)
        for known in as_list(entry.get("known")):
            first = as_list(known)[:1]
            if first and isinstance(first[0], int):
                pids.add(first[0])
        group = entry.get("pgid")
        if isinstance(group, int) and group > 1:
            groups.add(group)
    return pids, groups


def lease_holds(queue: Sequence[Mapping[str, Any]], table: _procs.Table, claimed: Sequence[int] = ()) -> Set[int]:
    """Every pid a Slotfather lease names (its holder and its tree, verified or not), the pids `claimed` for a lease
    elsewhere (cpu.lease_rule, the agent-browser daemons a lease's process started), and everything below them."""
    pids: Set[int] = {int(p) for p in claimed}
    for lease in queue:
        for value in [lease.get("pid"), *as_list(lease.get("tree"))]:
            if isinstance(value, int) and not isinstance(value, bool) and value > 1:
                pids.add(value)
    return pids | {below.pid for pid in list(pids) for below in table.descendants(pid)}


# The decision

def tops_of(item: Mapping[str, Any]) -> List[int]:
    return [int(item["pid"]), *(int(p) for p in as_list(item.get("also")))]


def link(item: Mapping[str, Any], envs: Mapping[int, Environment]) -> Tuple[Optional[str], Optional[str]]:
    """(session, None) when the report item's processes name that one Claude Code session; (None, why) when they
    name one but not certainly; (None, None) when none of them names a session."""
    tops = tops_of(item)
    named: Set[str] = set()
    for pid in tops:
        named.update((envs.get(pid) or {}).get(SESSION) or [])
    if not named:
        return None, None
    if len(named) > 1:
        return None, f"its processes name {len(named)} sessions"
    (session,) = named
    if not _safe(session):
        return None, "the session id it names is not one"
    for pid in tops:
        env = envs.get(pid)
        if env is None:
            return None, f"pid {pid}'s environment cannot be read"
        if set(env.get(SESSION) or []) != {session}:
            return None, f"pid {pid} names no session"
    for pid in as_list(item.get("tree")):
        if set((envs.get(pid) or {}).get(SESSION) or []) - {session}:
            return None, f"pid {pid} in its tree names another session"
    return session, None


def _record_gone(record: Mapping[str, Any], table: _procs.Table) -> bool:
    """Whether the process a registry record names is certainly gone: its pid runs nothing, or a process that did
    not start when the record says. A record without a start time proves nothing while its pid runs."""
    proc = table.get(record.get("pid"))
    if proc is None:
        return True
    if proc.started is None:
        return False
    if isinstance(record.get("procStart"), str):
        recorded = _procs.started_at(record["procStart"])
        if recorded is not None:
            return abs(recorded - proc.started) > _procs.EPOCH_SLACK
    if isinstance(record.get("startedAt"), (int, float)):
        return not -2 <= record["startedAt"] / 1000 - proc.started <= 120
    return False


def _session_process_gone(value: str, top: _procs.Proc, table: _procs.Table) -> bool:
    """Whether the process CLAUDE_PID named when `top` started can no longer be the session's: its pid runs
    nothing, or a process that started after `top` did (a reused pid)."""
    if not value.isdigit():
        return False
    proc = table.get(int(value))
    if proc is None:
        return True
    if proc.pid == top.pid or proc.started is None or top.started is None:
        return False
    return proc.started > top.started + _procs.EPOCH_SLACK


def gone(session: str, tops: Sequence[_procs.Proc], envs: Mapping[int, Environment], table: _procs.Table,
         registry: Optional[Sequence[Mapping[str, Any]]], store: Store) -> Tuple[Optional[bool], str]:
    """True and the proof when `session` is certainly gone; False when it runs; None and why it cannot be told."""
    if registry is None:
        return None, "Claude Code's session registry cannot be read"
    records = [r for r in registry if r.get("sessionId") == session]
    if any(_owner.registered_alive(dict(r), table) for r in records):
        return False, "the session runs"
    if any(not _record_gone(r, table) for r in records):
        return None, "Claude Code's registry names a process for the session that may still be it"
    proofs: List[str] = []
    pids = {p: sorted(set((envs.get(p.pid) or {}).get(SESSION_PID) or [])) for p in tops}
    for top, values in pids.items():
        if len(values) > 1:
            return None, f"pid {top.pid} names {len(values)} session processes"
        if values and not _session_process_gone(values[0], top, table):
            return None, f"pid {values[0]}, the session's process when pid {top.pid} started, may still be it"
    if pids and all(pids.values()):
        names = sorted({v[0] for v in pids.values()})
        proofs.append(f"its process (pid {', '.join(names)}) is gone")
    end = ended(store, session)
    if end is not None:
        proofs.insert(0, f"SessionEnd recorded ({end.get('reason') or '?'})")
    if records and all(_record_gone(r, table) for r in records):
        proofs.append("the process Claude Code's registry names for it is gone")
    if not proofs:
        return None, "nothing says it ended: no SessionEnd recorded and no record of its process"
    return True, "; ".join(proofs)


def refusal(item: Mapping[str, Any], table: _procs.Table, holders: Holders) -> Optional[str]:
    """Why a report item whose session is gone must still not be stopped, or None."""
    tree = {int(p) for p in as_list(item.get("tree"))}
    chain = {a for pid in tops_of(item) for a in table.ancestors(pid)}
    if holders.leases is None:
        return f"Slotfather's leases cannot be read ({holders.leases_why or 'unknown'})"
    if (tree | chain) & holders.leases:
        return "a Slotfather lease holds it or runs above it"
    procs = [p for p in (table.get(pid) for pid in tree) if p is not None]
    if (tree | chain) & holders.entries or any(p.pgid in holders.groups for p in procs):
        return "an active Spawnfather entry holds part of its tree"
    all_procs = [p for p in (table.get(pid) for pid in tree | chain) if p is not None]
    if any(_browser.is_browser(p) for p in all_procs):
        return "an agent-browser daemon, whose browser session another agent may use"
    return None


def targets(item: Mapping[str, Any], table: _procs.Table) -> List[_procs.Proc]:
    """What the stop signals: each top process and its descendants that safety.forced_targets allows."""
    found: Dict[int, _procs.Proc] = {}
    for pid in tops_of(item):
        for proc in _safety.forced_targets(pid, table):
            found.setdefault(proc.pid, proc)
    return list(found.values())


# The sweep's pass

def stop_gone(store: Store, items: List[Dict[str, Any]], table: _procs.Table,
              registry: Optional[Sequence[Mapping[str, Any]]], holders: Holders, grace: float, dry_run: bool,
              now: float, environ: Optional[Environments] = None, stopper: Optional[_reap.Stopper] = None
              ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """The sweep's unowned report items split into those it keeps reporting and the stop rows of those it stopped
    (or, in a dry run, would stop). A kept item tied to a session says why in `session` ({id, gone, why, kept}); a
    stop that failed keeps its item too, and its row says so. Then the old SessionEnd records go."""
    kept: List[Dict[str, Any]] = []
    rows: List[Dict[str, Any]] = []
    if items:
        wanted = {pid: proc.cmd for item in items for pid in as_list(item.get("tree"))
                  if (proc := table.get(int(pid))) is not None}
        envs = (environ or _procs.environments)(wanted)
        for item in items:
            row = _stop_one(store, item, envs, table, registry, holders, grace, dry_run, stopper or _reap.stop)
            if row is not None:
                rows.append(row)
            if row is None or row["result"] not in _reap.STOPPED:
                kept.append(item)  # a dry run's too: it still runs
    if not dry_run:
        prune(store, now)
    return kept, rows


def _stop_one(store: Store, item: Dict[str, Any], envs: Mapping[int, Environment], table: _procs.Table,
              registry: Optional[Sequence[Mapping[str, Any]]], holders: Holders, grace: float, dry_run: bool,
              stopper: _reap.Stopper) -> Optional[Dict[str, Any]]:
    """Stop one report item when its session is certainly gone and nothing holds it: its stop row, or None when it
    stays report-only (its `session` then says why, when it is tied to one)."""
    session, unsure = link(item, envs)
    if session is None:
        if unsure is not None:
            item["session"] = {"id": None, "gone": None, "why": unsure}
        return None
    tops = [p for p in (table.get(pid) for pid in tops_of(item)) if p is not None]
    is_gone, why = gone(session, tops, envs, table, registry, store)
    verdict: Dict[str, Any] = {"id": session, "gone": is_gone, "why": why}
    item["session"] = verdict
    if not is_gone:
        return None
    refused = refusal(item, table, holders)
    signalled = [] if refused else targets(item, table)
    if not refused:
        entries, groups = entry_holds(store, table)  # read again: an entry may have adopted it since
        chain = {a for pid in tops_of(item) for a in table.ancestors(pid)}
        if ({p.pid for p in signalled} | chain) & entries or any(p.pgid in groups for p in signalled):
            refused = "an active Spawnfather entry holds part of its tree"
        elif not signalled:
            refused = "nothing in its tree may be signalled"
    if refused:
        verdict["kept"] = refused
        return None
    reason = f"its Claude Code session {session} is gone: {why}"
    row: Dict[str, Any] = {"pid": item["pid"], "start": item["start"], "cmd": item["cmd"], "cwd": item.get("cwd"),
                           "session": session, "reason": reason, "pids": [p.pid for p in signalled],
                           "result": "would-stop"}
    if dry_run:
        return row
    outcome = stopper(signalled, grace)
    row["result"] = outcome["result"]
    done = outcome["result"] in _reap.STOPPED
    _events.write(store.root, "unowned.reaped", {"type": "process", "id": str(item["pid"]), "pid": item["pid"]},
                  _events.tool("sweep"),
                  {"cmd": item["cmd"], "start": item["start"], "cwd": item.get("cwd"), "reason": reason,
                   "session": session, "result": outcome["result"], "pids": row["pids"],
                   "left": outcome.get("left") or [], **({"error": outcome["error"]} if outcome.get("error") else {})},
                  None if done else "error")
    if not done:
        verdict["kept"] = f"the stop {outcome['result']}" + (f": {outcome['error']}" if outcome.get("error") else "")
    return row


def text(rows: Sequence[Mapping[str, Any]], dry_run: bool) -> List[str]:
    lines: List[str] = []
    for row in rows:
        verb = "would stop" if dry_run else "stopped" if row.get("result") in _reap.STOPPED else "failed to stop"
        lines.append(f"{verb} unowned pid {row.get('pid')} ({row.get('reason')}) -> {row.get('result')} (pids "
                     f"{', '.join(map(str, as_list(row.get('pids')))) or '-'}): {str(row.get('cmd'))[:160]}")
    return lines
