"""junkfather: owns the long-lived processes agents start (preview servers, browsers, drivers, watchers) and ends
them when their owner ends or their TTL passes.

  junkfather own [--ttl 2h] [--keep-for owner|session] [--label L] [--lane L] [--detach [--log FILE]] -- CMD...
  junkfather own --kind simulator|docker|tailscale-serve|safari-tp [--remove] [--ttl ...] -- CMD...
                 (runs CMD, then owns the resource it made: xcrun simctl boot UDID, docker run -d ..., ...)
  junkfather own-resource --kind KIND --id ID [--remove] [--ttl 2h] [--keep-for ...] [--label L] [--lane L]
  junkfather own-resource --kind command --id NAME --alive CMD --stop CMD [...]   (CMD split as argv, no shell)
  junkfather adopt PID [--ttl 2h] [--keep-for owner|session] [--label L] [--lane L]
  junkfather list [--all] [--json]
  junkfather status --json   (list --json, the last sweep's unowned processes and its time and result)
  junkfather capabilities --json
  junkfather changelog --json [--since VERSION]   (the installed release's CHANGELOG, newest first)
  junkfather reap (--id ID... | --owner SESSION|AGENT [--agent AGENT] | --lane L | --pid PID [--force])
                  [--expired] [--reason TEXT] [--grace S] [--dry-run] [--json] [--quiet]
  junkfather hand ID --to SESSION|self
  junkfather extend ID --ttl 2h
  junkfather sweep [--dry-run] [--json] [--quiet] | sweep --plist [--interval S]
  junkfather hook            (Claude Code / Codex lifecycle hook: JSON on stdin)
  junkfather version

Durations take seconds or 90s, 30m, 2h, 1d. State: $JUNKFATHER_STATE (default ~/.local/state/junkfather).
See README.md for how agents launch through it and docs/reference.md for the rules.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
import time
from typing import Any, Dict, List, Optional

from . import __version__
from . import changelog as _changelog
from . import entry as _entry
from . import events as _events
from . import hook as _hook
from . import launch as _launch
from . import owner as _owner
from . import procs as _procs
from . import reap as _reap
from . import resources as _resources
from . import sweep as _sweep
from .state import Entry, Store


class Usage(Exception):
    pass


def seconds(text: str) -> float:
    found = re.fullmatch(r"(\d+(?:\.\d+)?)([smhd]?)", text.strip())
    if not found or float(found[1]) <= 0:
        raise argparse.ArgumentTypeError(f"not a duration: {text!r} (use 90s, 30m, 2h, 1d)")
    return float(found[1]) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[found[2]]


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog="junkfather", description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = top.add_subparsers(dest="command")

    def ownership(p: argparse.ArgumentParser) -> None:
        p.add_argument("--ttl", type=seconds, default=7200.0, help="end it after this long at most (default 2h)")
        p.add_argument("--keep-for", choices=("owner", "session"), default="owner",
                       help="owner: ends with the agent that started it; session: survives a subagent's stop and "
                            "ends with the session")
        p.add_argument("--label", help="a name for list")
        p.add_argument("--lane", help="the lane it belongs to (default LANEFATHER_LANE or the lane records)")
        p.add_argument("--agent", help=argparse.SUPPRESS)

    p = sub.add_parser("own", help="start a command as an owned process group")
    ownership(p)
    p.add_argument("--kind", choices=_resources.WRAPPABLE,
                   help="the command makes a resource that outlives it (simctl boot, docker run -d, tailscale serve "
                        "--bg, open -a 'Safari Technology Preview'): run it, then own that resource")
    p.add_argument("--remove", action="store_true", help="with --kind docker: docker rm the container after its stop")
    p.add_argument("--detach", action="store_true", help="run it under a supervisor and return at once")
    p.add_argument("--log", help="with --detach: where its output goes (default logs/<id>.log)")
    p.add_argument("--quiet", action="store_true", help="no line on start")
    p.add_argument("argv", nargs=argparse.REMAINDER)

    p = sub.add_parser("own-resource", help="own a resource that runs now (a simulator, container, serve route)")
    ownership(p)
    p.add_argument("--kind", choices=tuple(_resources.KINDS), required=True)
    p.add_argument("--id", required=True, dest="resource_id",
                   help="simulator UDID, container id or name, serve route (https:6475), or a name for --kind "
                        "command (safari-tp only through own --kind safari-tp)")
    p.add_argument("--alive", help="--kind command: exits 0 while it runs, 1 once it is gone (split as argv)")
    p.add_argument("--stop", help="--kind command: ends it (split as argv, never through a shell)")
    p.add_argument("--remove", action="store_true", help="with --kind docker: docker rm the container after its stop")

    p = sub.add_parser("adopt", help="own a process that already runs")
    ownership(p)
    p.add_argument("pid", type=int)

    p = sub.add_parser("list", help="what runs, whose it is, its age and TTL")
    p.add_argument("--all", action="store_true", help="also finished entries")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("status", help="the dashboard snapshot: running entries, unowned processes, the last sweep")
    p.add_argument("--json", action="store_true", required=True)

    p = sub.add_parser("capabilities", help="what this junkfather offers a dashboard")
    p.add_argument("--json", action="store_true", required=True)
    p = sub.add_parser("changelog", help="the installed release's changelog entries, newest first")
    p.add_argument("--json", action="store_true", required=True)
    p.add_argument("--since", help="only releases newer than this version")
    p = sub.add_parser("reap", help="end owned entries")
    p.add_argument("--id", action="append", default=[])
    p.add_argument("--owner", help="a session id (everything not handed away) or a subagent id")
    p.add_argument("--agent", help="with --owner SESSION: only this subagent's entries not kept for the session")
    p.add_argument("--lane")
    p.add_argument("--pid", type=int, help="the entry holding this pid; with --force an unowned pid sweep listed")
    p.add_argument("--force", action="store_true",
                   help="with --pid: stop an unowned process the last sweep reported; with --id: skip the "
                        "command check (the start time is still checked)")
    p.add_argument("--expired", action="store_true", help="only entries past their TTL")
    p.add_argument("--reason", default="reap")
    p.add_argument("--grace", type=float, default=_reap.GRACE)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--via", help=argparse.SUPPRESS)

    p = sub.add_parser("hand", help="hand an entry over to another session")
    p.add_argument("id")
    p.add_argument("--to", required=True, help="a Claude Code or Codex session id, or self")

    p = sub.add_parser("extend", help="give an entry a new TTL from now")
    p.add_argument("id")
    p.add_argument("--ttl", type=seconds, required=True)

    p = sub.add_parser("sweep", help="reap expired and orphaned entries; report unowned agent processes")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--quiet", action="store_true", help="print only reaps and errors")
    p.add_argument("--plist", action="store_true", help="print the hourly launchd agent")
    p.add_argument("--interval", type=int, default=3600)

    sub.add_parser("hook", help="the agent lifecycle hook (JSON on stdin)")
    p = sub.add_parser("supervise", help=argparse.SUPPRESS)
    p.add_argument("id")
    sub.add_parser("version")
    return top


def main(argv: Optional[List[str]] = None) -> int:
    args = parser().parse_args(argv)
    store = Store()
    try:
        if args.command == "own":
            command = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
            if not command:
                raise Usage("own needs a command after --")
            if args.log and not args.detach:
                raise Usage("--log goes with --detach")
            if args.remove and args.kind != "docker":
                raise Usage("--remove goes with --kind docker")
            if args.kind:
                if args.detach:
                    raise Usage("--kind runs its command attached: the resource outlives it")
                who = _launch.owner_of(store, os.environ, ["own", *command], args.agent, args.lane)
                return _resources.own_wrapped(store, args.kind, command, args.ttl, args.keep_for, args.label, who,
                                              args.remove, args.quiet)
            return _launch.own(store, command, args.ttl, args.keep_for, args.label, os.environ, args.detach,
                               args.log, args.quiet, args.agent, args.lane)
        if args.command == "own-resource":
            return own_resource_command(store, args)
        if args.command == "adopt":
            return _launch.adopt(store, args.pid, args.ttl, args.keep_for, args.label, os.environ, args.lane,
                                 args.agent)
        if args.command == "list":
            return list_command(store, args.all, args.json)
        if args.command == "status":
            print(json.dumps(status(store), sort_keys=True))
            return 0
        if args.command == "capabilities":
            print(json.dumps(capabilities(store), sort_keys=True))
            return 0
        if args.command == "changelog":
            return changelog_command(args.since)
        if args.command == "reap":
            return reap_command(store, args)
        if args.command == "hand":
            return hand_command(store, args.id, args.to)
        if args.command == "extend":
            return extend_command(store, args.id, args.ttl)
        if args.command == "sweep":
            return sweep_command(store, args)
        if args.command == "hook":
            return _hook.main()
        if args.command == "supervise":
            return _launch.supervise(store, args.id)
        if args.command == "version":
            print(f"junkfather {__version__}{commit()}")
            return 0
        parser().print_help()
        return 2
    except Usage as error:
        print(f"junkfather: {error}", file=sys.stderr)
        return 2


def commit() -> str:
    path = os.path.join(os.path.dirname(__file__), "COMMIT")
    try:
        with open(path, encoding="utf-8") as handle:
            return f" ({handle.read().strip()[:12]})"
    except OSError:
        return " (checkout)"


def own_resource_command(store: Store, args: argparse.Namespace) -> int:
    if args.remove and args.kind != "docker":
        raise Usage("--remove goes with --kind docker")
    if (args.alive or args.stop) and args.kind != "command":
        raise Usage("--alive and --stop go with --kind command")
    try:
        alive = shlex.split(args.alive) if args.alive else None
        stop = shlex.split(args.stop) if args.stop else None
    except ValueError as error:
        raise Usage(f"--alive/--stop: {error}") from None
    who = _launch.owner_of(store, os.environ, ["own-resource", args.resource_id], args.agent, args.lane)
    return _resources.own_resource(store, args.kind, args.resource_id, args.ttl, args.keep_for, args.label, who,
                                   alive, stop, args.remove)


# list

def table_or_none() -> Optional[_procs.Table]:
    try:
        return _procs.Table.read()
    except _procs.ProcError:
        return None


def row(entry: Entry, table: Optional[_procs.Table], now: float) -> Dict[str, Any]:
    alive = None
    resource = entry.get("resource") if entry.get("mode") == "resource" else None
    if table is not None and entry.get("state") in _entry.ACTIVE and resource is None:
        alive = len(_reap.members(entry, table)[0])
    owner = entry.get("owner") or {}
    return {"id": entry["id"], "state": entry.get("state"), "owner": _owner.describe(owner),
            "session": owner.get("session"), "agent": owner.get("agent"), "lane": owner.get("lane"),
            "owner_alive": _owner.alive(owner, table) if entry.get("state") in _entry.ACTIVE else None,
            "age": now - float(entry.get("created") or now), "ttl": entry.get("ttl"),
            "ttl_left": float(entry.get("expires") or now) - now, "keep": entry.get("keep"),
            "pid": entry.get("pid"), "pgid": entry.get("pgid"), "processes": alive, "label": entry.get("label"),
            "mode": entry.get("mode"), "cwd": entry.get("cwd"), "argv": entry.get("argv"), "log": entry.get("log"),
            "end": entry.get("end"), "exit": entry.get("exit"),
            "resource": {k: resource.get(k) for k in ("kind", "id", "name")} if isinstance(resource, dict) else None}


def list_command(store: Store, everything: bool, as_json: bool) -> int:
    now = time.time()
    table = table_or_none()
    rows = [row(e, table, now) for e in store.entries() if everything or e.get("state") in _entry.ACTIVE]
    if as_json:
        print(json.dumps({"v": 1, "at": now, "entries": rows}, sort_keys=True))
        return 0
    if not rows:
        print("nothing owned is running" + ("" if everything else " (--all shows finished entries)"))
        return 0
    print(f"{'ID':8} {'STATE':8} {'OWNER':34} {'AGE':>7} {'TTL':>7} {'KEEP':7} {'PID':>6} {'PROCS':>5}  COMMAND")
    for r in rows:
        ttl = _launch.duration(r["ttl_left"]) if r["state"] in _entry.ACTIVE else "-"
        owner = r["owner"] + ("" if r["owner_alive"] is not False else " (gone)")
        what = _resources.describe(r["resource"]) if r["resource"] else " ".join(r["argv"] or [])
        command = (f"[{r['label']}] " if r["label"] else "") + what
        count = "-" if r["processes"] is None else str(r["processes"])
        print(f"{r['id']:8} {str(r['state']):8} {owner[:34]:34} {_launch.duration(r['age']):>7} {ttl:>7} "
              f"{str(r['keep']):7} {str(r['pid'] or '-'):>6} {count:>5}  {command[:80]}")
    return 0


# status, capabilities (Godfather's snapshot and capabilities contracts)

STATUS_VERSION = 1


def status(store: Store) -> Dict[str, Any]:
    now = time.time()
    table = table_or_none()
    rows = [row(e, table, now) for e in store.entries() if e.get("state") in _entry.ACTIVE]
    last = _sweep_record(store, "sweep.json")
    listed = _sweep_record(store, "unowned.json")
    unowned = []
    for item in listed.get("items") or []:
        if not isinstance(item, dict):
            continue
        started = _procs.started_at(str(item.get("start") or ""))
        alive = table.alive(item.get("pid"), item.get("start")) if table is not None else None
        unowned.append({"pid": item.get("pid"), "start": item.get("start"), "cmd": item.get("cmd"),
                        "cwd": item.get("cwd"), "root": item.get("root"), "why": item.get("why"),
                        "age": None if started is None else max(0.0, now - started), "alive": alive})
    resources = [{k: item.get(k) for k in ("kind", "id", "name", "why")}
                 for item in listed.get("resources") or [] if isinstance(item, dict)]
    return {"v": STATUS_VERSION, "host": _events.host(), "time": _events.now(), "entries": rows,
            "unowned": unowned, "unowned_resources": resources,
            "sweep": {"time": _iso(last.get("at")), "result": last.get("result")},
            "errors": [] if table is not None else ["the process table cannot be read"]}


def _sweep_record(store: Store, name: str) -> Dict[str, Any]:
    from .state import read_json
    data = read_json(store.root / name)
    return data if isinstance(data, dict) else {}


def _iso(epoch: Any) -> Optional[str]:
    if not isinstance(epoch, (int, float)):
        return None
    import datetime
    moment = datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def capabilities(store: Store) -> Dict[str, Any]:
    dashboard: Dict[str, Any] = {
        "v": 1, "tag": "junk", "snapshot": {"argv": ["status", "--json"], "versions": [STATUS_VERSION], "poll": 5},
        "events": {"path": str((store.root / "events.jsonl").resolve()), "versions": [_events.VERSION],
                   "kinds": list(_events.KINDS)},
        "actions": ["reap", "extend"]}
    changelog = changelog_path()
    if os.path.isfile(changelog):
        dashboard["changelog"] = changelog
    return {"v": 1, "tool": "junkfather", "version": __version__, "commit": commit().strip(" ()"),
            "dashboard": dashboard}


def installed_commit() -> Optional[str]:
    """The installed release's commit (12 characters), None from a checkout."""
    found = commit().strip(" ()")
    return found if found and found != "checkout" else None


