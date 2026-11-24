"""The periodic pass: end owned entries whose TTL passed or whose owner is gone, and report unowned processes that
live in an agent's scratchpad or worktree, stopping only those of a Claude Code session that is certainly gone.

Owned entries:
- `expired`  its TTL passed: reaped.
- `orphaned` its owner session is gone (owner.alive is False; unknown is never gone): reaped.
- an entry whose processes all ended is marked `gone`; one stuck in `reaping` (a reaper that died) runs again.
- finished entries and their logs go after KEEP_FINISHED.

Unowned report: this user's processes, outside every running entry, whose working directory or a path in their
command is under a scratch root (`/private/tmp/claude-<uid>/`, `/tmp/claude-<uid>/`, `$SPAWNFATHER_SCRATCH_ROOTS`)
or a `.claude/worktrees/` or `.codex/worktrees/` directory. Not reported: agent sessions themselves, processes with
a terminal, spawnfather and its ancestors, anything below a live agent session (its shells, MCP and language servers
at work) and any process whose tree holds a terminal's process or an agent session (a tmux server and its panes).
A browser or server left behind has been adopted by launchd, so it is reported. Each is reported once per tree, with
the commands to own or end it; the list goes to unowned.json, which `spawnfather reap --pid PID --force` checks.
One whose environment ties it to a Claude Code session that is gone, and that no entry or Slotfather lease holds, is
stopped instead (leftovers.py, fathers#168); anything uncertain stays in the report with the reason.

Resource entries (resources.py) are marked gone when their kind says so, reaped past their TTL or when their owner
is gone, and left alone while whether they run cannot be told. Booted simulators, running containers and serve
routes no entry owns are reported in unowned.json's `resources`, never stopped.
"""
from __future__ import annotations

import os
from pathlib import Path
import plistlib
import re
import stat
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from . import browser as _browser
from . import cpu as _cpu
from . import entry as _entry
from . import events as _events
from . import leftovers as _leftovers
from . import owner as _owner
from . import procs as _procs
from . import reap as _reap
from . import resources as _resources
from . import safety as _safety
from . import settings as _settings
from . import worktrees as _worktrees
from .defaults import (GONE_LOG_BYTES, GONE_LOG_LINE_MAX, GONE_LOG_LINES, KEEP_FINISHED, REAPING_STUCK,
                       STARTING_STUCK, WORKTREE_IDLE)
from .entry import Entry
from .state import Store, VERSION, as_dict, as_list, read_json, write_atomic
from .text import duration
LABEL = "com.spawnfather.sweep"
WORKTREES = ("/.claude/worktrees/", "/.codex/worktrees/")
SPLIT = re.compile(r"[\s=:,]+")


def scratch_roots(uid: Optional[int] = None) -> List[str]:
    uid = os.getuid() if uid is None else uid
    roots = [f"/private/tmp/claude-{uid}/", f"/tmp/claude-{uid}/"]
    extra = os.environ.get("SPAWNFATHER_SCRATCH_ROOTS") or os.environ.get("JUNKFATHER_SCRATCH_ROOTS") or ""
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


def unowned(table: _procs.Table, cwds: Dict[int, str], owned: Set[int], roots: List[str],
            sessions_alive: Dict[int, bool], uid: Optional[int] = None) -> List[Dict[str, Any]]:
    uid = os.getuid() if uid is None else uid
    skip = _safety.protected(table)
    found: Dict[int, Dict[str, Any]] = {}
    for proc in table.procs.values():
        if not _safety.reportable(proc, uid, skip, owned):
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
        if _safety.below_session(proc.pid, table, sessions_alive) or _safety.untouchable_tree(proc.pid, table):
            continue
        below = table.descendants(proc.pid)
        found[proc.pid] = {"pid": proc.pid, "start": proc.start, "ppid": proc.ppid, "pgid": proc.pgid,
                           "age": table.age(proc), "cwd": cwd, "cmd": proc.cmd, "why": why, "root": root,
                           "tree": [proc.pid] + [p.pid for p in below]}
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
    return {p.pid: True for p in table.procs.values() if _safety.agent(p)}


Leases = Callable[[], Tuple[Optional[List[Dict[str, Any]]], str]]


