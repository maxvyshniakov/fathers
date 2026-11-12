"""The periodic pass: end owned entries whose TTL passed or whose owner is gone, and report (never stop) unowned
processes that live in an agent's scratchpad or worktree.

Owned entries:
- `expired`  its TTL passed: reaped.
- `orphaned` its owner session is gone (owner.alive is False; unknown is never gone): reaped.
- an entry whose processes all ended is marked `gone`; one stuck in `reaping` (a reaper that died) runs again.
- finished entries and their logs go after KEEP_FINISHED.

Unowned report: this user's processes, outside every running entry, whose working directory or a path in their
command is under a scratch root (`/private/tmp/claude-<uid>/`, `/tmp/claude-<uid>/`, `$JUNKFATHER_SCRATCH_ROOTS`)
or a `.claude/worktrees/` or `.codex/worktrees/` directory. Not reported: agent sessions themselves, processes with
a terminal, junkfather and its ancestors, and processes under a live agent session younger than `session_age`
(an agent's own shells and servers at work). Each is reported once per tree, with the commands to own or end it;
the list goes to unowned.json, which `junkfather reap --pid PID --force` checks.
"""
from __future__ import annotations

import os
from pathlib import Path
import plistlib
import re
import sys
import time
from typing import Any, Dict, List, Optional

from . import events as _events
from . import owner as _owner
from . import procs as _procs
from . import reap as _reap
from .state import Store, VERSION, read_json, write_atomic

KEEP_FINISHED = 7 * 86400.0
SESSION_AGE = 6 * 3600.0  # an unowned process under a live session is reported once it is this old
REAPING_STUCK = 300.0
LABEL = "com.junkfather.sweep"
WORKTREES = ("/.claude/worktrees/", "/.codex/worktrees/")
SPLIT = re.compile(r"[\s=:,]+")


def scratch_roots(uid: Optional[int] = None) -> List[str]:
    uid = os.getuid() if uid is None else uid
    roots = [f"/private/tmp/claude-{uid}/", f"/tmp/claude-{uid}/"]
    extra = os.environ.get("JUNKFATHER_SCRATCH_ROOTS", "")
    roots += [r.rstrip("/") + "/" for r in extra.split(":") if r]
    return roots


def points_into(path: str, roots: List[str]) -> Optional[str]:
    """Why a path is in an agent's space: the root or worktree marker it is under, or None."""
    if not path.startswith("/"):
        return None
    candidate = path if path.endswith("/") else path + "/"
    for root in roots:
        if candidate.startswith(root):
            return root
    for marker in WORKTREES:
        if marker in candidate:
            return marker.strip("/")
    return None


def unowned(table: _procs.Table, cwds: Dict[int, str], owned: set, roots: List[str],
            sessions_alive: Dict[int, bool], session_age: float = SESSION_AGE,
            uid: Optional[int] = None) -> List[Dict[str, Any]]:
    uid = os.getuid() if uid is None else uid
    skip = _reap.protected(table)
    found: Dict[int, Dict[str, Any]] = {}
    for proc in table.procs.values():
        if proc.uid != uid or proc.pid in skip or proc.pid in owned or proc.tty:
            continue
        if _owner.CLAUDE.search(proc.cmd) or _owner.CODEX.search(proc.cmd):
            continue
        why = root = None
        cwd = cwds.get(proc.pid)
        if cwd and points_into(cwd, roots):
            root = points_into(cwd, roots)
            why = f"working directory in {root}"
        else:
            for word in SPLIT.split(proc.cmd):
                root = points_into(word, roots)
                if root:
                    why = f"command names a path in {root}"
                    break
        if why is None:
            continue
        session = next((pid for pid in table.ancestors(proc.pid)
                        if pid in sessions_alive and sessions_alive[pid]), None)
        age = table.age(proc)
        if session is not None and (age is None or age < session_age):
            continue
        found[proc.pid] = {"pid": proc.pid, "start": proc.start, "ppid": proc.ppid, "pgid": proc.pgid,
                           "age": age, "cwd": cwd, "cmd": proc.cmd, "why": why, "root": root,
                           "under_session": session,
                           "tree": [proc.pid] + [p.pid for p in table.descendants(proc.pid)]}
    # One item per tree (drop those whose ancestor is reported too) and per process group (a browser's helpers).
    tops = [item for pid, item in found.items() if not any(a in found for a in table.ancestors(pid))]
    groups: Dict[int, Dict[str, Any]] = {}
    for item in sorted(tops, key=lambda i: i["pid"]):
        first = groups.get(item["pgid"])
        if first is None:
            item["also"] = []
            groups[item["pgid"]] = item
        else:
            first["also"].append(item["pid"])
            first["tree"] += item["tree"]
    return sorted(groups.values(), key=lambda item: -(item["age"] or 0))