def changelog_path() -> str:
    return os.path.join(_launch.PACKAGE_ROOT, "CHANGELOG.md")


def changelog_command(since: Optional[str]) -> int:
    """Godfather's changelog contract: {v, tool, version, source, entries: [{version, date, section, text}]},
    newest first, the Unreleased entries first with version null, `unreleased` true and the installed commit as
    their `source`; no changelog in the release gives no entries and source null."""
    if since is not None and not _changelog.valid_version(since):
        raise Usage(f"--since takes a version like 0.1.0, not {since!r}")
    path = changelog_path()
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        source: Optional[str] = os.path.realpath(path)
    except (OSError, UnicodeDecodeError):
        text, source = "", None
    print(json.dumps({"v": 1, "tool": "junkfather", "version": __version__, "source": source,
                      "entries": _changelog.entries(text, since, installed_commit())}, sort_keys=True))
    return 0


# reap

def matches_owner(entry: Entry, owner: str, agent: Optional[str]) -> bool:
    who = entry.get("owner") or {}
    if agent:
        return who.get("session") == owner and who.get("agent") == agent and entry.get("keep") == "owner"
    if who.get("session") == owner:
        return True
    return who.get("agent") == owner and entry.get("keep") == "owner"


def reap_command(store: Store, args: argparse.Namespace) -> int:
    chosen = [bool(args.id), bool(args.owner), bool(args.lane), args.pid is not None]
    if sum(chosen) != 1:
        raise Usage("reap takes one of --id, --owner, --lane or --pid")
    if args.agent and not args.owner:
        raise Usage("--agent goes with --owner SESSION")
    now = time.time()
    running = [e for e in store.entries() if e.get("state") in _entry.OPEN]
    results: List[Dict[str, Any]] = []
    force = False
    if args.id:
        known = {e["id"] for e in running}
        missing = [i for i in args.id if i not in known]
        for entry_id in missing:
            entry = store.load(entry_id)
            results.append({"id": entry_id, "result": "skipped" if entry else "unknown",
                            "reason": f"already {entry.get('state')}" if entry else "no such entry", "pids": [],
                            "how": ""})
        selected = [e for e in running if e["id"] in args.id]
        force = args.force
    elif args.owner:
        selected = [e for e in running if matches_owner(e, args.owner, args.agent)]
    elif args.lane:
        selected = [e for e in running if (e.get("owner") or {}).get("lane") == args.lane]
    else:
        table = table_or_none()
        if table is None:
            print("junkfather reap: the process table cannot be read", file=sys.stderr)
            return 1
        selected = [e for e in running if args.pid in {p.pid for p in _reap.members(e, table)[0]}]
        if not selected:
            unowned = _reap.reap_unowned(store, args.pid, table, args.force, args.reason, args.grace, args.dry_run,
                                         args.via)
            return 1 if unowned is None else report(unowned, args)
    if args.expired:
        selected = [e for e in selected if now >= float(e.get("expires") or 0)]
    for entry in selected:
        results.append(_reap.reap(store, entry["id"], args.reason, grace=args.grace, dry_run=args.dry_run,
                                  force=force, actor=_events.tool(args.via) if args.via else None))
    return report(results, args)