def sweep(store: Store, dry_run: bool = False, table: Optional[_procs.Table] = None,
          cwds: Optional[Dict[int, str]] = None, sessions: Optional[List[Dict[str, Any]]] = None,
          now: Optional[float] = None, grace: Optional[float] = None, leases: Optional[Leases] = None,
          environ: Optional[_leftovers.Environments] = None, stopper: Optional[_reap.Stopper] = None
          ) -> Dict[str, Any]:
    """One pass. `leases` reads Slotfather's queue (cpu.leases, `slotfather status --json`); `environ` reads the
    unowned processes' session variables (procs.environments) and `stopper` stops those whose session is gone
    (reap.stop; leftovers.py)."""
    now = time.time() if now is None else now
    grace = _settings.value("reap.grace", store.root) if grace is None else grace
    report: Dict[str, Any] = {"v": VERSION, "at": now, "dry_run": dry_run, "reaped": [], "gone": [], "pruned": 0,
                              "running": 0, "unowned": [], "stopped_unowned": [], "unowned_resources": [],
                              "notes": [], "errors": []}
    members_of: Dict[str, Tuple[Optional[str], Sequence[_procs.Proc]]] = {}
    try:
        table = table or _procs.Table.read()
    except _procs.ProcError as error:
        report["errors"].append(str(error))
        return report
    if sessions is None:
        sessions = _owner.registry()
    owned: Set[int] = set()
    snapshot = _resources.Snapshot(table)
    survey = _browser.Survey(table)
    for entry in store.entries():
        state = entry.get("state")
        reaping = entry.get("reaping") or {}
        if state == _entry.REAPING and now - float(reaping.get("at") or 0) > REAPING_STUCK \
                and not table.alive(reaping.get("by"), reaping.get("by_start")):
            if not dry_run:
                _entry.release_stuck(store, entry["id"])
            state = _entry.RUNNING
        if state == _entry.REAPING:
            if entry.get("mode") != "resource":
                members, _ = _reap.members(entry, table)
                daemons = survey.daemons_below(members)
                if daemons:
                    members, _ = _reap.members(_entry.with_daemons(entry, daemons), table)
                owned.update(p.pid for p in members)
                if members:
                    members_of[entry["id"]] = (entry.get("label"), members)
                supervisor = entry.get("supervisor") or {}
                if table.alive(supervisor.get("pid"), supervisor.get("start")):
                    owned.add(supervisor["pid"])
            continue
        if state not in _entry.OPEN:
            ended = entry.get("ended")
            if ended and now - float(ended) > KEEP_FINISHED and not dry_run:
                prune(store, entry)
                report["pruned"] += 1
            continue
        if state == _entry.STARTING:
            if now - float(entry.get("created") or now) > STARTING_STUCK and not dry_run:
                _entry.failed(store, entry["id"], "never started", now)
            continue
        if entry.get("mode") == "resource":
            sweep_resource(store, entry, snapshot, report, now, sessions, grace, dry_run)
            continue
        members, how = _reap.members(entry, table)
        daemons = survey.daemons_below(members)
        if daemons:  # agent-browser daemons its processes started: they join it, and are reaped with it
            joined = None if dry_run else _entry.daemons(store, entry["id"], daemons)
            members, how = _reap.members(joined or _entry.with_daemons(entry, daemons), table)
        supervisor = entry.get("supervisor") or {}
        supervised = table.alive(supervisor.get("pid"), supervisor.get("start"))
        if not members:
            if supervised:  # the supervisor marks its own entry when the command ends
                report["running"] += 1
                owned.add(supervisor["pid"])
                continue
            if not dry_run:
                updated = _entry.gone(store, entry["id"], now)
                details = _gone_details(updated or entry, how)
                if updated is not None:
                    _events.write(store.root, "entry.gone", _events.subject(updated), _events.tool("sweep"), details)
            else:
                details = _gone_details(entry, how)
            report["gone"].append({"id": entry["id"], **details})
            continue
        result = reap_due_or_orphaned(store, entry, now, table, sessions, grace, dry_run)
        if result is not None:
            report["reaped"].append(result)
            if result["result"] in _reap.STOPPED:
                continue
        report["running"] += 1
        owned.update(p.pid for p in members)
        members_of[entry["id"]] = (entry.get("label"), members)
        if supervised:
            owned.add(supervisor["pid"])  # and it keeps `known` fresh itself
        elif not dry_run:
            _entry.known(store, entry["id"], members)
    if cwds is None:
        cwds = _procs.working_dirs(os.getuid())
        if not cwds:
            report["errors"].append("lsof listed no working directory; paths in commands only")
    found_unowned = unowned(table, cwds, owned, scratch_roots(), agent_sessions(table))
    queue, queue_why = (leases or _cpu.leases)()
    report["cpu"], lease_rule = cpu_report(table, cwds, members_of, sessions, (queue, queue_why), survey)
    entry_pids, entry_groups = _leftovers.entry_holds(store, table)
    holders = _leftovers.Holders(
        entry_pids | owned, entry_groups,
        None if queue is None else _leftovers.lease_holds(queue, table, [*lease_rule.claims,
                                                                         *survey.rule(lease_rule).claims]),
        queue_why)
    report["unowned"], report["stopped_unowned"] = _leftovers.stop_gone(
        store, found_unowned, table, sessions, holders, grace, dry_run, now, environ=environ, stopper=stopper)
    report["agent_browser"] = {"leftovers": _browser.clean(survey, now, dry_run),
                               "idle": survey.idle(owned, lease_rule, now)}
    found, unknown = _resources.unowned(snapshot, _resources.owned_keys(store))
    report["worktrees"] = sweep_worktrees(store, table, cwds or None, now, dry_run, _resources.owned_keys(store), grace)
    found += report["worktrees"].pop("kept")
    report["unowned_resources"] = found
    report["unlisted"] = unknown
    report["notes"] += [f"{kind}: cannot be listed, so its unowned resources are not reported" for kind in unknown]
    if not dry_run:
        record(store, report)
    return report