def agent_sessions(table: _procs.Table) -> Dict[int, bool]:
    """Agent session processes in the table (Claude Code, Codex): each is alive by being there."""
    return {p.pid: True for p in table.procs.values() if _owner.CLAUDE.search(p.cmd) or _owner.CODEX.search(p.cmd)}


def sweep(store: Store, dry_run: bool = False, table: Optional[_procs.Table] = None,
          cwds: Optional[Dict[int, str]] = None, sessions: Optional[List[Dict[str, Any]]] = None,
          now: Optional[float] = None, grace: float = _reap.GRACE) -> Dict[str, Any]:
    now = time.time() if now is None else now
    report: Dict[str, Any] = {"v": VERSION, "at": now, "dry_run": dry_run, "reaped": [], "gone": [], "pruned": 0,
                              "running": 0, "unowned": [], "errors": []}
    try:
        table = table or _procs.Table.read()
    except _procs.ProcError as error:
        report["errors"].append(str(error))
        return report
    if sessions is None:
        sessions = _owner.registry()
    owned: set = set()
    for entry in store.entries():
        state = entry.get("state")
        if state == "reaping" and now - float((entry.get("reaping") or {}).get("at") or 0) > REAPING_STUCK:
            if not dry_run:
                store.update(entry["id"], lambda e: e.update(state="running") if e.get("state") == "reaping" else False)
            state = "running"
        if state not in ("running", "starting"):
            if entry.get("ended") and now - float(entry["ended"]) > KEEP_FINISHED and not dry_run:
                prune(store, entry)
                report["pruned"] += 1
            continue
        if state == "starting":
            if now - float(entry.get("created") or now) > 60 and not dry_run:
                store.update(entry["id"], lambda e: e.update(state="failed", ended=now, error="never started")
                             if e.get("state") == "starting" else False)
            continue
        members, how = _reap.members(entry, table)
        if not members:
            if not dry_run:
                store.update(entry["id"], lambda e: e.update(state="gone", ended=now, known=[])
                             if e.get("state") == "running" else False)
            report["gone"].append({"id": entry["id"], "how": how})
            continue
        reason = None
        if now >= float(entry.get("expires") or 0):
            reason = "ttl expired"
        elif _owner.alive(entry.get("owner") or {}, table, sessions) is False:
            reason = f"owner gone ({_owner.describe(entry.get('owner') or {})})"
        if reason:
            result = _reap.reap(store, entry["id"], reason, grace=grace, dry_run=dry_run, table=table,
                                actor=_events.tool("sweep"))
            report["reaped"].append(result)
            if result["result"] in _reap.STOPPED:
                continue
        report["running"] += 1
        owned.update(p.pid for p in members)
        supervisor = entry.get("supervisor") or {}
        if table.alive(supervisor.get("pid"), supervisor.get("start")):
            owned.add(supervisor["pid"])
        if not dry_run:
            store.update(entry["id"], refresher([[p.pid, p.start] for p in members]))
    if cwds is None:
        cwds = _procs.working_dirs(os.getuid())
        if not cwds:
            report["errors"].append("lsof listed no working directory; paths in commands only")
    report["unowned"] = unowned(table, cwds, owned, scratch_roots(), agent_sessions(table))
    if not dry_run:
        record(store, report)
    return report


