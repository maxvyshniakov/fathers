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
from typing import Any, Callable, Dict, List, Mapping, Optional

from . import events as _events
from . import owner as _owner
from . import procs as _procs
from . import reap as _reap
from .state import Entry, Store, VERSION

KNOWN_EVERY = 15.0  # seconds between refreshes of an entry's known members
POLL = 0.5  # seconds between checks of the child and the entry
DETACH_WAIT = 15.0  # seconds `own --detach` waits for the supervisor to start the command
PACKAGE_ROOT = str(Path(__file__).resolve().parent.parent)
RETRY_FIRST = 1.0  # seconds before the supervisor tries a failed TTL reap again; doubled per failure
RETRY_MAX = 60.0  # the longest wait between two tries


def retry_delay(failures: int) -> float:
    """How long the supervisor waits after the `failures`-th failed TTL reap in a row."""
    return min(RETRY_MAX, RETRY_FIRST * 2 ** min(max(failures - 1, 0), 16))


def self_command(*args: str) -> List[str]:
    """This junkfather's own command line for a helper process (the supervisor, a detached reaper)."""
    code = f"import sys; sys.path.insert(0, {PACKAGE_ROOT!r}); from junkfather.cli import main; sys.exit(main())"
    return [sys.executable, "-c", code, *args]


def new_entry(store: Store, argv: List[str], ttl: float, keep: str, label: Optional[str], owner: Dict[str, Any],
              mode: str, cwd: str) -> Entry:
    now = time.time()
    return {"v": VERSION, "id": store.new_id(), "label": label, "argv": argv, "cwd": cwd, "created": now,
            "ttl": ttl, "expires": now + ttl, "keep": keep, "owner": owner, "mode": mode, "state": "starting",
            "pid": None, "pgid": None, "start": None, "seen": None, "group": True, "known": [], "known_seen": [],
            "handed": [],
            "supervisor": None, "log": None, "claim_note": owner.pop("claim_note", "")}


def end_child(child: subprocess.Popen, grace: float = 3.0) -> None:
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


def record_child(store: Store, entry_id: str, child: subprocess.Popen, supervisor: int) -> Optional[Entry]:
    """Fill in the leader's identity right after it started (Popen returns once the exec succeeded). Without a
    readable process table, or when the entry stopped starting meanwhile (a reap of its owner), the child is
    stopped and the entry fails: an entry whose identity is unknown could never be reaped safely."""
    table = proc = None
    for attempt in range(3):
        try:
            table = _procs.Table.read()
            proc = table.get(child.pid)
            break
        except _procs.ProcError:
            time.sleep(0.2 * (attempt + 1))
    me = table.get(supervisor) if table is not None else None

    def fill(e: Entry) -> Optional[bool]:
        if e.get("state") != "starting":
            return False
        if proc is None:
            e.update(state="failed", ended=time.time(), error="its identity could not be read")
            return None
        e.update(state="running", pid=child.pid, pgid=child.pid, start=proc.start, seen=proc.cmd,
                 supervisor={"pid": supervisor, "start": me.start if me else None}, **_reap.known_fields([proc]))
        return None
    entry = store.update(entry_id, fill)
    if entry is None or entry.get("state") != "running" or entry.get("pid") != child.pid:
        end_child(child)
        return None
    if entry is not None:
        _events.write(store.root, "entry.owned", _events.subject(entry), _events.actor_of(entry.get("owner")),
                      {"argv": entry.get("argv"), "cwd": entry.get("cwd"), "ttl": entry.get("ttl"),
                       "keep": entry.get("keep"), "mode": entry.get("mode"), "pgid": entry.get("pgid"),
                       "log": entry.get("log")})
    return entry