def sweep_worktrees(store: Store, table: _procs.Table, cwds: Optional[Dict[int, str]], now: float, dry_run: bool,
                    owned: Dict[str, str], grace: float) -> Dict[str, Any]:
    """Every repository's linked worktrees (worktrees.repositories). One no entry and no live Lanefather lane owns
    goes through worktrees.plan, the guarded removal an owned worktree's stop uses, with 24 h of quiet required, and
    resources.stop runs it; the others are reported. A first look with the sweep's process table picks the
    candidates; plan reads everything again right before. `git worktree prune` drops the entries whose folder is
    gone. A dry run removes and prunes nothing."""
    found: Dict[str, Any] = {"removed": [], "pruned": [], "kept": [], "notes": []}
    lanes = _worktrees.lane_worktrees()
    if not lanes.known:
        found["notes"].append("Lanefather's project registry cannot be read: no worktree under .claude/worktrees is "
                              "removed")
    # A size is measured (du) when a worktree is first reported, then kept from the last report: du over every
    # kept worktree's node_modules each hour would cost minutes.
    sizes = {str(i.get("key")): i.get("size") for i in map(as_dict, as_list(as_dict(
        read_json(store.root / "unowned.json")).get("resources"))) if i.get("kind") == "worktree"}
    actor = _events.tool("sweep")
    for repo in _worktrees.repositories():
        items = _worktrees.listing(repo)
        if items is None:
            found["notes"].append(f"{repo}: git cannot list its worktrees")
            continue
        main = str(items[0]["path"])
        common = _worktrees.common_dir(main)
        for item in items[1:]:
            path = str(item["path"])
            if item.get("prunable") or not os.path.isdir(path) or f"worktree:{path}" in owned or path in lanes.paths:
                continue
            branch = item.get("branch")
            resource: Dict[str, Any] = {"kind": "worktree", "id": path, "repo": main, "common": common,
                                        "branch": branch}
            key = _resources.key(resource)
            why = _worktrees.refusal(item, main, table, cwds, items, idle=WORKTREE_IDLE, now=now, lanes=lanes)
            steps: List[List[str]] = []
            if why is None:
                try:
                    steps = _worktrees.plan(resource, idle=WORKTREE_IDLE, now=now, lanes=lanes)
                except _resources.Refused as refused:
                    why = str(refused).removeprefix("kept: ")
            if why is not None:
                found["kept"].append({**resource, "name": branch, "age": max(0.0, now - _worktrees.activity(path)),
                                      "size": sizes[key] if key in sizes else _worktrees.size(path), "key": key,
                                      "why": f"an unowned worktree, kept: {why}"})
                continue
            removed: Dict[str, Any] = {**resource, "size": _worktrees.size(path),
                                       "delete_branch": branch if len(steps) > 1 else None,
                                       "result": "would-remove" if dry_run else "removed"}
            if not dry_run:
                outcome = _resources.stop(resource, grace, _resources.Snapshot(None).bound(resource), commands=steps)
                done = outcome["result"] in _reap.STOPPED
                subject = {"type": "resource", "id": key, "kind": "worktree", **({"label": str(branch)} if branch
                                                                                 else {})}
                _resources.stopped_event(store, subject, actor, "unowned, clean, merged and idle", resource, outcome,
                                         done)
                if not done:
                    found["kept"].append({**resource, "name": branch, "key": key,
                                          "why": f"an unowned worktree, kept: {outcome.get('error')}"})
                    continue
                if outcome.get("warning"):
                    removed["warning"] = outcome["warning"]
            found["removed"].append(removed)
        code, out = _worktrees.git(main, "worktree", "prune", "--dry-run", "--verbose", both=True)
        stale = [line.strip() for line in out.splitlines() if line.strip()] if code == 0 else []
        if stale and not dry_run:
            code, _ = _worktrees.git(main, "worktree", "prune")
            if code != 0:
                found["notes"].append(f"{main}: git worktree prune failed")
                stale = []
        found["pruned"] += [f"{main}: {line}" for line in stale]
    return found