def record(store: Store, report: Dict[str, Any]) -> None:
    """unowned.json, sweep.json and the sweep's events: one unowned.reported per process not reported before."""
    before = read_json(store.root / "unowned.json")
    seen = {(i.get("pid"), i.get("start")) for i in (before.get("items") or [])} if isinstance(before, dict) else set()
    write_atomic(store.root / "unowned.json", {"v": VERSION, "at": report["at"], "items": report["unowned"]})
    actor = _events.tool("sweep")
    for item in report["unowned"]:
        if (item["pid"], item["start"]) not in seen:
            _events.write(store.root, "unowned.reported", {"type": "process", "id": str(item["pid"]),
                                                           "pid": item["pid"]}, actor,
                          {"cmd": item["cmd"], "cwd": item["cwd"], "root": item["root"], "why": item["why"],
                           "start": item["start"], "age": item["age"], "also": item.get("also") or []}, "warn")
    summary = result_of(report)
    write_atomic(store.root / "sweep.json", {"v": VERSION, "at": report["at"], "result": summary})
    _events.write(store.root, "sweep.finished", {"type": "sweep", "id": _events.now()}, actor, summary,
                  "error" if report["errors"] else None)


def result_of(report: Dict[str, Any]) -> Dict[str, Any]:
    return {"reaped": [r["id"] for r in report["reaped"] if r["result"] in _reap.STOPPED],
            "failed": [r["id"] for r in report["reaped"] if r["result"] == "failed"],
            "gone": [g["id"] for g in report["gone"]], "running": report["running"],
            "unowned": len(report["unowned"]), "pruned": report["pruned"], "errors": report["errors"]}


def refresher(known: List[List[Any]]):
    def change(e: Dict[str, Any]) -> Optional[bool]:
        if e.get("state") != "running":
            return False
        e["known"] = known
        return None
    return change


def prune(store: Store, entry: Dict[str, Any]) -> None:
    try:
        store.path(entry["id"]).unlink()
    except OSError:
        pass
    log = entry.get("log")
    if isinstance(log, str) and log.startswith(str(store.logs_dir) + "/"):
        try:
            Path(log).unlink()
        except OSError:
            pass


def listed_unowned(store: Store, pid: int) -> Optional[Dict[str, Any]]:
    data = read_json(store.root / "unowned.json")
    items = data.get("items") if isinstance(data, dict) else None
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and item.get("pid") == pid:
            return item
    return None


def text(report: Dict[str, Any], quiet: bool = False) -> str:
    lines = []
    for result in report["reaped"]:
        lines.append(f"{'would reap' if result['result'] == 'would-stop' else 'reaped'} {result['id']}: "
                     f"{result['reason']} -> {result['result']} (pids {', '.join(map(str, result['pids'])) or '-'})")
    for error in report["errors"]:
        lines.append(f"error: {error}")
    if quiet:
        return "\n".join(lines)
    for item in report["unowned"]:
        age = _age(item.get("age"))
        under = f", under live session pid {item['under_session']}" if item.get("under_session") else ""
        also = f", {len(item['also'])} more in its group" if item.get("also") else ""
        lines.append(f"unowned pid {item['pid']} ({age}{under}{also}; {item['why']}): {item['cmd'][:160]}")
        lines.append(f"  own it: junkfather adopt {item['pid']} --ttl 1h   end it: junkfather reap --pid "
                     f"{item['pid']} --force")
    lines.append(f"{report['running']} running, {len([r for r in report['reaped'] if r['result'] in _reap.STOPPED])} "
                 f"reaped, {len(report['gone'])} gone, {len(report['unowned'])} unowned reported"
                 + (" (dry run)" if report["dry_run"] else ""))
    return "\n".join(lines)


def _age(seconds: Optional[float]) -> str:
    from .launch import duration
    return "age ?" if seconds is None else f"age {duration(seconds)}"


def plist(command: List[str], interval: int, directory: Path) -> str:
    environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")}
    for name in ("JUNKFATHER_STATE", "JUNKFATHER_SCRATCH_ROOTS", "JUNKFATHER_CLAUDE_SESSIONS"):
        if os.environ.get(name):
            environment[name] = os.environ[name]
    data = {"Label": LABEL, "ProgramArguments": command, "StartInterval": interval, "RunAtLoad": True,
            "ProcessType": "Background", "LowPriorityIO": True, "Nice": 10, "EnvironmentVariables": environment,
            "StandardOutPath": str(directory / "sweep.out"), "StandardErrorPath": str(directory / "sweep.out")}
    return plistlib.dumps(data).decode()


def default_command() -> List[str]:
    launcher = os.path.expanduser("~/.local/bin/junkfather")
    if os.path.isfile(launcher):
        return [sys.executable, launcher, "sweep", "--quiet"]
    from .launch import self_command
    return self_command("sweep", "--quiet")