class Supervisor:
    """Waits on the leader, enforces the TTL, keeps `known` fresh, and stops the group on a signal. A TTL reap that
    fails is tried again after a capped backoff (retry_delay), and only the first failure of a streak is an event.
    Tests inject `clock` (monotonic), `pause` and `reaper`."""

    def __init__(self, store: Store, entry_id: str, child: subprocess.Popen, quiet: bool = False,
                 attached: bool = True, catcher: Optional["Catcher"] = None,
                 clock: Callable[[], float] = time.monotonic, pause: Callable[[float], None] = time.sleep,
                 reaper: Optional[Callable[..., Dict[str, Any]]] = None):
        self.store, self.id, self.child, self.quiet, self.attached = store, entry_id, child, quiet, attached
        self.catcher = catcher or Catcher()
        self.clock, self.pause, self.reaper = clock, pause, reaper or _reap.reap

    def say(self, text: str) -> None:
        if not self.quiet:
            print(f"junkfather: {text}", file=sys.stderr, flush=True)

    def run(self) -> int:
        next_known = self.clock() + KNOWN_EVERY
        code: Optional[int] = None
        failures, retry_at = 0, 0.0
        while True:
            signalled = self.catcher.signum
            if signalled is not None:
                name = signal.Signals(signalled).name
                result = _reap.reap(self.store, self.id, f"junkfather got {name}", grace=SIGNAL_GRACE,
                                    actor=_events.tool("supervisor"))
                if self.child.poll() is None and result["result"] not in _reap.STOPPED + ("skipped",):
                    end_child(self.child, SIGNAL_GRACE)
                self.say(f"{self.id} stopped on {name}: {result['result']}")
                self.child.poll()
                return 128 + signalled
            exited_now = False
            if code is None:
                code = self.child.poll()
                exited_now = code is not None
            entry = self.store.load(self.id)
            if entry is not None and entry.get("state") == "gone" and code is None:
                # A sweep took it for ended while our child runs: it is still ours.
                self.store.update(self.id, lambda e: e.update(state="running", ended=None)
                                  if e.get("state") == "gone" else False)
                continue
            if entry is None or entry.get("state") not in ("running", "starting", "reaping"):
                if code is None:
                    end_child(self.child)  # ended on the record (reaped, failed) while ours still runs
                    code = self.child.poll()
                if entry is not None and entry.get("state") == "reaped":
                    self.say(f"{self.id} reaped ({(entry.get('end') or {}).get('reason')})")
                return status(code)
            if entry.get("state") == "running" and time.time() >= float(entry.get("expires") or 0) \
                    and self.clock() >= retry_at:
                result = self.reaper(self.store, self.id, "ttl expired", actor=_events.tool("supervisor"),
                                     report_failure=not failures)
                if result["result"] != "failed":
                    self.say(f"{self.id} reached its TTL: {result['result']}")
                    continue
                failures += 1
                retry_at = self.clock() + retry_delay(failures)
                if failures == 1:
                    self.say(f"{self.id} reached its TTL: failed ({result['reason']}); trying again with a backoff "
                             f"up to {RETRY_MAX:.0f} s")
            if exited_now or self.clock() >= next_known:
                next_known = self.clock() + KNOWN_EVERY
                done, left = self.refresh(code)
                if done:
                    return status(code)
                if exited_now and self.attached and entry.get("state") == "running":
                    self.say(f"{self.id}: the command exited; {left} process(es) it left keep the entry running "
                             f"until its owner ends or its TTL passes (now: junkfather reap --id {self.id})")
                    return status(code)
            self.pause(POLL)

    def refresh(self, code: Optional[int]) -> "tuple[bool, int]":
        """Refresh `known`. Returns (the entry exited: the leader is gone and nothing of it remains, how many
        processes remain)."""
        try:
            table = _procs.Table.read()
        except _procs.ProcError:
            return False, 0
        result = {"done": False, "left": 0}

        def change(e: Entry) -> Optional[bool]:
            if e.get("state") != "running":
                return False
            # While our child runs unwaited its pid cannot be reused: it is the leader whatever command it shows now
            # (a launcher such as /usr/bin/python3 execs another program), and known_seen records that command.
            members, _ = _reap.members(e, table, ours=code is None and self.child.poll() is None)
            members = [p for p in members if p.pid != os.getpid()]
            result["left"] = len(members)
            if members:
                e.update(_reap.known_fields(members))
                return None
            if code is None:
                return False
            e.update(state="exited", ended=time.time(), exit=code, known=[], known_seen=[])
            result["done"] = True
            return None

        entry = self.store.update(self.id, change)
        if result["done"] and entry is not None:
            _events.write(self.store.root, "entry.exited", _events.subject(entry), _events.tool("supervisor"),
                          {"exit": code})
        return bool(result["done"]), int(result["left"])


