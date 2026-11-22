"""Starting a command under junkfather's ownership, and adopting one that already runs.

`own` starts the command in a process group (and session) of its own and records the entry before it returns
control. Attached (the default) it stays the command's parent: output and exit status pass through, SIGTERM, SIGINT
and SIGHUP stop the command's group, and its TTL is enforced on the spot. This is the same in a foreground Bash call
and under `run_in_background`, where stopping the background task stops the command. `--detach` instead hands the
command to a supervisor process of its own (its command line names only the entry, so no process sweep takes it
for the command), sends its output to logs/<id>.log and returns at once: a server started in a foreground call then
neither holds the call open nor dies with the call's shell.

While it runs, the supervisor refreshes the entry's `known` members every KNOWN_EVERY seconds (what a reap may
signal after the leader exits) and reaps the entry when its TTL passes. When the leader exits and nothing else of
the group remains, the entry is `exited`; members that outlive the leader keep the entry running until its owner
ends or its TTL passes.
"""
from __future__ import annotations

import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, TextIO, Tuple, cast

from . import entry as _entry
from . import events as _events
from . import owner as _owner
from . import procs as _procs
from . import reap as _reap
from . import safety as _safety
from . import supervise as _supervise
from .errors import NOT_FOUND, REFUSED, Refused
from .defaults import DETACH_WAIT, KNOWN_EVERY, POLL, SIGNAL_GRACE
from .entry import Entry, new as new_entry
from .release import PACKAGE_ROOT
from .state import Store
from .text import duration

retry_delay = _supervise.retry_delay


def self_command(*args: str) -> List[str]:
    """This junkfather's own command line for a helper process (the supervisor, a detached reaper)."""
    code = f"import sys; sys.path.insert(0, {PACKAGE_ROOT!r}); from junkfather.cli import main; sys.exit(main())"
    return [sys.executable, "-c", code, *args]


def end_child(child: subprocess.Popen[bytes], grace: float = SIGNAL_GRACE) -> None:
    """Stop our own child's group through its handle: an unwaited child keeps its pid and group id from reuse."""
    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 2.0)):
        if child.poll() is not None:
            break
        try:
            os.killpg(child.pid, sig)
        except OSError:
            pass
        try:
            child.wait(wait)
        except subprocess.TimeoutExpired:
            pass


def record_child(store: Store, entry_id: str, child: subprocess.Popen[bytes], supervisor: int,
                 timer: Optional[_supervise.Timer] = None,
                 reader: Callable[[], _procs.Table] = _procs.Table.read) -> Optional[Entry]:
    """Fill in the leader's identity right after it started (Popen returns once the exec succeeded). Without a
    readable process table, or when the entry stopped starting meanwhile (a reap of its owner), the child is
    stopped and the entry fails: an entry whose identity is unknown could never be reaped safely."""
    timer = timer or _supervise.Timer()
    table = proc = None
    for attempt in range(3):
        try:
            table = reader()
            proc = table.get(child.pid)
            break
        except _procs.ProcError:
            if attempt < 2:
                timer.pause(0.2 * (attempt + 1))
    me = table.get(supervisor) if table is not None else None
    if proc is None:
        entry = _entry.failed(store, entry_id, "its identity could not be read")
    else:
        entry = _entry.started(store, entry_id, proc, {"pid": supervisor, "start": me.start if me else None})
    if entry is None or entry.get("state") != _entry.RUNNING or entry.get("pid") != child.pid:
        end_child(child)
        return None
    _events.write(store.root, "entry.owned", _events.subject(entry), _events.actor_of(entry.get("owner")),
                  {"argv": entry.get("argv"), "cwd": entry.get("cwd"), "ttl": entry.get("ttl"),
                   "keep": entry.get("keep"), "mode": entry.get("mode"), "pgid": entry.get("pgid"),
                   "log": entry.get("log")})
    return entry