def report(results: List[Dict[str, Any]], args: argparse.Namespace) -> int:
    if args.json:
        print(json.dumps({"v": 1, "results": results}, sort_keys=True))
    elif not args.quiet:
        if not results:
            print("nothing to reap")
        for r in results:
            pids = ", ".join(map(str, r["pids"])) or "-"
            print(f"{r['id']}: {r['result']} ({r['reason']}; pids {pids}{'; ' + r['how'] if r['how'] else ''})")
    return 1 if any(r["result"] in ("failed", "unknown") for r in results) else 0


# hand, extend

def hand_command(store: Store, entry_id: str, to: str) -> int:
    table = table_or_none()
    if to == "self":
        target = _owner.detect(os.environ, table, os.getcwd())
        if not target.get("session"):
            raise Usage("--to self needs an agent session (CLAUDE_CODE_SESSION_ID or CODEX_THREAD_ID)")
    else:
        target = {"kind": "claude", "session": to, "agent": None, "agent_type": None, "pid": None,
                  "pid_start": None, "lane": None}
        for record in _owner.registry() or []:
            if record.get("sessionId") == to and table is not None and _owner.registered_alive(record, table):
                proc = table.get(record["pid"])
                target.update(pid=record["pid"], pid_start=proc.start if proc else None)
                break
        else:
            target["kind"] = "codex" if not any(r.get("sessionId") == to for r in _owner.registry() or []) else "claude"
            print(f"junkfather hand: no live Claude Code session {to} in the registry; the entry ends at its TTL or "
                  "when swept as orphaned if that session never shows up", file=sys.stderr)
    target["agent"] = None
    entry = _entry.handed(store, entry_id, target)
    before: Dict[str, Any] = entry["handed"][-1]["from"] if entry is not None else {}
    if entry is None or not before:
        print(f"junkfather hand: no running entry {entry_id}", file=sys.stderr)
        return 1
    _events.write(store.root, "entry.handed", _events.subject(entry), _events.caller(),
                  {"from": {k: before.get(k) for k in ("kind", "session", "agent", "lane")},
                   "to": {k: target.get(k) for k in ("kind", "session", "lane")}})
    print(f"junkfather: {entry_id} now belongs to {_owner.describe(target)}")
    return 0


def extend_command(store: Store, entry_id: str, ttl: float) -> int:
    entry = _entry.extended(store, entry_id, ttl)
    if entry is None:
        print(f"junkfather extend: no running entry {entry_id}", file=sys.stderr)
        return 1
    _events.write(store.root, "entry.extended", _events.subject(entry), _events.caller(),
                  {"ttl": ttl, "expires": entry.get("expires")})
    print(f"junkfather: {entry_id} now ends in {_launch.duration(ttl)} at the latest")
    return 0


# sweep

def sweep_command(store: Store, args: argparse.Namespace) -> int:
    if args.plist:
        if args.interval < 60:
            raise Usage("--interval takes 60 seconds or more")
        print(_sweep.plist(_sweep.default_command(), args.interval, store.root), end="")
        return 0
    with store.lock("sweep.lock", blocking=False) as got:
        if not got:
            print("junkfather sweep: another sweep is running", file=sys.stderr)
            return 0
        result = _sweep.sweep(store, dry_run=args.dry_run)
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        output = _sweep.text(result, args.quiet)
        if output:
            print(output)
    failed = any(r["result"] == "failed" for r in result["reaped"])
    return 1 if failed or (result["errors"] and not result["unowned"] and not result["running"]) else 0