SIGNAL_GRACE = 3.0  # seconds a signalled junkfather gives its command before SIGKILL


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


def owner_of(store: Store, env: Mapping[str, str], words: List[str], agent: Optional[str] = None,
             lane: Optional[str] = None) -> Dict[str, Any]:
    """The owner of what this command starts: the detected session, its subagent from the claim holding `words`
    (junkfather's own arguments), and the --lane/--agent overrides."""
    try:
        table: Optional[_procs.Table] = _procs.Table.read()
    except _procs.ProcError:
        table = None
    who = _owner.detect(env, table, os.getcwd())
    if lane:
        who["lane"] = lane
    if agent:
        who["agent"] = agent
    elif who["kind"] == "claude" and env.get("JUNKFATHER_AGENT"):
        who["agent"] = env["JUNKFATHER_AGENT"]
    elif who["kind"] == "claude" and who.get("session"):
        claim, note = _owner.find_claim(store, who["session"], words)
        if claim is not None:
            who["agent"], who["agent_type"] = claim.get("agent"), claim.get("agent_type")
        who["claim_note"] = note
    return who


def own(store: Store, argv: List[str], ttl: float, keep: str, label: Optional[str], env: Mapping[str, str],
        detach: bool = False, log: Optional[str] = None, quiet: bool = False, agent: Optional[str] = None,
        lane: Optional[str] = None) -> int:
    cwd = os.getcwd()
    who = owner_of(store, env, ["own", *argv], agent, lane)
    entry = new_entry(store, argv, ttl, keep, label, who, "detached" if detach else "attached", cwd)
    if detach:
        entry["log"] = log or str(store.logs_dir / f"{entry['id']}.log")
    with store.lock():
        store.save(entry)
    if detach:
        return start_detached(store, entry, quiet)
    catcher = Catcher()
    try:
        child = subprocess.Popen(argv, start_new_session=True)
    except OSError as error:
        message = str(error)
        store.update(entry["id"], lambda e: e.update(state="failed", ended=time.time(), error=message))
        print(f"junkfather: cannot start {shlex.join(argv)}: {error}", file=sys.stderr)
        return 127
    if record_child(store, entry["id"], child, os.getpid()) is None:
        print(f"junkfather: {entry['id']} could not be recorded (no readable process table, or its owner ended "
              "meanwhile); the command was stopped", file=sys.stderr)
        return 1
    if not quiet:
        announce(entry, child.pid)
    return Supervisor(store, entry["id"], child, quiet, catcher=catcher).run()


def announce(entry: Entry, pid: int, out=None) -> None:
    keep = "" if entry["keep"] == "owner" else f", kept for the {entry['keep']}"
    note = f" ({entry['claim_note']})" if entry.get("claim_note") else ""
    log = f", log {entry['log']}" if entry.get("log") else ""
    print(f"junkfather: {entry['id']} owns pid {pid} (pgid {pid}) for {_owner.describe(entry['owner'])}{note}, "
          f"ttl {duration(entry['ttl'])}{keep}{log}; stop it: junkfather reap --id {entry['id']}",
          file=out or sys.stderr, flush=True)


