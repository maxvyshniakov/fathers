"""Measurement sessions (fathers#96): one lease that drains the queue once and holds it for a series of runs.

  slotfather session start --label NAME [-n N|all] [--idle S] [--expect S] [--priority P] [--ticket ID]
                           [--meta K=V]... [--wait S]
  slotfather run --session NAME [--] COMMAND...
  slotfather session stop NAME

`start` queues one request (-n all by default) held by a detached holder process and returns once it is granted.
The holder keeps the slots between runs and releases them when no run has been active for --idle seconds (default
120), when `session stop` asks (after the runs that are active end; no new run starts meanwhile), or when it dies
(the next look reaps it, as any crashed holder). `run --session NAME` runs its command under that lease without a
lease of its own (SLOTFATHER_LEASE names the session's, so nested runs take none either), waiting while the session
still waits for its slots. To other requests and older clients the session is one held lease; its expected run is
--expect where given, else that of earlier sessions of its label. The session's history line carries the summed
use of its runs and `session`: {name, idle, runs, busy, ended_by}. Runs are kept in the entry's .session file
beside it, written under the queue lock.
"""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from types import FrameType
from typing import Any, cast

from . import config as _config
from . import events as _events
from . import facts as _facts
from . import leases as _leases
from . import platform as _platform
from . import state as _state

IDLE_DEFAULT = 120.0  # seconds without an active run before a session releases its slots
READY = "slotfather-session-ready"  # the line the holder writes once its lease is held
PRUNE_EVERY = 30.0  # seconds between full process looks for runs whose pid was reused after they died
STOP_WAIT = 600.0  # seconds `session stop` waits for the session to release


def mapping(value: object) -> dict[str, Any]:
    """`value` when it is a JSON object, else an empty one."""
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def uses_path(entry_path: Path) -> Path:
    return entry_path.with_suffix(".session")


def read_uses(entry_path: Path) -> dict[str, Any] | None:
    try:
        found: object = json.loads(uses_path(entry_path).read_text())
    except (OSError, ValueError):
        return None
    return cast(dict[str, Any], found) if isinstance(found, dict) else None


def name_of(entry: dict[str, Any]) -> str | None:
    found = mapping(entry.get("session")).get("name")
    return found if isinstance(found, str) else None


def find(live: list[tuple[Path, dict[str, Any]]], name: str) -> tuple[Path, dict[str, Any]] | None:
    return next(((p, e) for p, e in live if name_of(e) == name), None)


def view(path: Path, entry: dict[str, Any]) -> dict[str, Any]:
    """`status --json`'s `session` of an entry: its name, idle timeout, expected run, whether it stops, its runs so
    far, the busy seconds and the active runs (pid, label, cmd, since), and when it was last used."""
    spec = mapping(entry.get("session"))
    uses = read_uses(path) or {}
    active = mapping(uses.get("active"))
    return {"name": spec.get("name"), "idle": spec.get("idle"), "expect": spec.get("expect"),
            "stopping": bool(spec.get("stopping")), "runs": int(uses.get("runs") or 0),
            "busy": round(float(uses.get("busy") or 0), 1), "last_use": uses.get("last_use"),
            "active": [{"pid": int(pid), **{k: mapping(run).get(k) for k in ("label", "cmd", "since")}}
                       for pid, run in sorted(active.items())]}


def tags(path: Path, entry: dict[str, Any]) -> str:
    found = view(path, entry)
    return (f" session={found['name']} runs={found['runs']} active={len(found['active'])}"
            + (" stopping" if found["stopping"] else ""))


def option_value(argv: list[str], at: int) -> str:
    if at + 1 >= len(argv):
        raise _config.SlotError(f"{argv[at]} takes a value")
    return argv[at + 1]


def parse_start(argv: list[str]) -> dict[str, Any]:
    spec: dict[str, Any] = {"count": "all", "idle": IDLE_DEFAULT, "meta": [], "priority": None, "ticket": None,
                            "wait": None, "expect": None, "name": None}
    at = 0
    while at < len(argv):
        word = argv[at]
        if word in ("-n", "--slots"):
            value = option_value(argv, at)
            spec["count"] = "all" if value == "all" else _config_count(value)
        elif word in ("-l", "--label"):
            spec["name"] = option_value(argv, at)
        elif word in ("--idle", "--expect", "--wait"):
            try:
                value = _config.parse_duration(option_value(argv, at))
            except ValueError:
                raise _config.SlotError(f"{word} takes seconds or a duration such as 30m") from None
            if value <= 0:
                raise _config.SlotError(f"{word} takes a positive duration")
            spec[word[2:]] = value
        elif word == "--meta":
            spec["meta"].append(option_value(argv, at))
        elif word == "--priority":
            value = option_value(argv, at)
            if value not in _config.PRIORITIES:
                raise _config.SlotError(f"--priority takes {', '.join(_config.PRIORITIES)}, not {value!r}")
            spec["priority"] = value
        elif word == "--ticket":
            spec["ticket"] = _config.checked_ticket(option_value(argv, at))
        else:
            raise _config.SlotError(f"unknown session start option {word}")
        at += 2
    if not spec["name"] or not _config.META_KEY.fullmatch(spec["name"]):
        raise _config.SlotError("session start takes --label NAME, a lowercase word (a-z, 0-9, _ . -)")
    spec["meta"] = _config.parse_meta(spec["meta"])
    return spec


