#!/usr/bin/env python3
"""slotfather-sweep: find, and with --live stop, processes left behind on the machine (#662); `slotfather sweep`
runs it.

  slotfather sweep [--live [--dev-servers]] [--json] [--quiet] [--pid PID]... [--no-log]
  slotfather sweep config [KEY VALUE]... | config reset
  slotfather sweep --plist [--live [--dev-servers]] [--interval SECONDS]

Classes (the 2026-11-03 incident, straylight#661):
  removed     a process whose working directory, or a path in its command, is in a directory that no longer
              exists (a removed worktree or temporary directory), older than removed_age: --live stops it
  orphan      a process adopted by launchd/init (ppid 1) of a known dev kind (verify and test runners, vitest,
              Playwright, agent-browser, headless Chromium, cargo/swift test, `tail -f`), older than orphan_age:
              --live stops it
  dev-server  storybook/vite/next dev servers older than dev_server_age whose parent chain reaches no live
              session and in whose project no session works: reported; --live --dev-servers stops them
  container   containers in a restart loop: reported only
  memory      while swap or memory runs short, the largest resident processes with their class: reported only

A live session's child (an agent's background shell, a terminal's job) is reported, never stopped, whatever its
class, except a removed-class one whose working directory is gone and which is older than session_removed_age (6 h):
the worktree it waited on will not come back. A `pgrep -f PATTERN` wait loop whose own command line matches
PATTERN gets a note (Linux's pgrep finds the loop itself; macOS's finds other waits for the same pattern).

Never touched: a process inside a live lease of this queue (the holder's tree, or carrying the lease's
SLOTFATHER_LEASE, as `status --json --pid` decides; one whose environment cannot be read while leases are live is
unknown, so spared), or one whose tree holds such a process or a session; other
users' processes; processes with a terminal; agent and terminal sessions (claude, codex, tmux, sshd); system
and app bundle processes; launchd jobs and systemd services' main processes; the sweep and its ancestors. A stop
sends SIGTERM to the process and its descendants and SIGKILL to those still there after `grace` seconds, each
time only to a pid whose start time is unchanged. Without --live nothing is signalled. Every sweep appends a line
to sweep.jsonl beside the queue (`slotfather stats` counts them); stops are also written to queue.log. Settings
(ages, grace, the enabled classes) live in sweep.json beside the queue: `slotfather sweep config`.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
from pathlib import Path
import plistlib
import re
import sys
import time
from typing import Any, cast

from . import config as _config
from . import events as _events
from . import leases as _leases
from . import process as _process
from . import state as _state
from . import sweep_os, sweep_plan
from .sweep_os import SweepError, stop
from .sweep_plan import CLASSES, Carriers, Snapshot


VERSION = 1
DEFAULTS: dict[str, Any] = {
    "classes": list(CLASSES), "removed_age": 600.0, "orphan_age": 7200.0, "dev_server_age": 28800.0,
    "session_removed_age": 21600.0, "grace": 10.0, "container_minutes": 30.0, "container_restarts": 3.0,
    "swap_free_mb": 1024.0, "swap_free_pct": 20.0, "memory_top": 8.0}
DURATIONS = ("removed_age", "orphan_age", "dev_server_age", "session_removed_age", "grace", "container_minutes")
LOG_MAX = 4 << 20  # bytes of sweep.jsonl before it becomes sweep.jsonl.1
LABEL = "com.slotfather.sweep"
USAGE = ("usage: slotfather sweep [--live [--dev-servers]] [--json] [--quiet] [--pid PID]... [--no-log]\n"
         "       slotfather sweep config [KEY VALUE]... | config reset\n"
         "       slotfather sweep --plist [--live [--dev-servers]] [--interval SECONDS]")


# Leases: never touch what runs inside one

Labelled = list[tuple[_process.Lease, str]]


def leases(directory: Path, snap: Snapshot | None = None) -> tuple[dict[int, int], Labelled]:
    """The live leases, each with its label, of the queue (reaped as `status` does) and the earlier queues, and the
    process table they were judged on (pid -> ppid). A table ps could not give raises (fathers#121): this sweep
    stops nothing."""
    with _state.locked(directory):
        processes = _leases.process_table(directory)
        live = _leases.reap(directory, processes)
    found: Labelled = [(_process.Lease(str(path), entry.get("pid")), str(entry.get("label") or path.stem))
                       for path, entry in live]
    found += [(_process.Lease(str(path), entry.get("pid")), str(entry.get("label") or path.stem))
              for path, entry, _ in _leases.legacy_holders(processes)]
    return _process.parents_of(processes), found


def protection(parents: dict[int, int], found: Labelled) -> tuple[dict[int, str], Carriers | None]:
    """What the leases protect, by the rule `status --json --pid` and `procs` follow (process.lease_membership()):
    pid -> the label of the live lease whose holder's tree it is in, and `carriers(pids)`, which gives those of
    pids outside every tree whose environment carries a live lease's SLOTFATHER_LEASE (pid -> its label) or cannot
    be read (pid -> None: unknown, so spared)."""
    held = [lease for lease, _ in found]
    labels = [label for _, label in found]
    trees = _process.lease_membership(parents, held, parents, markers=None)
    protected = {pid: labels[member[-1][0]] for pid, member in trees.leases.items() if member}

    def carriers(pids: list[int]) -> dict[int, str | None]:
        membership = _process.lease_membership(parents, held, pids)
        marked: dict[int, str | None] = {}
        for pid, member in membership.leases.items():
            env = [index for index, via in member if via == "env"]
            if env:
                marked[pid] = labels[env[0]]
        marked.update((pid, None) for pid in membership.unknown if pid not in marked)
        return marked
    return protected, (carriers if found else None)


# Settings

def settings(directory: Path | None = None) -> dict[str, Any]:
    """sweep.json beside the queue over DEFAULTS. Unknown keys are ignored with a "warnings" entry (a newer
    slotfather may have written them); a file that cannot be read or holds a bad value gives the defaults with
    "error", and a live sweep then stops nothing."""
    found = dict(DEFAULTS, classes=list(DEFAULTS["classes"]), saved=False, warnings=[])
    try:
        data = json.loads(((directory or _config.state_dir()) / "sweep.json").read_text())
        if not isinstance(data, dict):
            raise ValueError(f"not sweep settings: {data!r}")
        unknown = sorted(set(data) - set(DEFAULTS))
        if unknown:
            found["warnings"].append(f"sweep.json: unknown setting(s) {', '.join(unknown)} ignored")
        found.update(checked({k: v for k, v in data.items() if k in DEFAULTS}), saved=True)
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError) as error:
        found["error"] = f"sweep.json ignored: {error}"
    return found


def seconds_of(text: str) -> float:
    found = re.fullmatch(r"(\d+(?:\.\d+)?)([smhd]?)", text.strip())
    if not found:
        raise ValueError(f"not a duration: {text!r} (seconds, or 90s, 30m, 2h, 1d)")
    return float(found[1]) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[found[2]]


def checked(data) -> dict:
    if not isinstance(data, dict) or set(data) - set(DEFAULTS):
        raise ValueError(f"not sweep settings: {data!r}")
    found = {}
    for key, value in data.items():
        if key == "classes":
            if not isinstance(value, list) or set(value) - set(CLASSES):
                raise ValueError(f"classes are some of {', '.join(CLASSES)}, not {value!r}")
            found[key] = [c for c in CLASSES if c in value]
        elif type(value) not in (int, float) or value < 0 or (key == "memory_top" and value < 1):
            raise ValueError(f"{key} must be a number of at least {1 if key == 'memory_top' else 0}, not {value!r}")
        else:
            found[key] = float(value)
    return found


def config_command(argv: list[str], directory: Path) -> int:
    if argv == ["reset"]:
        _state.require_own(directory, "a sweep settings change")
        with _state.locked(directory):
            (directory / "sweep.json").unlink(missing_ok=True)
    elif argv:
        if len(argv) % 2:
            raise SweepError("config takes KEY VALUE pairs, or reset")
        changes = {}
        for key, value in zip(argv[::2], argv[1::2]):
            key = key.replace("-", "_")
            if key not in DEFAULTS:
                raise SweepError(f"unknown setting {key}; settings: {', '.join(DEFAULTS)}")
            try:
                if key == "classes":
                    changes[key] = [] if value in ("none", "") else [c.strip() for c in value.split(",") if c.strip()]
                else:
                    changes[key] = seconds_of(value) if key in DURATIONS else float(value)
            except ValueError as error:
                raise SweepError(str(error)) from None
        _state.require_own(directory, "a sweep settings change")
        with _state.locked(directory):
            current = settings(directory)
            if "error" in current:  # a broken file is replaced by the defaults and these changes
                current = dict(DEFAULTS, classes=list(DEFAULTS["classes"]))
            saved = {k: v for k, v in current.items() if k in DEFAULTS}
            try:
                values = checked({**saved, **changes})
            except ValueError as error:
                raise SweepError(str(error)) from None
            _state.write(directory / "sweep.json", values)
    found = settings(directory)
    for problem in found["warnings"] + ([found["error"]] if "error" in found else []):
        print(f"slotfather sweep: {problem}", file=sys.stderr)
    print(f"sweep settings ({'saved in ' + str(directory / 'sweep.json') if found['saved'] else 'defaults'}):")
    for key in DEFAULTS:
        value = found[key]
        text = ",".join(value) or "none" if key == "classes" else (
            _config.duration(value) if key in DURATIONS and key != "container_minutes" else f"{value:g}")
        print(f"  {key:<18} {text}")
    return 0


def read_state(directory: Path) -> dict[str, Any]:
    try:
        data = json.loads((directory / "sweep-state.json").read_text())
    except (OSError, ValueError):
        return {}
    gone = data.get("gone_since") if isinstance(data, dict) else None
    return {"gone_since": {str(k): float(v) for k, v in gone.items() if isinstance(v, (int, float))}} \
        if isinstance(gone, dict) else {}


# The sweep

def sweep(directory: Path, live: bool = False, dev_servers: bool = False, only: set[int] | None = None,
          logged: bool = True) -> dict[str, Any]:
    found_settings = settings(directory)
    refused = live and "error" in found_settings
    if refused:
        live = False  # settings that cannot be read stop nothing: the sweep reports what it would do
    snap = sweep_os.snapshot()
    protected, carriers = protection(*leases(directory, snap))
    before = read_state(directory).get("gone_since", {})
    items = sweep_plan.plan(snap, found_settings, protected, carriers, only, live, dev_servers, before)
    if live:
        for item in items:
            if item["action"] == "stop":
                item.update(stop(item, snap, found_settings["grace"]))
                item["action"] = item.pop("result")
                _state.log(directory, f"sweep {item['action']} {item['class']} pid {item['pid']} ({item['kind']}, "
                                  f"age {_config.duration(item['age'])}, {len(item['tree'])} process(es)): "
                                  f"{item['cmd'][:120]}")
                swept = item
                _events.emit(directory, "process.swept", {"type": "process", "id": str(swept["pid"]), "pid": swept["pid"]},
                             {"action": swept["action"], "class": swept["class"], "kind": swept["kind"],
                              "age": round(float(swept["age"])), "processes": len(cast(list[Any], swept["tree"])),
                              "cmd": _events.command(swept["cmd"])},
                             dict(_events.actor(kind="tool"), via="sweep"), level="warn")
    enabled = set(found_settings["classes"])
    docker: dict[str, Any] = sweep_os.containers(found_settings) if "container" in enabled and only is None else \
        {"available": None, "items": []}
    memory = sweep_os.memory_report(sweep_os.memory_facts(), snap, found_settings, items, protected) if "memory" in enabled else {}
    waits = sweep_plan.self_matching_waits(snap, only)
    counts = {c: sum(i["class"] == c for i in items) for c in ("removed", "orphan", "dev-server")}
    counts["container"] = len(docker["items"])
    report: dict[str, Any] = {"v": VERSION, "at": round(snap.moment, 3), "live": live, "dev_servers": dev_servers,
              "only": sorted(only) if only is not None else None,
              "settings": {k: found_settings[k] for k in DEFAULTS},
              "settings_error": found_settings.get("error"), "live_refused": refused,
              "notes": snap.notes + found_settings["warnings"] + (
                  [found_settings["error"] + ("; the live sweep stopped nothing" if refused else "")]
                  if "error" in found_settings else []),
              "items": items, "containers": docker, "memory": memory, "found": counts,
              "stopped": {c: sum(i["class"] == c and i["action"] in ("stopped", "killed") for i in items)
                          for c in ("removed", "orphan", "dev-server")},
              "would_stop": sum(i["action"] == "would-stop" for i in items),
              "spared": sum(i["action"] == "spared" for i in items),
              "under_session": sum(bool(i.get("under_session")) for i in items),
              "self_matching": waits,
              "failed": sum(i["action"] == "failed" for i in items),
              "freed_mb": round(sum(i["rss_mb"] for i in items if i["action"] in ("stopped", "killed")), 1)}
    if logged and _state.foreign_owner(directory) is None:
        with contextlib.suppress(OSError):
            directory.mkdir(parents=True, exist_ok=True)
            _state.write(directory / "sweep-state.json", {"v": VERSION, "gone_since": sweep_plan.track_gone(snap, before)})
            _state.append_bounded(directory / "sweep.jsonl", json.dumps(report, sort_keys=True) + "\n", LOG_MAX)
    return report


ACTIONS = {"would-stop": "would stop", "stop": "stop", "stopped": "stopped", "killed": "killed", "gone": "gone",
           "failed": "FAILED", "spared": "spared", "report": "report"}


def text(report: dict, quiet: bool = False) -> str:
    lines = []
    items = report["items"]
    if quiet:
        return "\n".join(f"slotfather sweep: {ACTIONS[i['action']]} {i['class']} pid {i['pid']} {i['kind']}: "
                         f"{i['cmd'][:120]}" for i in items if i["action"] in ("stopped", "killed", "failed"))
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(report["at"]))
    mode = "live" + (" with dev servers" if report["dev_servers"] else "") if report["live"] else \
        ("dry run (live refused: settings)" if report.get("live_refused") else "dry run")
    found = report["found"]
    lines.append(f"slotfather sweep · {mode} · {when} · removed {found['removed']}, orphans {found['orphan']}, "
                 f"dev servers {found['dev-server']}, looping containers {found['container']}"
                 + (f" · only pids {','.join(map(str, report['only']))}" if report["only"] else ""))
    for item in items:
        if item.get("under_session"):
            continue
        lines.append(f"  {item['class']:<10} {ACTIONS[item['action']]:<10} pid {item['pid']:<6} "
                     f"{_config.duration(item['age']):>7} {item['rss_mb']:>7.0f} MB  {item['kind']:<14} "
                     f"{item['cmd'][:110]}")
        detail = item["reason"] + (f"; {len(item['tree'])} process(es)" if len(item["tree"]) > 1 else "")
        lines.append(f"  {'':<10} {'':<10} {detail}")
    if not items:
        lines.append("  no removed-directory processes, orphans or stale dev servers")
    sessions = {}
    for item in items:
        if item.get("under_session"):
            sessions.setdefault(item.get("session"), []).append(item)
    for session, group in sessions.items():
        lines.append(f"  under live session pid {session}: {len(group)} child(ren) reported only "
                     f"(stopped only once a sweep has seen their cwd gone {_config.duration(report['settings']['session_removed_age'])})")
        for item in group:
            gone = f" cwd gone {_config.duration(item['gone_for'])}" if item.get("cwd_gone") else ""
            lines.append(f"    {item['class']:<10} pid {item['pid']:<6} {_config.duration(item['age']):>7}{gone}  "
                         f"{sweep_plan.loop_head(item['cmd'])[:100]}")
    patterns = {}
    for wait in report.get("self_matching") or []:
        patterns.setdefault(wait["pattern"], []).append(wait)
    for pattern, group in patterns.items():
        oldest = max((w["age"] or 0) for w in group)
        pids = [w["pid"] for w in group]
        lines.append(f"  note: self-matching wait: pgrep -f {pattern!r} matches its own command line; "
                     f"{len(group)} wait(s), oldest {_config.duration(oldest)}, pids {', '.join(map(str, pids[:8]))}"
                     + ("…" if len(pids) > 8 else "") + f": {group[0]['cmd'][:80]}")
    docker = report["containers"]
    if docker.get("available") is False:
        lines.append(f"  containers: not checked ({docker.get('note')})")
    for item in docker["items"]:
        lines.append(f"  container  report     {item['name']} ({item['project'] or '-'}): {item['status']}, "
                     f"{item['restarts']} restarts, policy {item['policy']}, exit {item['exit_code']}")
    memory = report["memory"]
    if memory:
        swap = "" if memory.get("swap_total_mb") is None else \
            f"swap {memory['swap_total_mb'] - memory['swap_free_mb']} of {memory['swap_total_mb']} MB used"
        if memory.get("short"):
            lines.append(f"  memory short: {memory['short']}" + (f"; {swap}" if swap else "") + "; largest:")
            for entry in memory["top"]:
                lines.append(f"    pid {entry['pid']:<6} {entry['rss_mb']:>6} MB  {entry['class']:<14} "
                             f"{entry['cmd'][:100]}")
        elif swap:
            lines.append(f"  memory: {swap}, not short")
    for note in report["notes"]:
        lines.append(f"  note: {note}")
    if report["live"]:
        stopped = sum(report["stopped"].values())
        lines.append(f"stopped {stopped} tree(s), {report['freed_mb']:.0f} MB resident; spared {report['spared']} "
                     f"({report.get('under_session', 0)} under a live session), failed {report['failed']}")
    else:
        lines.append(f"dry run: nothing was signalled; {report['would_stop']} would be stopped with --live"
                     + ("" if report["dev_servers"] else " (dev servers only with --live --dev-servers)")
                     + f"; spared {report['spared']} ({report.get('under_session', 0)} under a live session)")
    return "\n".join(lines)


def plist(live: bool, dev_servers: bool, interval: int, directory: Path) -> str:
    """A launchd agent that runs the sweep every `interval` seconds, dry-run unless `live`."""
    command = [sys.executable, str(Path(__file__).resolve().with_name("__main__.py")), "sweep", "--quiet"]
    if live:
        command.append("--live")
    if dev_servers:
        command.append("--dev-servers")
    environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")}
    if os.environ.get("SLOTFATHER_DIR"):
        environment["SLOTFATHER_DIR"] = os.environ["SLOTFATHER_DIR"]
    data = {"Label": LABEL, "ProgramArguments": command, "StartInterval": interval, "RunAtLoad": True,
            "ProcessType": "Background", "LowPriorityIO": True, "Nice": 10, "EnvironmentVariables": environment,
            "StandardOutPath": str(directory / "sweep.out"), "StandardErrorPath": str(directory / "sweep.out")}
    return plistlib.dumps(data).decode()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    directory = _config.state_dir()
    try:
        if argv[:1] == ["config"]:
            return config_command(argv[1:], directory)
        live = dev = as_json = quiet = as_plist = False
        logged = True
        only, interval = None, 3600
        while argv:
            option = argv.pop(0)
            if option == "--live":
                live = True
            elif option == "--dev-servers":
                dev = True
            elif option == "--json":
                as_json = True
            elif option == "--quiet":
                quiet = True
            elif option == "--no-log":
                logged = False
            elif option == "--plist":
                as_plist = True
            elif option == "--interval" and argv and argv[0].isdigit() and int(argv[0]) >= 60:
                interval = int(argv.pop(0))
            elif option == "--pid" and argv and argv[0].isdigit():
                only = (only or set()) | {int(argv.pop(0))}
            elif option in ("-h", "--help"):
                print(__doc__)
                return 0
            else:
                raise SweepError(f"unknown or incomplete option {option}")
        if as_plist:
            print(plist(live, dev, interval, directory), end="")
            return 0
        if live:
            _state.require_own(directory, "a live sweep")
        lock_path = directory / "sweep.lock"
        handle = None
        if _state.foreign_owner(directory) is None:
            directory.mkdir(parents=True, exist_ok=True)
            handle = open(lock_path, "a")
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print("slotfather sweep: another sweep is running", file=sys.stderr)
                return 0
        try:
            if live and not logged:
                raise SweepError("a live sweep is always logged")
            report = sweep(directory, live, dev, only, logged)
        finally:
            if handle is not None:
                handle.close()
    except (SweepError, _config.SlotError) as error:
        print(f"slotfather sweep: {error}\n{USAGE}", file=sys.stderr)
        return 2
    if as_json:
        print(json.dumps(report, sort_keys=True))
    else:
        output = text(report, quiet)
        if output:
            print(output)
    if report["live_refused"]:
        print(f"slotfather sweep: {report['settings_error']}; the live sweep stopped nothing (fix it with "
              f"`slotfather sweep config ...` or `config reset`)", file=sys.stderr)
        return 3
    return 1 if report["failed"] else 0