def cpu_report(table: _procs.Table, cwds: Dict[int, str],
               members_of: Dict[str, Tuple[Optional[str], Sequence[_procs.Proc]]],
               sessions: Optional[List[Dict[str, Any]]], leases: Tuple[Optional[List[Dict[str, Any]]], str],
               survey: "_browser.Survey") -> Tuple[Dict[str, Any], "_cpu.Rule"]:
    """Whose processes the busy cores belong to (cpu.py): entries, Slotfather leases (`leases`: the queue Leases
    read, or None and why not; and the agent-browser daemons their processes started), agent sessions. Also the
    lease rule, empty without Slotfather."""
    notes: List[str] = []
    rules = [_cpu.entry_rule(members_of)]
    queue, why = leases
    lease_rule = _cpu.Rule("lease", {})
    if queue is None:
        notes.append(f"Slotfather leases are left out: {why}")
    else:
        lease_rule = _cpu.lease_rule(queue, table)
        rules += [lease_rule, survey.rule(lease_rule)]
    rules.append(_cpu.session_rule(table, sessions))
    return _cpu.report(table, rules, cwds, notes), lease_rule


def reap_due_or_orphaned(store: Store, entry: Entry, now: float, table: Optional[_procs.Table],
                         sessions: Optional[List[Dict[str, Any]]], grace: float, dry_run: bool
                         ) -> Optional[Dict[str, Any]]:
    """Reap `entry` if its TTL passed or its owner is gone; returns the reap result row, or None."""
    reason: Optional[str] = None
    guard: Optional[_entry.Guard] = None
    if _entry.due(entry, now):
        reason = "ttl expired"
        guard = _entry.require_due(now)
    elif _owner.alive(entry.get("owner") or {}, table, sessions) is False:
        reason = f"owner gone ({_owner.describe(entry.get('owner') or {})})"
    if not reason:
        return None
    use_table = table if (dry_run or entry.get("mode") == "resource") else None
    return _reap.reap(store, entry["id"], reason, grace=grace, dry_run=dry_run,
                      table=use_table, actor=_events.tool("sweep"), allowed=guard)


def sweep_resource(store: Store, entry: Entry, snapshot: "_resources.Snapshot", report: Dict[str, Any],
                   now: float, sessions: Optional[List[Dict[str, Any]]], grace: float, dry_run: bool) -> None:
    """A resource entry: gone when its kind says so, reaped past its TTL or when its owner is gone, left alone while
    whether it runs cannot be told."""
    resource: Dict[str, Any] = entry.get("resource") or {}
    alive = snapshot.alive(resource)
    if alive is False:
        if not dry_run:
            _entry.gone(store, entry["id"], now)
        report["gone"].append({"id": entry["id"], "how": _resources.describe(resource)})
        return
    if alive is None:
        report["running"] += 1
        report["notes"].append(f"{entry['id']}: whether {_resources.describe(resource)} runs cannot be told")
        return
    result = reap_due_or_orphaned(store, entry, now, snapshot.table, sessions, grace, dry_run)
    if result is not None:
        report["reaped"].append(result)
        snapshot.listed.pop(str(resource.get("kind")), None)  # the report lists that kind again, after the stop
        if result["result"] in _reap.STOPPED:
            return
    report["running"] += 1