def _config_count(value: str) -> int:
    if not value.isdigit() or int(value) < 1:
        raise _config.SlotError(f"-n takes a positive count or all, not {value!r}")
    return int(value)


def start(argv: list[str]) -> int:
    """`session start`: checks, starts the holder detached and relays what it says until its lease is held."""
    spec = parse_start(argv)
    directory = _config.state_dir()
    _state.require_own(directory, "a session")
    if _leases.inherited_lease(_platform.processes()) is not None:
        raise _config.SlotError("a session cannot start inside a lease: it would wait for the slots its caller holds")
    with _state.locked(directory):  # the process table of this moment: one taken earlier would reap new waiters
        found = find(_leases.reap(directory, _leases.process_table(directory)), spec["name"])
    if found is not None:
        raise _config.SlotError(f"session {spec['name']} exists (pid {found[1].get('pid')}, {found[1].get('state')})")
    env = {k: v for k, v in os.environ.items() if k not in ("SLOTFATHER_LEASE", "SLOTFATHER_COUNT", "SLOTFATHER_YIELD")}
    env.update(SLOTFATHER_DIR=str(directory), PYTHONPATH=str(Path(__file__).resolve().parent.parent))
    holder = subprocess.Popen([sys.executable, "-m", "slotfather", "session", "hold", json.dumps(spec)], env=env,
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                              start_new_session=True, cwd=os.getcwd())
    assert holder.stdout is not None

    def give_up(signum: int, _frame: FrameType | None) -> None:
        with contextlib.suppress(ProcessLookupError):
            os.kill(holder.pid, signal.SIGTERM)
        raise SystemExit(128 + signum)
    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, give_up)
    for line in holder.stdout:
        if line.strip() == READY:
            holder.stdout.close()
            idle = _config.duration(spec["idle"])
            with _state.locked(directory):
                held = find(_state.entries(directory), spec["name"])
            count = held[1].get("count") if held else "?"
            print(f"slot: session {spec['name']} holds {count} slot(s) (holder pid {holder.pid}); run work with "
                  f"`slotfather run --session {spec['name']} -- COMMAND`; it ends after {idle} without a run or at "
                  f"`slotfather session stop {spec['name']}`", flush=True)
            return 0
        print(line, end="", file=sys.stderr, flush=True)
    code = holder.wait()
    return 128 - code if code < 0 else code


