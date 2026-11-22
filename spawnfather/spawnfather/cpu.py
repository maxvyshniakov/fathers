"""Whose processes the busy cores belong to (fathers#98): a report, never a reason to stop anything.

The sweep reads `%cpu` for every process in its one `ps` call (100 = one core) and gives each busy process to the
first rule, in order, that claims it or one of its ancestors:

  entry    a running spawnfather entry: its leader, group, `known` members and their descendants (reap.members)
  lease    a Slotfather lease (`slotfather status --json`): its holder, verified by pid and start time, and the
           tree Slotfather lists for it; with Slotfather missing or not answering this rule is left out, and the
           report says so
  lease    (again) an agent-browser daemon launchd adopted, when a lease's process started its session (browser.py,
           Survey.rule, fathers#115): its CPU counts for that lease
  session  an agent session (Claude Code, Codex) found among its ancestors (owner.nearest)

What no rule claims is unattributed, reported as the top process trees by CPU (each under its topmost ancestor below
launchd). A rule is a mapping pid -> owner, so another module adds one without changing this one (`Rule`).
"""
from __future__ import annotations

import calendar
import json
import os
import subprocess
import time
from typing import Any, Dict, List, Mapping, NamedTuple, Optional, Sequence, Tuple

from . import procs as _procs
from . import safety as _safety
from .defaults import CPU_OWNERS, CPU_TOP, PS_TIMEOUT
from .state import as_dict, as_list

VERSION = 1
CMD_MAX = 120
Owner = Dict[str, Any]  # {"kind": entry|lease|session, "id", "label"}


class Rule(NamedTuple):
    """One attribution step: the processes it claims (a process below one of them is claimed too)."""
    kind: str
    claims: Dict[int, Owner]


# The rules

def entry_rule(members: Mapping[str, Tuple[Optional[str], Sequence[_procs.Proc]]]) -> Rule:
    """Entry id -> (label, its members as the sweep found them)."""
    claims: Dict[int, Owner] = {}
    for entry_id, (label, found) in members.items():
        for proc in found:
            claims.setdefault(proc.pid, {"kind": "entry", "id": entry_id, "label": label})
    return Rule("entry", claims)


def lease_rule(leases: Sequence[Mapping[str, Any]], table: _procs.Table) -> Rule:
    """Slotfather's entries: each holder that still runs with the start time Slotfather recorded, and its tree."""
    claims: Dict[int, Owner] = {}
    for lease in leases:
        pid = lease.get("pid")
        holder = table.get(pid if isinstance(pid, int) else None)
        if holder is None or not same_start(str(lease.get("start") or ""), holder):
            continue
        owner: Owner = {"kind": "lease", "id": str(lease.get("id") or pid), "label": lease.get("label")}
        tree: List[int] = [p for p in as_list(lease.get("tree")) if isinstance(p, int)]
        for member in [holder.pid, *tree]:
            claims.setdefault(member, owner)
    return Rule("lease", claims)


def session_rule(table: _procs.Table, sessions: Optional[Sequence[Mapping[str, Any]]]) -> Rule:
    """Every agent session's process; its id from Claude Code's registry when that names the pid."""
    by_pid = {r.get("pid"): r.get("sessionId") for r in sessions or [] if r.get("sessionId")}
    claims: Dict[int, Owner] = {}
    for proc in table.procs.values():
        if _safety.agent(proc):
            kind = "claude" if _safety.claude(proc.cmd) else "codex"
            claims[proc.pid] = {"kind": "session", "id": str(by_pid.get(proc.pid) or f"pid {proc.pid}"),
                                "label": kind}
    return Rule("session", claims)


def same_start(lstart: str, proc: _procs.Proc) -> bool:
    """Whether Slotfather's start (ps lstart in its own local time) is this process's (read here in UTC)."""
    try:
        local = time.mktime(time.strptime(_procs.norm(lstart), _procs.LSTART))
    except ValueError:
        return False
    started = proc.started
    if started is None:
        return False
    utc = float(calendar.timegm(time.strptime(_procs.norm(lstart), _procs.LSTART)))
    return abs(local - started) <= 1 or abs(utc - started) <= 1


# Slotfather, through its CLI