def record(store: Store, report: Dict[str, Any]) -> None:
    """unowned.json, sweep.json and the sweep's events: one unowned.reported per process not reported before."""
    before = as_dict(read_json(store.root / "unowned.json"))
    items: List[Dict[str, Any]] = [as_dict(i) for i in as_list(before.get("items"))]
    resources: List[Dict[str, Any]] = [as_dict(i) for i in as_list(before.get("resources"))]
    seen = {(i.get("pid"), i.get("start")) for i in items}
    seen_resources = {i.get("key") for i in resources}
    # A kind that could not be listed keeps its last report, so the next sweep does not report it as new again.
    kept = [i for i in resources if i and i.get("kind") in (report.get("unlisted") or [])]
    write_atomic(store.root / "unowned.json", {"v": VERSION, "at": report["at"], "items": report["unowned"],
                                                "resources": report["unowned_resources"] + kept})
    actor = _events.tool("sweep")
    for item in report["unowned"]:
        if (item["pid"], item["start"]) not in seen:
            _events.write(store.root, "unowned.reported", {"type": "process", "id": str(item["pid"]),
                                                           "pid": item["pid"]}, actor,
                          {"cmd": item["cmd"], "cwd": item["cwd"], "root": item["root"], "why": item["why"],
                           "start": item["start"], "age": item["age"], "also": item.get("also") or [],
                           **({"session": item["session"]} if item.get("session") else {})}, "warn")
    for item in report["unowned_resources"]:
        if item["key"] not in seen_resources:
            subject = {"type": "resource", "id": item["key"], "kind": item["kind"]}
            if item.get("name"):
                subject["label"] = str(item["name"])
            _events.write(store.root, "unowned.reported", subject, actor,
                          {k: v for k, v in item.items() if k != "key"}, "warn")
    summary = result_of(report)
    write_atomic(store.root / "sweep.json", {"v": VERSION, "at": report["at"], "result": summary,
                                              "cpu": report.get("cpu"), "agent_browser": report.get("agent_browser"),
                                              "worktrees": report.get("worktrees")})
    _events.write(store.root, "sweep.finished", {"type": "sweep", "id": _events.now()}, actor, summary,
                  "error" if report["errors"] else None)


def result_of(report: Dict[str, Any]) -> Dict[str, Any]:
    return {"reaped": [r["id"] for r in report["reaped"] if r["result"] in _reap.STOPPED],
            "failed": [r["id"] for r in report["reaped"] if r["result"] == "failed"],
            "gone": [g["id"] for g in report["gone"]], "running": report["running"],
            "unowned": len(report["unowned"]),
            "stopped_unowned": [as_dict(r).get("pid") for r in as_list(report.get("stopped_unowned"))
                                if as_dict(r).get("result") in _reap.STOPPED],
            "unowned_resources": len(report.get("unowned_resources") or []),
            "pruned": report["pruned"], "errors": report["errors"], "notes": report.get("notes") or []}


def prune(store: Store, entry: Entry) -> None:
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


def text(report: Dict[str, Any], quiet: bool = False) -> str:
    lines: List[str] = []
    for result in report["reaped"]:
        verbs = {"would-stop": "would reap", "would-fail": "would keep"}  # text, not a state
        verb = verbs.get(result["result"], "reaped")  # text, not a state
        lines.append(f"{verb} {result['id']}: "
                     f"{result['reason']} -> {result['result']} (pids {', '.join(map(str, result['pids'])) or '-'})")
    lines += _leftovers.text(as_list(report.get("stopped_unowned")), bool(report.get("dry_run")))
    for error in report["errors"]:
        lines.append(f"error: {error}")
    if quiet:
        return "\n".join(lines)
    for item in report["unowned"]:
        age = _age(item.get("age"))
        under = ""
        also = f", {len(item['also'])} more in its group" if item.get("also") else ""
        lines.append(f"unowned pid {item['pid']} ({age}{under}{also}; {item['why']}): {item['cmd'][:160]}")
        session = as_dict(item.get("session"))
        if session:
            verdict = "is gone" if session.get("gone") is True else "runs" if session.get("gone") is False \
                else "cannot be told"
            name = str(session.get("id") or "?")[:8]
            lines.append(f"  its Claude Code session {name} {verdict} ({session.get('why')})"
                         + (f"; kept: {session['kept']}" if session.get("kept") else ""))
        lines.append(f"  own it: spawnfather adopt {item['pid']} --ttl 1h   end it: spawnfather reap --pid "
                     f"{item['pid']} --force")
    for item in as_list(report.get("unowned_resources")):
        lines.append(f"unowned {_resources.describe(item)}: {item['why']}")
        lines.append(f"  own it: spawnfather own-resource --kind {item['kind']} --id {item['id']} --ttl 1h")
    for note in as_list(report.get("notes")):
        lines.append(f"note: {note}")
    lines += _browser.text(as_dict(report.get("agent_browser")), bool(report.get("dry_run")))
    lines += worktrees_text(as_dict(report.get("worktrees")), bool(report.get("dry_run")))
    lines += _cpu.text(as_dict(report.get("cpu")))
    lines.append(f"{report['running']} running, {len([r for r in report['reaped'] if r['result'] in _reap.STOPPED])} "
                 f"reaped, {len(report['gone'])} gone, {len(report['unowned'])} unowned reported, "
                 f"{len(report.get('unowned_resources') or [])} unowned resources"
                 + (" (dry run)" if report["dry_run"] else ""))
    return "\n".join(lines)


