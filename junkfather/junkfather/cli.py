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
  junkfather settings [--json] | settings check KEY VALUE | settings set KEY VALUE [--revision R]
                      | settings reset (KEY | --all) [--revision R]     (own.default_ttl, sweep.interval, reap.grace)
  junkfather hook            (Claude Code / Codex lifecycle hook: JSON on stdin)
  junkfather version | --version

Durations take seconds or 90s, 30m, 2h, 1d; without --ttl, --grace or --interval the setting applies (settings).
Exit codes (docs/cli.md): 0 done, 1 failed, 2 usage, 3 refused by the current state, 5 read-only, 6 not found;
own passes its command's status through.
State: $JUNKFATHER_STATE (default ~/.local/state/junkfather).
See README.md for how agents launch through it and docs/reference.md for the rules.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import time
import traceback
from typing import Any, Callable, Dict, List, Mapping, NoReturn, Optional

from fathers_common.cli import UsageParser

from . import __version__
from . import background as _background
from . import changelog as _changelog
from . import dashboard as _dashboard
from . import defaults
from . import entry as _entry
from . import errors
from . import events as _events
from . import hook as _hook
from . import launch as _launch
from . import owner as _owner
from . import procs as _procs
from . import release as _release
from . import reap as _reap
from . import resources as _resources
from . import settings as _settings
from . import sweep as _sweep
from .errors import NOT_FOUND, REFUSED, Refused, Usage
from .state import Store
from .text import duration, seconds


class Parser(UsageParser):
    """Keep exact option names and route usage errors through the same refusal as the command handlers."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)

    def usage_error(self, message: str) -> NoReturn:
        command = " ".join(self.prog.split()[1:]) or None
        raise Usage(message[len("argument "):] if message.startswith("argument ") else message, command)


def parser() -> Parser:
    top = Parser(prog="junkfather", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    top.add_argument("--version", action="store_true", help="print junkfather's version and exit")
    sub = top.add_subparsers(dest="command", metavar="COMMAND")

    def ownership(p: argparse.ArgumentParser) -> None:
        p.add_argument("--ttl", type=seconds,
                       help=f"end it after this long at most (default: the own.default_ttl setting, "
                            f"{duration(defaults.TTL)} unless changed)")
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
    p.add_argument("--grace", type=float, help="seconds between SIGTERM and SIGKILL (default: the reap.grace "
                                               "setting)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--via", help=argparse.SUPPRESS)
    p.add_argument("--lifecycle", type=_reap.Lifecycle, help=argparse.SUPPRESS)

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
    p.add_argument("--interval", type=int, help="seconds (default: the sweep.interval setting)")

    p = sub.add_parser("settings", help="the settings a dashboard shows and changes (own.default_ttl, "
                                        "sweep.interval, reap.grace)")
    p.add_argument("action", nargs="?", choices=("check", "set", "reset"))
    p.add_argument("key", nargs="?")
    p.add_argument("value", nargs="?")
    p.add_argument("--all", action="store_true", dest="everything", help="with reset: every setting")
    p.add_argument("--revision", help="refuse the change when the settings no longer have this revision")
    p.add_argument("--json", action="store_true")

    sub.add_parser("hook", help="the agent lifecycle hook (JSON on stdin)")
    p = sub.add_parser("supervise")  # internal (launch.py runs it): no help, so the help does not list it
    p.add_argument("id")
    p = sub.add_parser("background")  # internal: the line the hook puts before a background Bash command
    p.add_argument("tool_use_id")
    sub.add_parser("version", help="print junkfather's version")
    return top


def main(argv: Optional[List[str]] = None) -> int:
    """Parse, dispatch through COMMANDS and turn every refusal into docs/cli.md's one line and exit code."""
    words = list(sys.argv[1:] if argv is None else argv)
    command: Optional[str] = None
    try:
        if words and not words[0].startswith("-") and words[0] not in COMMANDS:
            raise Usage(f"unknown command {words[0]!r}")
        args, extra = parser().parse_known_args(words)
        command = args.command
        if extra:
            raise Usage(f"unknown option {extra[0]}" if extra[0].startswith("-") else
                        f"unexpected argument {extra[0]!r}", command)
        if args.version:
            return version_command(Store(), args)
        handler = COMMANDS.get(command or "")
        if handler is None:
            raise Usage("no command given")
        return handler(Store(), args)
    except Refused as error:
        return errors.refuse(error, command)
    except Exception as error:  # noqa: BLE001 - one line on the terminal, the traceback in the log (docs/cli.md)
        logged(words)
        errors.say(f"internal error: {type(error).__name__}: {error}")
        return errors.FAILED