class ChildDriver:
    """Supervision driver for a child process started by `junkfather own` (attached or detached)."""

    max_wait = POLL
    wait_first = False

    def __init__(self, entry_id: str, child: subprocess.Popen[bytes], *, quiet: bool = False,
                 attached: bool = True, catcher: Optional["Catcher"] = None,
                 timer: Optional[_supervise.Timer] = None,
                 reaper: Optional[_supervise.Reaper] = None,
                 reader: Callable[[], _procs.Table] = _procs.Table.read):
        self.id = entry_id
        self.child = child
        self.quiet = quiet
        self.attached = attached
        self.catcher = catcher or Catcher()
        self.timer = timer or _supervise.Timer()
        self.reaper = reaper or _reap.reap
        self.reader = reader
        self.next_known = self.timer.clock() + KNOWN_EVERY
        self.code: Optional[int] = None
        self._exit_handled = False

    def notice(self, message: str) -> None:
        if not self.quiet:
            print(f"junkfather: {message}", file=sys.stderr, flush=True)

    def _poll_child(self) -> Optional[int]:
        if self.code is None:
            self.code = self.child.poll()
        return self.code

    def holds_leader(self) -> bool:
        return self._poll_child() is None

    def check_interrupt(self, store: Store, entry_id: str) -> Optional[int]:
        signalled = self.catcher.signum
        if signalled is None:
            return None
        name = signal.Signals(signalled).name
        result = self.reaper(store, entry_id, f"junkfather got {name}", grace=SIGNAL_GRACE,
                             actor=_events.tool("supervisor"))
        if self.child.poll() is None and result["result"] not in _reap.STOPPED + ("skipped",):
            end_child(self.child, SIGNAL_GRACE)
        self.notice(f"{entry_id} stopped on {name}: {result['result']}")
        self.child.poll()
        return 128 + signalled

    def wait(self, timeout: float) -> None:
        self.timer.pause(timeout)

    def refresh_members(self, store: Store, code: Optional[int], now: Optional[float] = None
                        ) -> Tuple[Optional[Entry], bool, int]:
        """Refresh `known` and `daemons`. Raises `ProcError` when the process table cannot be read."""
        table = self.reader()

        def members_of(e: Entry) -> List[_procs.Proc]:
            # While our child runs unwaited its pid cannot be reused: it is the leader whatever command it shows now
            # (a launcher such as /usr/bin/python3 execs another program), and known_seen records that command.
            members, _ = _reap.members(e, table, ours=code is None and self.child.poll() is None)
            daemons = _reap.daemons_started(e, table, members)
            if daemons:  # agent-browser daemons its processes started join the entry, saved with `known`
                _entry.add_daemons(e, daemons)
                members, _ = _reap.members(e, table, ours=code is None and self.child.poll() is None)
            return [p for p in members if p.pid != os.getpid()]

        updated, done, left = _entry.refreshed(store, self.id, members_of, code, now=now)
        if done and updated is not None:
            _events.write(store.root, "entry.exited", _events.subject(updated), _events.tool("supervisor"),
                          {"exit": code})
        return updated, done, left

    def refresh(self, store: Store, entry: Entry, seen_at: float) -> Optional[int]:
        code = self._poll_child()
        exited_now = code is not None and not self._exit_handled
        if not exited_now and self.timer.clock() < self.next_known:
            return None
        updated, done, left = self.refresh_members(store, code, now=seen_at)
        self.next_known = self.timer.clock() + KNOWN_EVERY
        if done:
            self._exit_handled = True
            return status(code)
        if exited_now and updated is not None and left > 0:
            self._exit_handled = True
            if self.attached:
                self.notice(f"{self.id}: the command exited; {left} process(es) it left keep the entry running "
                            f"until its owner ends or its TTL passes (now: junkfather reap --id {self.id})")
                return status(code)
        return None

    def finish_external(self, store: Store, entry: Optional[Entry]) -> int:
        if self._poll_child() is None:
            end_child(self.child)  # ended on the record (reaped, failed) while ours still runs
            self.code = self.child.poll()
        return status(self.code)

    def finish_ttl(self, store: Store, entry_id: str, result: Dict[str, Any]) -> int:
        self.notice(f"{entry_id} reached its TTL: {result['result']}")
        if self._poll_child() is None:
            end_child(self.child)
            self.code = self.child.poll()
        return status(self.code)


class Supervisor:
    """Waits on the leader, enforces the TTL, keeps `known` fresh, and stops the group on a signal. Delegates the
    shared supervision loop to `supervise.run` through `ChildDriver`. Tests inject `clock`, `wall`, `pause`,
    `reaper` and `reader`."""

    def __init__(self, store: Store, entry_id: str, child: subprocess.Popen[bytes], quiet: bool = False,
                 attached: bool = True, catcher: Optional["Catcher"] = None,
                 clock: Callable[[], float] = time.monotonic, pause: Callable[[float], None] = time.sleep,
                 reaper: Optional[Callable[..., Dict[str, Any]]] = None, wall: Callable[[], float] = time.time,
                 reader: Callable[[], _procs.Table] = _procs.Table.read):
        self.store, self.id, self.child, self.quiet, self.attached = store, entry_id, child, quiet, attached
        self.timer = _supervise.Timer(clock=clock, wall=wall, pause=pause)
        self.reaper = reaper or _reap.reap
        self.reader = reader
        self.driver = ChildDriver(
            entry_id,
            child,
            quiet=quiet,
            attached=attached,
            catcher=catcher,
            timer=self.timer,
            reaper=self.reaper,
            reader=reader,
        )

    def say(self, text: str) -> None:
        self.driver.notice(text)

    def run(self) -> int:
        reader = None if self.reader is _procs.Table.read else self.reader
        return _supervise.run(
            self.store, self.id, self.driver, timer=self.timer, reaper=self.reaper, reader=reader,
        )

    def refresh(self, code: Optional[int]) -> Tuple[bool, int]:
        """Refresh `known`. Returns (the entry exited: the leader is gone and nothing of it remains, how many
        processes remain)."""
        try:
            _, done, left = self.driver.refresh_members(self.store, code)
        except _procs.ProcError:
            return False, 0
        return done, left