class Holder:
    """The detached process that holds a session's lease and releases it when the session ends."""

    def __init__(self, spec: dict[str, Any]):
        self.spec, self.stop_asked = spec, False
        self.lease: _leases.Lease | None = None

    def ask_stop(self, _signum: int, _frame: FrameType | None) -> None:
        self.stop_asked = True

    def run(self) -> int:
        spec = self.spec
        session = {"name": spec["name"], "idle": spec["idle"]}
        if spec.get("expect"):
            session["expect"] = spec["expect"]
        # Installed before the grant: acquire() puts it back once the lease is held, so a stop sent at that moment
        # ends the session cleanly instead of killing the holder. While it waits, acquire's own handler leaves.
        signal.signal(signal.SIGTERM, self.ask_stop)
        note = (f"session {spec['name']} holds its slots between runs until it is stopped or idle for "
                f"{_config.duration(spec['idle'])}")
        try:
            lease = _leases.acquire(spec["count"], spec["name"], f"slotfather session {spec['name']}",
                                    wait=spec.get("wait"), note=note, meta=spec.get("meta") or None,
                                    priority=spec.get("priority"), ticket=spec.get("ticket"), session=session)
        except _config.SlotTimeout as error:
            print(f"slot: {spec['name']}: {error}", file=sys.stderr, flush=True)
            return _config.TIMED_OUT
        except _config.SlotError as error:
            print(f"slotfather: {error}", file=sys.stderr, flush=True)
            return 2
        except SystemExit as stop:
            return int(stop.code or 1)
        assert lease.path is not None
        self.lease = lease
        directory = lease.path.parent.parent
        began = time.time()
        with _state.locked(directory):
            _state.write(uses_path(lease.path), {"active": {}, "runs": 0, "busy": 0.0, "cpu": 0.0,
                                                 "rss_peak_mb": 0.0, "last_use": round(began, 3)})
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        with contextlib.suppress(OSError):  # whoever started it may be gone: the session holds all the same
            print(READY, flush=True)
        with open(os.devnull, "w") as quiet:  # whoever started it has stopped reading
            os.dup2(quiet.fileno(), 1)
            os.dup2(quiet.fileno(), 2)
        return self.hold(lease.path, directory, began)

    def hold(self, path: Path, directory: Path, began: float) -> int:
        idle = float(self.spec["idle"])
        tick = max(0.05, min(1.0, idle / 10))
        pruned = time.monotonic()
        while True:
            time.sleep(tick)
            full = time.monotonic() - pruned >= PRUNE_EVERY
            table = None
            if full:
                with contextlib.suppress(_config.SlotError):  # no table to trust: no run is pruned this time
                    table = _platform.processes()
            if full:
                pruned = time.monotonic()
            with _state.locked(directory):
                try:
                    entry = json.loads(path.read_text())
                except (OSError, ValueError):
                    return 1  # the lease vanished (removed by hand): nothing is left to release
                uses = read_uses(path) or {"active": {}, "runs": 0, "busy": 0.0, "cpu": 0.0, "last_use": began}
                moment = time.time()
                if prune(uses, table, moment):
                    _state.write(uses_path(path), uses)
                if self.stop_asked and not entry["session"].get("stopping"):
                    entry["session"]["stopping"] = True
                    _state.write(path, entry)
                if uses.get("active"):
                    continue
                reason = "stopped" if self.stop_asked else \
                    "idle" if moment - float(uses.get("last_use") or began) >= idle else None
                if reason is None:
                    continue
                entry["session"].update(stopping=True, runs=int(uses.get("runs") or 0),
                                        busy=round(float(uses.get("busy") or 0), 1), ended_by=reason)
                _state.write(path, entry)
                uses_path(path).unlink(missing_ok=True)
                _events.emit(directory, "session.ended", _events.request(entry, path),
                             {"reason": reason, "runs": entry["session"]["runs"], "busy": entry["session"]["busy"],
                              "held": round(moment - began)}, _events.holder(entry))
            break
        assert self.lease is not None
        self.lease.status = 0
        self.lease.usage = {"cpu": round(float(uses.get("cpu") or 0), 2),
                            "rss_peak_mb": round(float(uses.get("rss_peak_mb") or 0), 1),
                            "wall": round(time.time() - began, 2)}
        self.lease.release()
        return 0


def prune(uses: dict[str, Any], table: dict[int, tuple[int, str]] | None, moment: float) -> bool:
    """Drops active runs whose process is gone (a `run --session` killed before it could say it ended); with
    `table`, also those whose pid now belongs to another process. Returns whether any went."""
    active = mapping(uses.get("active"))
    gone: list[str] = []
    for pid, value in active.items():
        run = mapping(value)
        try:
            os.kill(int(pid), 0)
            alive = True
        except ProcessLookupError:
            alive = False
        except (PermissionError, ValueError):
            alive = True
        if alive and table is not None:
            alive = int(pid) in table and (not run.get("start") or table[int(pid)][1] == run.get("start"))
        if not alive:
            gone.append(pid)
    for pid in gone:
        active.pop(pid, None)
    if gone:
        uses["last_use"] = round(moment, 3)
    return bool(gone)


def hold(argv: list[str]) -> int:
    """`session hold SPEC`: the holder, which `session start` runs; not for direct use."""
    if len(argv) != 1:
        raise _config.SlotError("session hold is started by session start")
    spec: object = json.loads(argv[0])
    if not isinstance(spec, dict):
        raise _config.SlotError("session hold is started by session start")
    return Holder(cast(dict[str, Any], spec)).run()