def logged(words: List[str]) -> None:
    """The traceback of the exception being handled, appended to logs/junkfather.log; never raises."""
    try:
        logs = Store().logs_dir
        logs.mkdir(parents=True, exist_ok=True)
        with open(logs / "junkfather.log", "a", encoding="utf-8") as log:
            log.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} junkfather {shlex.join(words)}\n"
                      f"{traceback.format_exc()}\n")
    except OSError:
        pass


def show(data: Mapping[str, Any]) -> None:
    print(json.dumps(data, sort_keys=True))


# own, own-resource, adopt

def ttl_of(store: Store, args: argparse.Namespace) -> float:
    """--ttl, else the own.default_ttl setting as saved now."""
    return float(args.ttl) if args.ttl is not None else _settings.value("own.default_ttl", store.root)


def own_command(store: Store, args: argparse.Namespace) -> int:
    command = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
    if not command:
        raise Usage("own needs a command after --")
    if args.log and not args.detach:
        raise Usage("--log goes with --detach")
    if args.remove and args.kind != "docker":
        raise Usage("--remove goes with --kind docker")
    ttl = ttl_of(store, args)
    if not args.kind:
        return _launch.own(store, command, ttl, args.keep_for, args.label, os.environ, args.detach, args.log,
                           args.quiet, args.agent, args.lane)
    if args.detach:
        raise Usage("--kind runs its command attached: the resource outlives it")
    who = _owner.resolve(os.environ, _procs.read_or_none(), os.getcwd(), store, ["own", *command], args.lane,
                         args.agent)
    return _resources.own_wrapped(store, args.kind, command, ttl, args.keep_for, args.label, who, args.remove,
                                  args.quiet)


def own_resource_command(store: Store, args: argparse.Namespace) -> int:
    if args.remove and args.kind != "docker":
        raise Usage("--remove goes with --kind docker")
    if (args.alive or args.stop) and args.kind != "command":
        raise Usage("--alive and --stop go with --kind command")
    if args.kind == "safari-tp":
        raise Usage("own-resource refuses safari-tp: an instance that runs cannot be told from a person's own "
                    "browser; launch it through junkfather own --kind safari-tp -- open -g -a 'Safari Technology "
                    "Preview'")
    try:
        alive = shlex.split(args.alive) if args.alive else None
        stop = shlex.split(args.stop) if args.stop else None
    except ValueError as error:
        raise Usage(f"--alive/--stop: {error}") from None
    if args.kind == "command" and not (alive and stop):  # after the split: --stop ' ' is no command either
        raise Usage("--kind command needs --alive CMD and --stop CMD, each naming a command")
    ttl = ttl_of(store, args)
    who = _owner.resolve(os.environ, _procs.read_or_none(), os.getcwd(), store, ["own-resource", args.resource_id],
                         args.lane, args.agent)
    return _resources.own_resource(store, args.kind, args.resource_id, ttl, args.keep_for, args.label, who,
                                   alive, stop, args.remove)


def adopt_command(store: Store, args: argparse.Namespace) -> int:
    ttl = ttl_of(store, args)
    return _launch.adopt(store, args.pid, ttl, args.keep_for, args.label, os.environ, args.lane, args.agent)


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
    lifecycle: Optional[_reap.Lifecycle] = args.lifecycle
    if lifecycle is not None and (args.via != "hook" or not args.owner
                                  or bool(args.agent) != (lifecycle == _reap.Lifecycle.SUBAGENT_STOP)):
        raise Usage("--lifecycle requires the hook owner selector for that event")
    now = time.time()
    table = None
    if args.pid is not None:
        table = _procs.read_or_none()
        if table is None:
            raise Refused("the process table cannot be read")
        if args.force:
            return report(_reap.reap_unowned(store, args.pid, table, True, args.reason, args.grace, args.dry_run,
                                             args.via), args)
    selected, results = _reap.select(store, args.id, args.owner, args.agent, args.lane, table, args.pid,
                                     lifecycle=lifecycle)
    if table is not None and not selected:
        return report(_reap.reap_unowned(store, args.pid, table, False, args.reason, args.grace, args.dry_run,
                                         args.via), args)
    if args.expired:
        selected = [e for e in selected if _entry.due(e, now)]
    allowed = _reap.selector_guard(owner=args.owner, agent=args.agent, lane=args.lane,
                                   lifecycle=lifecycle, expired=args.expired)
    for entry in selected:
        results.append(_reap.reap(store, entry["id"], args.reason, grace=args.grace, dry_run=args.dry_run,
                                  force=bool(args.id) and args.force,
                                  actor=_events.tool(args.via) if args.via else None,
                                  lifecycle=lifecycle, allowed=allowed))
    return report(results, args)