def leases(command: Optional[str] = None) -> Tuple[Optional[List[Dict[str, Any]]], str]:
    """Slotfather's queue entries from `slotfather status --json`, or None and why not (missing, failed)."""
    if command is not None:
        name = command
    elif "SPAWNFATHER_SLOTFATHER" in os.environ:
        name = os.environ["SPAWNFATHER_SLOTFATHER"]
    else:
        name = os.environ.get("JUNKFATHER_SLOTFATHER", "slotfather")
    from .resources import tool
    found = tool(name) if name and "/" not in name else (name if name and os.access(name, os.X_OK) else None)
    if not found:
        return None, "slotfather is not installed here"
    try:
        done = subprocess.run([found, "status", "--json"], capture_output=True, text=True, timeout=PS_TIMEOUT,
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return None, f"slotfather status --json did not answer in {PS_TIMEOUT:.0f} s"
    except OSError as error:
        return None, f"slotfather: {error}"
    if done.returncode != 0:
        return None, f"slotfather status --json exited {done.returncode}"
    try:
        data: Any = json.loads(done.stdout)
    except ValueError:
        return None, "slotfather status --json printed no JSON"
    entries = as_dict(data).get("entries")
    if not isinstance(entries, list):
        return None, "slotfather status --json has no entries"
    return [as_dict(e) for e in as_list(entries) if isinstance(e, dict)], ""


# The report

def owner_of(pid: int, table: _procs.Table, rules: Sequence[Rule]) -> Optional[Owner]:
    """The owner of the first rule that claims the process or one of its ancestors."""
    chain = [pid, *table.ancestors(pid)]
    for rule in rules:
        for here in chain:
            if here in rule.claims:
                return rule.claims[here]
    return None


def report(table: _procs.Table, rules: Sequence[Rule], cwds: Mapping[int, str], notes: Sequence[str] = (),
           top: int = CPU_TOP) -> Dict[str, Any]:
    """The busy cores by owner, and the top unattributed process trees."""
    by_kind: Dict[str, float] = {rule.kind: 0.0 for rule in rules}
    owners: Dict[Tuple[str, str], Dict[str, Any]] = {}
    trees: Dict[int, Dict[str, Any]] = {}
    total = unattributed = 0.0
    for proc in table.procs.values():
        if proc.cpu <= 0:
            continue
        cores = proc.cpu / 100.0
        total += cores
        owner = owner_of(proc.pid, table, rules)
        if owner is not None:
            kind = str(owner["kind"])
            by_kind[kind] = by_kind.get(kind, 0.0) + cores
            item = owners.setdefault((kind, str(owner["id"])), {**owner, "cores": 0.0, "processes": 0})
            item["cores"] += cores
            item["processes"] += 1
            continue
        unattributed += cores
        chain = table.ancestors(proc.pid)
        root = table.procs[chain[-1]] if chain else proc
        tree = trees.setdefault(root.pid, {"pid": root.pid, "start": root.start, "cmd": root.cmd[:CMD_MAX],
                                           "cwd": cwds.get(root.pid), "uid": root.uid, "cpu": 0.0,
                                           "processes": 0})
        tree["cpu"] += proc.cpu
        tree["processes"] += 1
    ranked = sorted(owners.values(), key=lambda o: -o["cores"])[:CPU_OWNERS]
    for item in ranked:
        item["cores"] = round(item["cores"], 2)
    heavy = sorted(trees.values(), key=lambda t: -t["cpu"])[:top]
    for tree in heavy:
        tree["cpu"] = round(tree["cpu"], 1)
    return {"v": VERSION, "at": table.moment, "cores": round(total, 2),
            "attributed": {kind: round(cores, 2) for kind, cores in by_kind.items()},
            "unattributed": round(unattributed, 2), "owners": ranked, "top": heavy, "notes": list(notes)}


def text(found: Mapping[str, Any]) -> List[str]:
    """The report for people: one summary line, then the unattributed trees."""
    if not found:
        return []
    parts = [f"{kind} {float(cores):.1f}" for kind, cores in as_dict(found.get("attributed")).items()]
    lines = [f"cpu: {float(found.get('cores') or 0):.1f} cores busy; {', '.join(parts)}, unattributed "
             f"{float(found.get('unattributed') or 0):.1f}"]
    for tree in map(as_dict, as_list(found.get("top"))):
        lines.append(f"  unattributed pid {tree.get('pid')} ({float(tree.get('cpu') or 0):.0f}% in "
                     f"{tree.get('processes')} process(es)): {tree.get('cmd')}")
    lines += [f"  cpu note: {note}" for note in as_list(found.get("notes"))]
    return lines