def worktrees_text(found: Dict[str, Any], dry_run: bool) -> List[str]:
    lines: List[str] = []
    for item in map(as_dict, as_list(found.get("removed"))):
        branch = f" and its branch {item['delete_branch']}" if item.get("delete_branch") else ""
        lines.append(f"{'would remove' if dry_run else 'removed'} worktree {item.get('id')}{branch}")
    lines += [f"{'would prune' if dry_run else 'pruned'} {line}" for line in as_list(found.get("pruned"))]
    lines += [f"note: {note}" for note in as_list(found.get("notes"))]
    return lines


def _tail_log(path: Any) -> Optional[List[str]]:
    """Last few lines of an entry's log file, capped in bytes and lines; None if missing, not a regular file,
    unreadable, or empty."""
    if not isinstance(path, str) or not path:
        return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        handle = os.fdopen(fd, "rb")
    except OSError:
        os.close(fd)
        return None
    try:
        with handle:
            st = os.fstat(handle.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_size <= 0:
                return None
            truncated = st.st_size > GONE_LOG_BYTES
            if truncated:
                handle.seek(st.st_size - GONE_LOG_BYTES)
            chunk = handle.read(GONE_LOG_BYTES)
    except OSError:
        return None
    lines = chunk.decode("utf-8", errors="replace").splitlines()
    if truncated and len(lines) > 1:
        lines = lines[1:]  # drop the partial line at the seek boundary
    lines = [line[:GONE_LOG_LINE_MAX] for line in lines[-GONE_LOG_LINES:]]
    return lines or None


def _gone_details(entry: Entry, how: str) -> Dict[str, Any]:
    """What the sweep can record when an owned entry's processes are already gone."""
    details: Dict[str, Any] = {"how": how}
    code = entry.get("exit")
    if isinstance(code, int) and not isinstance(code, bool):
        details["exit"] = code
    log = entry.get("log")
    if isinstance(log, str) and log:
        tail = _tail_log(log)
        if tail is not None:
            details["log"] = log
            details["log_tail"] = tail
    return details


def _age(seconds: Optional[float]) -> str:
    return "age ?" if seconds is None else f"age {duration(seconds)}"


def plist(command: List[str], interval: int, directory: Path) -> str:
    environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")}
    for primary, legacy in (
        ("SPAWNFATHER_STATE", "JUNKFATHER_STATE"),
        ("SPAWNFATHER_SCRATCH_ROOTS", "JUNKFATHER_SCRATCH_ROOTS"),
        ("SPAWNFATHER_CLAUDE_SESSIONS", "JUNKFATHER_CLAUDE_SESSIONS"),
    ):
        value = os.environ.get(primary) or os.environ.get(legacy)
        if value:
            environment[primary] = value
    data = {"Label": LABEL, "ProgramArguments": command, "StartInterval": interval, "RunAtLoad": True,
            "ProcessType": "Background", "LowPriorityIO": True, "Nice": 10, "EnvironmentVariables": environment,
            "StandardOutPath": str(directory / "sweep.out"), "StandardErrorPath": str(directory / "sweep.out")}
    return plistlib.dumps(data).decode()


def default_command() -> List[str]:
    launcher = os.path.expanduser("~/.local/bin/spawnfather")
    if os.path.isfile(launcher):
        return [sys.executable, launcher, "sweep", "--quiet"]
    from .launch import self_command
    return self_command("sweep", "--quiet")