class Catcher:
    """SIGTERM, SIGINT and SIGHUP noted from before the command starts, so none is lost in between."""

    def __init__(self) -> None:
        self.signum: Optional[int] = None
        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(signum, self.handle)

    def handle(self, signum: int, _frame: Any) -> None:
        self.signum = signum


def status(code: Optional[int]) -> int:
    """A shell's exit status for a child's return code (a signal -N is 128 + N)."""
    if code is None:
        return 1
    return code if code >= 0 else 128 - code


def own(store: Store, argv: List[str], ttl: float, keep: str, label: Optional[str], env: Mapping[str, str],
        detach: bool = False, log: Optional[str] = None, quiet: bool = False, agent: Optional[str] = None,
        lane: Optional[str] = None) -> int:
    cwd = os.getcwd()
    who = _owner.resolve(env, _procs.read_or_none(), cwd, store, ["own", *argv], lane, agent)
    entry = new_entry(store, argv, ttl, keep, label, who, "detached" if detach else "attached", cwd)
    if detach:
        entry["log"] = log or str(store.logs_dir / f"{entry['id']}.log")
    _entry.save_starting(store, entry)
    if detach:
        return start_detached(store, entry, quiet)
    catcher = Catcher()
    try:
        child = subprocess.Popen(argv, start_new_session=True)
    except OSError as error:
        _entry.failed(store, entry["id"], str(error), start_error=True)
        print(f"junkfather: cannot start {shlex.join(argv)}: {error}", file=sys.stderr)
        return 127
    if record_child(store, entry["id"], child, os.getpid()) is None:
        print(f"junkfather: {entry['id']} could not be recorded (no readable process table, or its owner ended "
              "meanwhile); the command was stopped", file=sys.stderr)
        return 1
    if not quiet:
        announce(entry, child.pid)
    return Supervisor(store, entry["id"], child, quiet, catcher=catcher).run()


def announce(entry: Entry, pid: int, out: Optional[TextIO] = None) -> None:
    keep = "" if entry["keep"] == "owner" else f", kept for the {entry['keep']}"
    note = f" ({entry.get('claim_note')})" if entry.get("claim_note") else ""
    log = f", log {entry['log']}" if entry.get("log") else ""
    print(f"junkfather: {entry['id']} owns pid {pid} (pgid {pid}) for {_owner.describe(entry['owner'])}{note}, "
          f"ttl {duration(entry['ttl'])}{keep}{log}; stop it: junkfather reap --id {entry['id']}",
          file=out or sys.stderr, flush=True)


def start_detached(store: Store, entry: Entry, quiet: bool,
                   timer: Optional[_supervise.Timer] = None,
                   starter: Optional[Callable[[Entry, Path], None]] = None) -> int:
    if entry["log"] is None:
        raise ValueError(f"{entry['id']}: a detached entry needs a log")  # own --detach always gives it one
    log = Path(entry["log"])
    log.parent.mkdir(parents=True, exist_ok=True)
    if starter is not None:
        starter(entry, log)
    else:
        with open(os.devnull, "rb") as null, open(log, "ab") as out:
            subprocess.Popen(self_command("supervise", entry["id"]), stdin=null, stdout=out, stderr=out,
                             start_new_session=True, close_fds=True, cwd=entry["cwd"])
    timer = timer or _supervise.Timer()

    def check() -> Optional[int]:
        current = store.load(entry["id"])
        if current is not None and current.get("state") == _entry.FAILED:
            print(f"junkfather: cannot start {shlex.join(entry['argv'])}: {current.get('error')}", file=sys.stderr)
            return 127
        pid = current.get("pid") if current is not None else None
        if current is not None and pid:
            if not quiet:
                announce(current, pid, out=sys.stdout)
            return 0
        return None

    code = timer.poll_until(check, DETACH_WAIT, 0.05)
    if code is not None:
        return code
    print(f"junkfather: the supervisor of {entry['id']} did not start the command in {DETACH_WAIT:.0f} s; "
          f"see {log}", file=sys.stderr)
    return 1


