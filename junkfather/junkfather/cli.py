"""junkfather: owns the long-lived processes agents start (preview servers, browsers, drivers, watchers) and ends
them when their owner ends or their TTL passes.

  junkfather own [--ttl 2h] [--keep-for owner|session] [--label L] [--lane L] [--detach [--log FILE]] -- CMD...
  junkfather own --kind simulator|docker|tailscale-serve|safari-tp [--remove] [--ttl ...] -- CMD...
                 (runs CMD, then owns the resource it made: xcrun simctl boot UDID, docker run -d ..., ...)
  junkfather own-resource --kind KIND --id ID [--remove] [--ttl 2h] [--keep-for ...] [--label L] [--lane L]
  junkfather own-resource --kind command --id NAME --alive CMD --stop CMD [...]   (CMD split as argv, no shell)
  junkfather adopt PID [--ttl 2h] [--keep-for owner|session] [--label L] [--lane L]
  junkfather list [--all] [--json]
  junkfather status [--json]   (list, the last sweep's unowned processes and its time and result)
  junkfather capabilities [--json]
  junkfather changelog [--json] [--since VERSION]   (the installed release's CHANGELOG, newest first)
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
import shlex
import sys
import time
from typing import Any, Callable, Dict, List, Optional

from . import __version__
from . import changelog as _changelog
from . import dashboard as _dashboard
from . import defaults
from . import entry as _entry
from . import events as _events
from . import hook as _hook
from . import launch as _launch
from . import owner as _owner
from . import procs as _procs
from . import release as _release
from . import reap as _reap
from . import resources as _resources
from . import sweep as _sweep
from .state import Store
from .text import duration, seconds


class Usage(Exception):
    pass


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog="junkfather", description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = top.add_subparsers(dest="command")

    def ownership(p: argparse.ArgumentParser) -> None:
        p.add_argument("--ttl", type=seconds, default=defaults.TTL,
                       help=f"end it after this long at most (default {duration(defaults.TTL)})")
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
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("capabilities", help="what this junkfather offers a dashboard")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("changelog", help="the installed release's changelog entries, newest first")
    p.add_argument("--json", action="store_true")
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
    p.add_argument("--grace", type=float, default=defaults.GRACE)
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
    p.add_argument("--interval", type=int, default=defaults.SWEEP_INTERVAL)

    sub.add_parser("hook", help="the agent lifecycle hook (JSON on stdin)")
    p = sub.add_parser("supervise", help=argparse.SUPPRESS)
    p.add_argument("id")
    sub.add_parser("version")
    return top


def main(argv: Optional[List[str]] = None) -> int:
    args = parser().parse_args(argv)
    handler = COMMANDS.get(args.command or "")
    if handler is None:
        parser().print_help()
        return 2
    try:
        return handler(Store(), args)
    except Usage as error:
        print(f"junkfather: {error}", file=sys.stderr)
        return 2


def show(data: Dict[str, Any]) -> None:
    print(json.dumps(data, sort_keys=True))


# own, own-resource, adopt

def own_command(store: Store, args: argparse.Namespace) -> int:
    command = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
    if not command:
        raise Usage("own needs a command after --")
    if args.log and not args.detach:
        raise Usage("--log goes with --detach")
    if args.remove and args.kind != "docker":
        raise Usage("--remove goes with --kind docker")
    if not args.kind:
        return _launch.own(store, command, args.ttl, args.keep_for, args.label, os.environ, args.detach, args.log,
                           args.quiet, args.agent, args.lane)
    if args.detach:
        raise Usage("--kind runs its command attached: the resource outlives it")
    who = _owner.resolve(os.environ, _procs.read_or_none(), os.getcwd(), store, ["own", *command], args.lane,
                         args.agent)
    return _resources.own_wrapped(store, args.kind, command, args.ttl, args.keep_for, args.label, who, args.remove,
                                  args.quiet)


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
    who = _owner.resolve(os.environ, _procs.read_or_none(), os.getcwd(), store, ["own-resource", args.resource_id],
                         args.lane, args.agent)
    return _resources.own_resource(store, args.kind, args.resource_id, args.ttl, args.keep_for, args.label, who,
                                   alive, stop, args.remove)


def adopt_command(store: Store, args: argparse.Namespace) -> int:
    return _launch.adopt(store, args.pid, args.ttl, args.keep_for, args.label, os.environ, args.lane, args.agent)


# list, status, capabilities, changelog

def list_command(store: Store, args: argparse.Namespace) -> int:
    listed = _dashboard.listing(store, args.all)
    if args.json:
        show(listed)
    elif not listed["entries"]:
        print("nothing owned is running" + ("" if args.all else " (--all shows finished entries)"))
    else:
        print(_dashboard.rows_text(listed["entries"]))
    return 0


def status_command(store: Store, args: argparse.Namespace) -> int:
    snapshot = _dashboard.status(store)
    if args.json:
        show(snapshot)
    else:
        print(_dashboard.status_text(snapshot))
    return 0


def capabilities_command(store: Store, args: argparse.Namespace) -> int:
    found = _dashboard.capabilities(store)
    if args.json:
        show(found)
    else:
        print(_dashboard.capabilities_text(found))
    return 0


def changelog_command(store: Store, args: argparse.Namespace) -> int:
    try:
        found = _changelog.contract(args.since)
    except ValueError as error:
        raise Usage(str(error)) from None
    if args.json:
        show(found)
    else:
        print(_changelog.text(found["entries"]) or "no changelog entries")
    return 0


def version_command(store: Store, args: argparse.Namespace) -> int:
    print(f"junkfather {__version__}{_release.commit()}")
    return 0


# reap, hand, extend

def reap_command(store: Store, args: argparse.Namespace) -> int:
    chosen = [bool(args.id), bool(args.owner), bool(args.lane), args.pid is not None]
    if sum(chosen) != 1:
        raise Usage("reap takes one of --id, --owner, --lane or --pid")
    if args.agent and not args.owner:
        raise Usage("--agent goes with --owner SESSION")
    now = time.time()
    table = None
    if args.pid is not None:
        table = _procs.read_or_none()
        if table is None:
            print("junkfather reap: the process table cannot be read", file=sys.stderr)
            return 1
    selected, results = _reap.select(store, args.id, args.owner, args.agent, args.lane, table, args.pid)
    if table is not None and not selected:
        unowned = _reap.reap_unowned(store, args.pid, table, args.force, args.reason, args.grace, args.dry_run,
                                     args.via)
        return 1 if unowned is None else report(unowned, args)
    if args.expired:
        selected = [e for e in selected if now >= float(e.get("expires") or 0)]
    for entry in selected:
        results.append(_reap.reap(store, entry["id"], args.reason, grace=args.grace, dry_run=args.dry_run,
                                  force=bool(args.id) and args.force,
                                  actor=_events.tool(args.via) if args.via else None))
    return report(results, args)


def report(results: List[Dict[str, Any]], args: argparse.Namespace) -> int:
    if args.json:
        show({"v": 1, "results": results})
    elif not args.quiet:
        print(_reap.results_text(results))
    return 1 if any(r["result"] in ("failed", "unknown") for r in results) else 0


def hand_command(store: Store, args: argparse.Namespace) -> int:
    target, warning = _owner.hand_target(args.to, os.environ, _procs.read_or_none(), os.getcwd())
    if target is None:
        raise Usage(warning)
    if warning:
        print(f"junkfather hand: {warning}", file=sys.stderr)
    moved = _entry.handed(store, args.id, target)
    if moved is None or not moved[1]:
        print(f"junkfather hand: no running entry {args.id}", file=sys.stderr)
        return 1
    (entry, before), owner = moved, moved[0]["owner"]
    _events.write(store.root, "entry.handed", _events.subject(entry), _events.caller(),
                  {"from": {k: before.get(k) for k in ("kind", "session", "agent", "lane")},
                   "to": {k: owner.get(k) for k in ("kind", "session", "lane")}})
    print(f"junkfather: {args.id} now belongs to {_owner.describe(owner)}")
    return 0


def extend_command(store: Store, args: argparse.Namespace) -> int:
    entry = _entry.extended(store, args.id, args.ttl)
    if entry is None:
        print(f"junkfather extend: no running entry {args.id}", file=sys.stderr)
        return 1
    _events.write(store.root, "entry.extended", _events.subject(entry), _events.caller(),
                  {"ttl": args.ttl, "expires": entry.get("expires")})
    print(f"junkfather: {args.id} now ends in {duration(args.ttl)} at the latest")
    return 0


# sweep

def sweep_command(store: Store, args: argparse.Namespace) -> int:
    if args.plist:
        if args.interval < defaults.SWEEP_INTERVAL_MIN:
            raise Usage(f"--interval takes {defaults.SWEEP_INTERVAL_MIN} seconds or more")
        print(_sweep.plist(_sweep.default_command(), args.interval, store.root), end="")
        return 0
    with store.lock("sweep.lock", blocking=False) as got:
        if not got:
            print("junkfather sweep: another sweep is running", file=sys.stderr)
            return 0
        result = _sweep.sweep(store, dry_run=args.dry_run)
    if args.json:
        show(result)
    else:
        output = _sweep.text(result, args.quiet)
        if output:
            print(output)
    failed = any(r["result"] == "failed" for r in result["reaped"])
    return 1 if failed or (result["errors"] and not result["unowned"] and not result["running"]) else 0


COMMANDS: Dict[str, Callable[[Store, argparse.Namespace], int]] = {
    "own": own_command, "own-resource": own_resource_command, "adopt": adopt_command, "list": list_command,
    "status": status_command, "capabilities": capabilities_command, "changelog": changelog_command,
    "reap": reap_command, "hand": hand_command, "extend": extend_command, "sweep": sweep_command,
    "hook": lambda store, args: _hook.main(), "supervise": lambda store, args: _launch.supervise(store, args.id),
    "version": version_command,
}