def stop(argv: list[str]) -> int:
    """`session stop NAME`: asks the holder to end the session once its active runs end, and waits for that."""
    if len(argv) != 1:
        raise _config.SlotError("session stop takes the session's name")
    name = argv[0]
    directory = _config.state_dir()
    _state.require_own(directory, "a session stop")
    with _state.locked(directory):
        table = _leases.process_table(directory)
        found = find(_leases.reap(directory, table), name)
        if found is None:
            raise _config.SlotError(f"no session {name} in this queue")
        path, entry = found
        if _platform.process_uid(entry.get("pid")) != os.geteuid():
            raise _config.SlotError(f"session {name} (pid {entry.get('pid')}) is another user's")
        os.kill(int(entry["pid"]), signal.SIGTERM)
    said = False
    deadline = time.monotonic() + STOP_WAIT
    reaped = time.monotonic()
    while path.exists() and time.monotonic() < deadline:
        active = len(view(path, entry)["active"])
        if active and not said:
            print(f"slot: session {name} stops once its {active} active run(s) end", file=sys.stderr, flush=True)
            said = True
        time.sleep(0.1)  # the holder releases the lease itself; only a holder that died needs a look
        if time.monotonic() - reaped >= 5:
            reaped = time.monotonic()
            with _state.locked(directory), contextlib.suppress(_config.SlotError):
                _leases.reap(directory, _leases.process_table(directory))
    if path.exists():
        print(f"slot: session {name} still holds its slots after {STOP_WAIT:g} s", file=sys.stderr)
        return 1
    print(f"slot: session {name} ended")
    return 0


def command(argv: list[str]) -> int:
    if argv[:1] == ["start"]:
        return start(argv[1:])
    if argv[:1] == ["stop"]:
        return stop(argv[1:])
    if argv[:1] == ["hold"]:
        return hold(argv[1:])
    raise _config.SlotError("session takes start --label NAME [...] or stop NAME")


def run_inside(name: str, argv: list[str], label: str | None = None, meta: dict[str, str] | None = None) -> int:
    """`run --session NAME -- COMMAND`: COMMAND under the session's lease, recorded as one of its runs."""
    directory = _config.state_dir()
    _state.require_own(directory, "a session run")
    me = os.getpid()
    said = False
    poll = float(_config.setting("POLL") or 2)
    while True:
        with _state.locked(directory):
            try:
                table = _leases.process_table(directory)
            except _config.ProcessTableError:
                table = None  # no table to trust: this look reaps nothing, and the run looks again
            if table is not None:
                found = find(_leases.reap(directory, table), name)
                if found is None:
                    raise _config.SlotError(f"no session {name} in this queue (slotfather session start --label {name})")
                path, entry = found
                if _platform.process_uid(entry.get("pid")) != os.geteuid():
                    raise _config.SlotError(f"session {name} (pid {entry.get('pid')}) is another user's")
                if entry["session"].get("stopping"):
                    raise _config.SlotError(f"session {name} is stopping: it starts no more runs")
                uses = read_uses(path) if entry.get("state") == "held" else None
                if uses is not None:
                    since = _config.now()
                    uses.setdefault("active", {})[str(me)] = {"start": table.get(me, (0, ""))[1], "label": label,
                                                              "cmd": _events.command(" ".join(argv)), "since": since}
                    _state.write(uses_path(path), uses)
                    _events.emit(directory, "session.run_started", _events.request(entry, path),
                                 {"session": name, "run": {"pid": me, "label": label,
                                                           "cmd": _events.command(" ".join(argv))}})
                    break
        if not said:
            print(f"slot: session {name} still waits for its slots; this run starts once it holds them",
                  file=sys.stderr, flush=True)
            said = True
        time.sleep(poll)
    try:
        git = _facts.git(os.getcwd())
    except OSError:
        git = None
    environment: dict[str, str] = {}
    for prefix in _config.LEASE_ENV_PREFIXES:
        environment.update({prefix + "LEASE": str(path), prefix + "COUNT": str(entry["count"])})
    began = time.monotonic()
    status, usage = 1, None
    try:
        status, usage = _leases.execute(argv, environment, _leases.policy() == "background", directory=directory)
    finally:
        ran = time.monotonic() - began
        with _state.locked(directory):
            uses = read_uses(path)
            if uses is not None:
                mapping(uses.get("active")).pop(str(me), None)
                uses.update(runs=int(uses.get("runs") or 0) + 1, busy=round(float(uses.get("busy") or 0) + ran, 2),
                            last_use=round(time.time(), 3))
                if usage:
                    uses["cpu"] = round(float(uses.get("cpu") or 0) + float(usage.get("cpu") or 0), 2)
                    uses["rss_peak_mb"] = max(float(uses.get("rss_peak_mb") or 0), float(usage.get("rss_peak_mb") or 0))
                _state.write(uses_path(path), uses)
            run: dict[str, Any] = {"pid": me, "label": label, "exit": status, "ran": round(ran, 2),
                                   "cpu": (usage or {}).get("cpu")}
            if meta:
                run["meta"] = meta  # --meta cache=..., per run
            if git:
                run["git"] = git  # the tree this run checked
            _events.emit(directory, "session.run_ended", _events.request(entry, path), {"session": name, "run": run})
    return status