def report(results: List[Dict[str, Any]], args: argparse.Namespace) -> int:
    """The results; exit 1 when a reap failed, else 6 when an --id named no entry."""
    failed = [r for r in results if r["result"] == "failed"]
    missing = [r for r in results if r["result"] == "unknown"]
    bad = failed or missing
    if args.json:
        show({"v": 1, "results": results})
    elif not args.quiet:
        rest = [r for r in results if r not in bad]
        if rest or not bad:  # "nothing to reap" when there were no results at all
            print(_reap.results_text(rest))
    if bad:  # why on stderr, in one line (docs/cli.md); the rest of the results stay on stdout
        more = f" (and {len(bad) - 1} more)" if len(bad) > 1 else ""
        errors.say(_reap.results_text(bad[:1]) + more)
    if failed:
        return errors.FAILED
    return NOT_FOUND if missing else 0


def open_entry(store: Store, entry_id: str) -> None:
    """Refused unless `entry_id` names an entry that runs or starts: 6 when there is none, 3 when it ended."""
    entry = store.load(entry_id)
    if entry is None:
        raise Refused(f"no entry {entry_id}", NOT_FOUND)
    if entry.get("state") not in _entry.OPEN:
        raise Refused(f"{entry_id} is not running ({entry.get('state')})", REFUSED)


def hand_command(store: Store, args: argparse.Namespace) -> int:
    open_entry(store, args.id)
    target, warning = _owner.hand_target(args.to, os.environ, _procs.read_or_none(), os.getcwd())
    if target is None:
        raise Usage(warning)
    if warning:
        errors.say(warning)
    moved = _entry.handed(store, args.id, target)
    if moved is None:
        open_entry(store, args.id)  # it ended or went meanwhile: say which
        raise Refused(f"{args.id} is not running")
    (entry, before), owner = moved, moved[0].get("owner") or {}
    _events.write(store.root, "entry.handed", _events.subject(entry), _events.caller(),
                  {"from": {k: before.get(k) for k in ("kind", "session", "agent", "lane")},
                   "to": {k: owner.get(k) for k in ("kind", "session", "lane")}})
    print(f"junkfather: {args.id} now belongs to {_owner.describe(owner)}")
    return 0


def extend_command(store: Store, args: argparse.Namespace) -> int:
    open_entry(store, args.id)
    entry = _entry.extended(store, args.id, args.ttl)
    if entry is None:
        open_entry(store, args.id)  # it ended or went meanwhile: say which
        raise Refused(f"{args.id} is not running")
    _events.write(store.root, "entry.extended", _events.subject(entry), _events.caller(),
                  {"ttl": args.ttl, "expires": entry.get("expires")})
    print(f"junkfather: {args.id} now ends in {duration(args.ttl)} at the latest")
    return 0


# sweep

def sweep_command(store: Store, args: argparse.Namespace) -> int:
    if args.plist:
        interval = int(_settings.value("sweep.interval", store.root)) if args.interval is None else args.interval
        if interval < defaults.SWEEP_INTERVAL_MIN:
            raise Usage(f"--interval takes {defaults.SWEEP_INTERVAL_MIN} seconds or more")
        print(_sweep.plist(_sweep.default_command(), interval, store.root), end="")
        return 0
    with store.lock("sweep.lock", blocking=False) as got:
        if not got:
            raise Refused("another sweep is running; try again when it ends", errors.TEMPORARY)
        result = _sweep.sweep(store, dry_run=args.dry_run)
    if args.json:
        show(result)
    else:
        output = _sweep.text(result, args.quiet)
        if output:
            print(output)
    failed = any(r["result"] == "failed" for r in result["reaped"])
    return 1 if failed or (result["errors"] and not result["unowned"] and not result["running"]) else 0


# settings

def settings_command(store: Store, args: argparse.Namespace) -> int:
    return _settings.command(store, args.action, args.key, args.value, args.everything, args.revision, args.json)


COMMANDS: Dict[str, Callable[[Store, argparse.Namespace], int]] = {
    "own": own_command, "own-resource": own_resource_command, "adopt": adopt_command, "list": list_command,
    "status": status_command, "capabilities": capabilities_command, "changelog": changelog_command,
    "reap": reap_command, "hand": hand_command, "extend": extend_command, "sweep": sweep_command,
    "hook": lambda store, args: _hook.main(), "supervise": lambda store, args: _launch.supervise(store, args.id),
    "background": lambda store, args: _background.adopt(store, args.tool_use_id, os.environ),
    "settings": settings_command, "version": version_command,
}
