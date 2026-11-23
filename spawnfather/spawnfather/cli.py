"""spawnfather: owns the long-lived processes and resources agents spawn (preview servers, browsers, drivers,
watchers, simulators, containers, worktrees) and ends them when their owner ends or their TTL passes.

  spawnfather own [--ttl 2h] [--keep-for owner|session] [--label L] [--lane L] [--detach [--log FILE]] -- CMD...
  spawnfather own --kind simulator|docker|tailscale-serve|safari-tp|worktree [--remove] [--ttl ...] -- CMD...
                  (runs CMD, then owns the resource it made: xcrun simctl boot UDID, docker run -d ..., ...)
  spawnfather own-resource --kind KIND --id ID [--remove] [--ttl 2h] [--keep-for ...] [--label L] [--lane L]
  spawnfather own-resource --kind command --id NAME --alive CMD --stop CMD [...]   (CMD split as argv, no shell)
  spawnfather own-resource --owner lane:PROJECT/ID --kind worktree|dir --id PATH [--label L] [--json]
                  (a lane's worktree or folder: no TTL, ends only by a reap naming the lane; owning it again is ok)
  spawnfather adopt PID [--ttl 2h] [--keep-for owner|session] [--label L] [--lane L]
  spawnfather list [--all] [--json]
  spawnfather status [--json]   (list, the last sweep's unowned processes and its time and result)
  spawnfather capabilities [--json]
  spawnfather changelog [--json] [--since VERSION]   (the installed release's CHANGELOG, newest first)
  spawnfather reap (--id ID... | --owner SESSION|AGENT|lane:PROJECT/ID [--agent AGENT] | --lane L
                   | --pid PID [--force] | --worktree PATH)
                   [--base REF]... [--disposable PATTERN]... [--override REASON] [--stop-inside]
                   [--expired] [--reason TEXT] [--grace S] [--dry-run] [--json] [--quiet]
  spawnfather hand (ID | --owner lane:PROJECT/ID [--kind KIND]) --to SESSION|self|lane:PROJECT/ID
  spawnfather extend ID --ttl 2h
  spawnfather sweep [--dry-run] [--json] [--quiet] | sweep --plist [--interval S]
  spawnfather settings [--json] | settings check KEY VALUE | settings set KEY VALUE [--revision R]
                       | settings reset (KEY | --all) [--revision R]     (own.default_ttl, sweep.interval, reap.grace)
  spawnfather hook            (Claude Code / Codex lifecycle hook: JSON on stdin)
  spawnfather version | --version

Durations take seconds or 90s, 30m, 2h, 1d; without --ttl, --grace or --interval the setting applies (settings).
Exit codes (docs/cli.md): 0 done, 1 failed, 2 usage, 3 refused by the current state, 5 read-only, 6 not found;
own passes its command's status through.
State: $SPAWNFATHER_STATE (default ~/.local/state/spawnfather).
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
    top = Parser(prog="spawnfather", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    top.add_argument("--version", action="store_true", help="print spawnfather's version and exit")
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
    p.add_argument("--owner", help="lane:PROJECT/ID: a lane owns it (no TTL; only a reap that names the lane ends "
                                   "it); owning a resource that lane holds already succeeds")
    p.add_argument("--json", action="store_true", help="the entry as JSON: {v, id, already, resource, owner}")

    p = sub.add_parser("adopt", help="own a process that already runs")
    ownership(p)
    p.add_argument("pid", type=int)

    p = sub.add_parser("list", help="what runs, whose it is, its age and TTL")
    p.add_argument("--all", action="store_true", help="also finished entries")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("status", help="the dashboard snapshot: running entries, unowned processes, the last sweep")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("capabilities", help="what this spawnfather offers a dashboard")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("changelog", help="the installed release's changelog entries, newest first")
    p.add_argument("--json", action="store_true")
    p.add_argument("--since", help="only releases newer than this version")
    p = sub.add_parser("reap", help="end owned entries")
    p.add_argument("--id", action="append", default=[])
    p.add_argument("--owner", help="a session id (everything not handed away), a subagent id, or lane:PROJECT/ID "
                                   "(the lane's entries and those tagged with it; processes first, then its "
                                   "worktrees, then its folders)")
    p.add_argument("--agent", help="with --owner SESSION: only this subagent's entries not kept for the session")
    p.add_argument("--lane", help="a bare lane id: entries tagged with it (never a lane owner's)")
    p.add_argument("--worktree", help="remove this worktree no entry owns, through the same guards (a requester's "
                                      "one-shot removal)")
    p.add_argument("--base", action="append", default=[],
                   help="worktrees: a ref or commit its work may be merged into, besides origin's default branch")
    p.add_argument("--disposable", action="append", default=[],
                   help="worktrees: an ignored-file pattern that may go (each path component is matched)")
    p.add_argument("--override", metavar="REASON",
                   help="worktrees and folders: lift the work guards (merged, clean, ignored files, lock, .keep, in "
                        "use), never an integrity guard; the stop event records the reason")
    p.add_argument("--stop-inside", action="store_true",
                   help="worktrees and folders: first stop the unowned processes working inside (not a terminal's, "
                        "an agent session's or anything below a live one)")
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

    p = sub.add_parser("hand", help="hand an entry over to another session or lane")
    p.add_argument("id", nargs="?")
    p.add_argument("--owner", help="lane:PROJECT/ID: hand every entry that lane owns (with --kind, of that kind)")
    p.add_argument("--kind", choices=tuple(_resources.KINDS), help="with --owner: only resources of this kind")
    p.add_argument("--to", required=True, help="a Claude Code or Codex session id, self, or lane:PROJECT/ID")
    p.add_argument("--json", action="store_true")

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
    sub.add_parser("version", help="print spawnfather's version")
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
    """The traceback of the exception being handled, appended to logs/spawnfather.log; never raises."""
    try:
        logs = Store().logs_dir
        logs.mkdir(parents=True, exist_ok=True)
        with open(logs / "spawnfather.log", "a", encoding="utf-8") as log:
            log.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} spawnfather {shlex.join(words)}\n"
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


def lane_of_option(text: str, option: str, command: str) -> Dict[str, Any]:
    """The lane owner `--owner`/`--to` names; a usage error for anything else."""
    try:
        return _owner.lane_owner(text)
    except ValueError as error:
        raise Usage(f"{option}: {error}", command) from None


def own_resource_command(store: Store, args: argparse.Namespace) -> int:
    if args.owner:
        if not _owner.is_lane_owner(args.owner):
            raise Usage("--owner takes lane:PROJECT/ID (an agent session owns what it runs without it)",
                        "own-resource")
        if args.kind not in _resources.FOLDERS:
            raise Usage("--owner lane:... goes with --kind worktree or dir", "own-resource")
        if args.ttl is not None or args.lane or args.keep_for != "owner":
            raise Usage("--owner lane:... takes no --ttl, --lane or --keep-for: the lane ends it", "own-resource")
        who = lane_of_option(args.owner, "--owner", "own-resource")
        return _resources.own_resource(store, args.kind, args.resource_id, defaults.NO_TTL, "owner", args.label, who,
                                       as_json=args.json)
    if args.kind == "dir":
        raise Usage("--kind dir goes with --owner lane:PROJECT/ID", "own-resource")
    if args.remove and args.kind != "docker":
        raise Usage("--remove goes with --kind docker")
    if (args.alive or args.stop) and args.kind != "command":
        raise Usage("--alive and --stop go with --kind command")
    if args.kind == "safari-tp":
        raise Usage("own-resource refuses safari-tp: an instance that runs cannot be told from a person's own "
                    "browser; launch it through spawnfather own --kind safari-tp -- open -g -a 'Safari Technology "
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
                                   alive, stop, args.remove, as_json=args.json)


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
    print(f"spawnfather {__version__}{_release.commit()}")
    return 0


# reap, hand, extend

def reap_command(store: Store, args: argparse.Namespace) -> int:
    chosen = [bool(args.id), bool(args.owner), bool(args.lane), args.pid is not None, bool(args.worktree)]
    if sum(chosen) != 1:
        raise Usage("reap takes one of --id, --owner, --lane, --pid or --worktree")
    if args.agent and not args.owner:
        raise Usage("--agent goes with --owner SESSION")
    lane_owner = _owner.is_lane_owner(args.owner)
    folders = bool(args.base or args.disposable or args.override or args.stop_inside)
    if folders and not (args.id or lane_owner or args.worktree):
        raise Usage("--base, --disposable, --override and --stop-inside go with --id, --owner lane:... or "
                    "--worktree")
    if args.override is not None and not args.override.strip():
        raise Usage("--override needs a reason")
    evidence = {"bases": args.base, "disposable": args.disposable, "override": args.override} if folders else None
    if args.worktree:
        if args.expired or args.force or args.lifecycle is not None:
            raise Usage("--worktree takes no --expired, --force or --lifecycle")
        actor = _events.tool(args.via) if args.via else None
        return report([_resources.reap_worktree(store, args.worktree, args.reason,
                                                _settings.value("reap.grace", store.root) if args.grace is None
                                                else args.grace, evidence or {}, args.stop_inside, args.dry_run,
                                                actor)], args)
    if lane_owner:
        if args.agent or args.lifecycle is not None:
            raise Usage("--owner lane:... takes no --agent or --lifecycle")
        lane_of_option(args.owner, "--owner", "reap")
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
    if table is not None and not selected and not results:
        return report(_reap.reap_unowned(store, args.pid, table, False, args.reason, args.grace, args.dry_run,
                                         args.via), args)
    if args.expired:
        selected = [e for e in selected if _entry.due(e, now)]
    held = _reap.lane_worktrees(selected, _owner.lane_owner(args.owner)) if lane_owner else None
    allowed = _reap.selector_guard(owner=args.owner, agent=args.agent, lane=args.lane,
                                   lifecycle=lifecycle, expired=args.expired, held=held)
    for entry in selected:
        results.append(_reap.reap(store, entry["id"], args.reason, grace=args.grace, dry_run=args.dry_run,
                                  force=bool(args.id) and args.force,
                                  actor=_events.tool(args.via) if args.via else None,
                                  lifecycle=lifecycle, allowed=allowed, evidence=evidence,
                                  stopping=args.stop_inside))
        if lane_owner and not args.dry_run and results[-1]["result"] == "failed":
            break  # a lane's folders go after its worktrees and processes: never past one that stays
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
    if bool(args.id) == bool(args.owner):
        raise Usage("hand takes an entry ID or --owner lane:PROJECT/ID", "hand")
    if args.kind and not args.owner:
        raise Usage("--kind goes with --owner", "hand")
    if args.id:
        open_entry(store, args.id)
    if _owner.is_lane_owner(args.to):
        target: Optional[Dict[str, Any]] = lane_of_option(args.to, "--to", "hand")
        warning = ""
    else:
        target, warning = _owner.hand_target(args.to, os.environ, _procs.read_or_none(), os.getcwd())
    if target is None:
        raise Usage(warning)
    if warning:
        errors.say(warning)
    if args.owner:
        if not _owner.is_lane_owner(args.owner):
            raise Usage("--owner takes lane:PROJECT/ID", "hand")
        source = lane_of_option(args.owner, "--owner", "hand")
        ids = [e["id"] for e in store.entries() if e.get("state") in _entry.OPEN
               and _owner.same_lane(e.get("owner") or {}, source)
               and (not args.kind or (e.get("resource") or {}).get("kind") == args.kind)]
    else:
        ids = [args.id]
    handed: List[str] = []
    for entry_id in ids:
        moved = _entry.handed(store, entry_id, target)
        if moved is None:
            if args.id:
                open_entry(store, entry_id)  # it ended or went meanwhile: say which
                raise Refused(f"{entry_id} is not running")
            continue
        (entry, before), owner = moved, moved[0].get("owner") or {}
        if owner.get("kind") == "lane":  # a lane's entries have no TTL
            _entry.no_ttl(store, entry_id)
        _events.write(store.root, "entry.handed", _events.subject(entry), _events.caller(),
                      {"from": {k: before.get(k) for k in ("kind", "session", "agent", "lane", "lane_project")
                                if k != "lane_project" or before.get(k)},
                       "to": {k: owner.get(k) for k in ("kind", "session", "lane", "lane_project")
                              if k != "lane_project" or owner.get(k)}})
        handed.append(entry_id)
    if args.json:
        show({"v": 1, "handed": handed, "to": target})
    elif not handed:
        print(f"spawnfather: {_owner.describe(lane_of_option(args.owner, '--owner', 'hand'))} owns nothing to hand")
    else:
        for entry_id in handed:
            print(f"spawnfather: {entry_id} now belongs to {_owner.describe(target)}")
    return 0


def extend_command(store: Store, args: argparse.Namespace) -> int:
    open_entry(store, args.id)
    loaded = store.load(args.id) or {}
    if (loaded.get("owner") or {}).get("kind") == "lane":
        raise Refused(f"{args.id} belongs to {_owner.describe(loaded.get('owner') or {})}, which has no TTL: only a "
                      "reap that names the lane ends it", REFUSED)
    entry = _entry.extended(store, args.id, args.ttl)
    if entry is None:
        open_entry(store, args.id)  # it ended or went meanwhile: say which
        raise Refused(f"{args.id} is not running")
    _events.write(store.root, "entry.extended", _events.subject(entry), _events.caller(),
                  {"ttl": args.ttl, "expires": entry.get("expires")})
    print(f"spawnfather: {args.id} now ends in {duration(args.ttl)} at the latest")
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