def supervise(store: Store, entry_id: str) -> int:
    """The detached supervisor: start the command with output to the log, then run like an attached own."""
    entry = store.load(entry_id)
    if entry is None or entry.get("state") != _entry.STARTING:
        print(f"junkfather supervise: no starting entry {entry_id}", file=sys.stderr)
        return 2
    catcher = Catcher()
    try:
        with open(os.devnull, "rb") as null:
            child = subprocess.Popen(entry["argv"], stdin=null, start_new_session=True, cwd=entry["cwd"])
    except OSError as error:
        _entry.failed(store, entry_id, str(error), start_error=True)
        print(f"junkfather: cannot start {shlex.join(entry['argv'])}: {error}", file=sys.stderr)
        return 127
    if record_child(store, entry_id, child, os.getpid()) is None:
        print(f"junkfather: {entry_id} could not be recorded; the command was stopped", file=sys.stderr)
        return 1
    print(f"junkfather: {entry_id} started pid {child.pid}: {shlex.join(entry['argv'])}", file=sys.stderr, flush=True)
    code = Supervisor(store, entry_id, child, attached=False, catcher=catcher).run()
    print(f"junkfather: {entry_id} ended with {code}", file=sys.stderr, flush=True)
    return code


def adopt(store: Store, pid: int, ttl: float, keep: str, label: Optional[str], env: Mapping[str, str],
          lane: Optional[str] = None, agent: Optional[str] = None) -> int:
    """`junkfather adopt`: record a running process (and, when it leads its own group, that group) as owned."""
    try:
        table = _procs.Table.read()
    except _procs.ProcError as error:
        raise Refused(str(error)) from None
    if table.get(pid) is None:
        raise Refused(f"refusing pid {pid}: no such process", NOT_FOUND)
    who = _owner.resolve(env, table, os.getcwd(), store, lane=lane, agent=agent, claims=False)
    entry = adopt_process(store, pid, table, ttl, keep, label, who)
    scope = "its group" if entry["group"] else "it and its descendants (not its group: it does not lead one)"
    print(f"junkfather: {entry['id']} adopted pid {pid} ({scope}) for {_owner.describe(who)}, ttl {duration(ttl)}; "
          f"stop it: junkfather reap --id {entry['id']}")
    return 0


def adopt_process(store: Store, pid: int, table: _procs.Table, ttl: float, keep: str, label: Optional[str],
                  who: Dict[str, Any], argv: Optional[List[str]] = None, own_parent: bool = False,
                  extra: Optional[Dict[str, Any]] = None, now: Optional[float] = None) -> Entry:
    """The one adoption, for `adopt` and a background call's shell (background.py): the refusals of
    safety.adopt_refusal (`own_parent`: `pid` may be this process's parent, the one ancestor), the entry for `who`
    with the process and its descendants as known members (`argv` shown for it, default its command; `extra` fields
    on the entry and its event), saved as running, and its `entry.adopted` event. Refused when it may not."""
    proc = table.get(pid)
    refusal = _safety.adopt_refusal(pid, table, lambda candidate: any(
        e.get("state") == _entry.RUNNING and e.get("pid") == candidate for e in store.entries()), own_parent=own_parent)
    if refusal or proc is None:
        raise Refused(f"refusing pid {pid}: {refusal or 'no such process'}", NOT_FOUND if proc is None else REFUSED)
    made = new_entry(store, argv or [proc.cmd], ttl, keep, label, who, "adopted", os.getcwd(), now)
    made.update(cast(Entry, dict(extra or {})))
    mine = {os.getpid(), *(p.pid for p in table.descendants(os.getpid()))}  # this junkfather and its ps
    theirs = _reap.claimed_elsewhere(store, made, table)  # what another entry holds directly stays its own
    entry = _entry.adopted(store, made, proc,
                           [proc, *(p for p in table.descendants(pid, prune=_safety.unsafe) if p.pid not in mine
                                    and (theirs.get(p.pid) or (0, False))[0] < _reap.DIRECT)])
    _events.write(store.root, "entry.adopted", _events.subject(entry), _events.actor_of(who),
                  {"cmd": proc.cmd, "ttl": ttl, "keep": keep, "pgid": proc.pgid, "group": entry["group"],
                   **(extra or {})})
    return entry