def start_detached(store: Store, entry: Entry, quiet: bool) -> int:
    log = Path(entry["log"])
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(os.devnull, "rb") as null, open(log, "ab") as out:
        subprocess.Popen(self_command("supervise", entry["id"]), stdin=null, stdout=out, stderr=out,
                         start_new_session=True, close_fds=True, cwd=entry["cwd"])
    deadline = time.monotonic() + DETACH_WAIT
    while time.monotonic() < deadline:
        current = store.load(entry["id"]) or {}
        if current.get("state") == "failed":
            print(f"junkfather: cannot start {shlex.join(entry['argv'])}: {current.get('error')}", file=sys.stderr)
            return 127
        if current.get("pid"):
            if not quiet:
                announce(current, current["pid"], out=sys.stdout)
            return 0
        time.sleep(0.05)
    print(f"junkfather: the supervisor of {entry['id']} did not start the command in {DETACH_WAIT:.0f} s; "
          f"see {log}", file=sys.stderr)
    return 1


def supervise(store: Store, entry_id: str) -> int:
    """The detached supervisor: start the command with output to the log, then run like an attached own."""
    entry = store.load(entry_id)
    if entry is None or entry.get("state") != "starting":
        print(f"junkfather supervise: no starting entry {entry_id}", file=sys.stderr)
        return 2
    catcher = Catcher()
    try:
        with open(os.devnull, "rb") as null:
            child = subprocess.Popen(entry["argv"], stdin=null, start_new_session=True, cwd=entry["cwd"])
    except OSError as error:
        message = str(error)
        store.update(entry_id, lambda e: e.update(state="failed", ended=time.time(), error=message))
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
    """Record a running process (and, when it leads its own group, that group) as owned."""
    try:
        table = _procs.Table.read()
    except _procs.ProcError as error:
        print(f"junkfather adopt: {error}", file=sys.stderr)
        return 1
    proc = table.get(pid)
    refusal = None
    if proc is None:
        refusal = "no such process"
    elif proc.uid != os.getuid():
        refusal = "another user's process"
    elif pid in _reap.protected(table):
        refusal = "junkfather itself or one of its ancestors"
    elif _owner.CLAUDE.search(proc.cmd) or _owner.CODEX.search(proc.cmd):
        refusal = "an agent session"
    elif proc.tty:
        refusal = f"a process with a terminal ({proc.tty})"
    elif any(_reap.unsafe(p) for p in table.descendants(pid)):
        refusal = "its tree holds a terminal's process or an agent session"
    elif any(e.get("state") == "running" and e.get("pid") == pid for e in store.entries()):
        refusal = "already owned"
    if refusal:
        print(f"junkfather adopt: refusing pid {pid}: {refusal}", file=sys.stderr)
        return 1
    assert proc is not None
    who = _owner.detect(env, table, os.getcwd())
    if lane:
        who["lane"] = lane
    if agent:
        who["agent"] = agent
    entry = new_entry(store, [proc.cmd], ttl, keep, label, who, "adopted", os.getcwd())
    entry.update(state="running", pid=pid, pgid=proc.pgid, start=proc.start, seen=proc.cmd,
                 group=proc.pgid == pid, **_reap.known_fields([proc, *table.descendants(pid, prune=_reap.unsafe)]))
    with store.lock():
        store.save(entry)
    _events.write(store.root, "entry.adopted", _events.subject(entry), _events.actor_of(who),
                  {"cmd": proc.cmd, "ttl": ttl, "keep": keep, "pgid": proc.pgid, "group": entry["group"]})
    scope = "its group" if entry["group"] else "it and its descendants (not its group: it does not lead one)"
    print(f"junkfather: {entry['id']} adopted pid {pid} ({scope}) for {_owner.describe(who)}, ttl {duration(ttl)}; "
          f"stop it: junkfather reap --id {entry['id']}")
    return 0


def duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 120:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 120:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours}h{minutes:02d}m" if minutes else f"{hours}h"
    return f"{hours // 24}d{hours % 24}h"
