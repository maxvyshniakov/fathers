"""A background Bash command, owned on its own (fathers#166).

Claude Code's Bash tool runs each call in a shell that leads its own process group (`eval '<command>' && pwd -P
...`); with `run_in_background` nobody waits for it, and a wait loop left behind runs for hours. So the PreToolUse
rewriter (Mastack's `lifecycle.PreToolUse` entry with `"rewrite": true`, which bash_guard passes on) answers such a
call with `updatedInput`: the same input, the command preceded by one line,

    <junkfather> background <tool_use_id> >/dev/null 2>&1 </dev/null
    <the agent's command, unchanged>

and writes a claim for the call (owner.write_claim: session, subagent, command, description). The line's junkfather
adopts the shell that runs it, its parent, as an `adopted` entry marked `auto: "background"`, kept for the owner: a
subagent's with the TTL of `own.background_ttl`, a main-session call's with none (the session's end ends it). It
forks a watcher (`watch`) that follows the shell, so an `exec` and what the shell leaves in its group stay the
entry's. Then the agent's command runs in that same shell, so its output, exit status and shell state are what they
would have been: the line prints nothing, and its failure (no junkfather, no claim) only leaves the command unowned.
A `SubagentStop` can be the end of a turn while the call is still running. Lifecycle cleanup waits for the
session's end or the TTL (the `Stop` hook, the sweep). An explicit `reap` can still stop it.

Left alone: a foreground call, a call that is one explicit `junkfather own`, `own-resource` or `adopt`
(`owns_itself`: it owns what it starts, and the claim is the one `own` reads), a call without a session or a tool-use
id fit for a file name. The answer gives no permission decision, so Claude Code's permission rules and auto mode
judge the rewritten call, line by line; Mastack's checks judge the agent's command before it is rewritten.

`background` adopts only its own parent, once per claim, and only a claim the hook wrote for a background call of
the same session in the last CLAIM_WINDOW seconds; the safety rules of `adopt` apply (no terminal's process, no
agent session, nothing above junkfather but that one shell).
"""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import re
import select
import shlex
import sys
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol, Tuple, cast

from . import entry as _entry
from . import owner as _owner
from . import procs as _procs
from . import reap as _reap
from . import safety as _safety
from . import settings as _settings
from .defaults import CLAIM_WINDOW, HANDSHAKE, NO_TTL, POLL_EVERY, WAKE
from .errors import NOT_FOUND, REFUSED, Refused
from .entry import Entry
from .state import Store, as_dict, read_json, write_atomic

VERB = "background"
AUTO = "background"  # entry["auto"] of what this module adopts
QUIET = " >/dev/null 2>&1 </dev/null"


def wanted(data: Mapping[str, Any]) -> Optional[str]:
    """The command of a Bash call to own on its own, or None."""
    if data.get("hook_event_name") != "PreToolUse" or data.get("tool_name") != "Bash":
        return None
    tool_input = as_dict(data.get("tool_input"))
    command = tool_input.get("command")
    if tool_input.get("run_in_background") is not True or not isinstance(command, str) or not command.strip():
        return None
    if owns_itself(command):
        return None
    session, tool_use = data.get("session_id"), data.get("tool_use_id")
    if not (isinstance(session, str) and isinstance(tool_use, str)):
        return None
    return command


OPERATORS = "();<>|&\n"  # shell punctuation: a token of these alone ends a simple command or redirects
PREFIXES = {"exec", "command", "builtin", "nohup", "env", "time"}  # a program still follows these
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
PYTHON = re.compile(r"^python[0-9.]*$")
OWNING = ("own", "own-resource", "adopt")  # the junkfather commands that own what they start or name


def owns_itself(command: str) -> bool:
    """Whether the call is one explicit ownership command and nothing else: a single simple command whose program
    is junkfather (by name or path, or `python -m junkfather`; after assignments and `env`, `exec`, `nohup`...) and
    whose junkfather command is `own`, `own-resource` or `adopt`. Such a call owns what it starts, and its claim is
    the one `own` reads. Anything else is owned whole, `junkfather list; until ...; done` too: a reap of the shell
    leaves out what another entry holds (reap.targets_of). A command that cannot be split counts as compound."""
    if "junkfather" not in command:
        return False
    lexer = shlex.shlex(command, posix=True, punctuation_chars=OPERATORS)
    lexer.whitespace, lexer.whitespace_split = " \t\r", True
    try:
        tokens = list(lexer)
    except ValueError:
        return False
    while tokens and tokens[-1] in ("\n", ";"):
        tokens.pop()
    if any(token and all(ch in OPERATORS for ch in token) for token in tokens):
        return False  # a list, a pipeline, a subshell or a redirection: not one plain command
    words = iter(tokens)
    for word in words:
        if ASSIGNMENT.match(word) or word in PREFIXES or (word.startswith("-") and len(word) > 1):
            continue
        program = os.path.basename(word)
        if PYTHON.match(program):
            if next(words, None) != "-m" or next(words, None) != "junkfather":
                return False
        elif program != "junkfather":
            return False
        return next(words, None) in OWNING
    return False


def launcher() -> str:
    """This junkfather as a shell word: the installed command (~/.local/bin/junkfather) when it is what runs, else
    this interpreter on this package (a checkout, the tests)."""
    first = sys.argv[0] if sys.argv else ""
    if os.path.basename(first) == "junkfather" and os.path.isabs(first) and os.access(first, os.X_OK):
        return shlex.quote(first)
    from .launch import self_command
    return shlex.join(self_command())


def adopting_line(tool_use: str, command: str) -> str:
    return f"{command} {VERB} {shlex.quote(tool_use)}{QUIET}"


def rewrite(data: Mapping[str, Any], store: Store, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """The hook's answer to a background Bash call (the claim written), or None to leave the call alone."""
    command = wanted(data)
    if command is None:
        return None
    description = as_dict(data.get("tool_input")).get("description")
    if _owner.write_claim(store, data, now, background=True,
                          description=description if isinstance(description, str) else None) is None:
        return None
    updated = dict(as_dict(data.get("tool_input")))
    updated["command"] = adopting_line(str(data["tool_use_id"]), launcher()) + "\n" + command
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": updated}}


def take_claim(store: Store, tool_use: str, session: Optional[str], now: float) -> Dict[str, Any]:
    """The hook's claim for this call, marked used; Refused when there is none to take."""
    found = [path for path in store.claims_dir.glob(f"*/{tool_use}.json")
             if session is None or path.parent.name == session] \
        if _owner.SAFE_NAME.match(tool_use) and not tool_use.startswith(".") else []
    if len(found) != 1:
        raise Refused(f"no claim for {tool_use}" if not found else f"{tool_use} is claimed in several sessions",
                      NOT_FOUND)
    path: Path = found[0]
    with store.lock():  # two shells replaying one line: one takes the claim
        claim = as_dict(read_json(path))
        at = claim.get("at")
        if claim.get("background") is not True or claim.get("used") or not isinstance(at, (int, float)) \
                or not 0 <= now - at <= CLAIM_WINDOW or claim.get("session") != path.parent.name:
            raise Refused(f"the claim for {tool_use} is used, stale or not a background call's", REFUSED)
        claim["used"] = [[VERB]]
        write_atomic(path, claim)
    return claim


def adopt(store: Store, tool_use: str, env: Mapping[str, str], now: Optional[float] = None,
          watcher: Optional[Callable[[Store, Dict[str, Any]], None]] = None) -> int:
    """`junkfather background TOOL_USE_ID`: own the shell that runs this junkfather (its parent) for the call the
    claim names, and leave a watcher on it (`start_watcher`). A subagent's call keeps running through turn ends
    until the session ends or its TTL expires (own.background_ttl). A main-session call has no TTL and ends with
    the session. Prints nothing. Raises Refused when there is nothing it may own."""
    now = time.time() if now is None else now
    parent = os.getppid()
    claim = take_claim(store, tool_use, env.get("CLAUDE_CODE_SESSION_ID") or None, now)
    try:
        table = _procs.Table.read()
    except _procs.ProcError as error:
        raise Refused(str(error)) from None
    session_env = {**env, "CLAUDE_CODE_SESSION_ID": str(claim["session"])}  # an older Claude Code sets none
    who = _owner.resolve(session_env, table, os.getcwd(), store, agent=claim.get("agent") or None, claims=False)
    who["agent_type"] = claim.get("agent_type") if who.get("agent") else None
    ttl = _settings.value("own.background_ttl", store.root) if who.get("agent") else NO_TTL
    command = claim.get("command")
    from .launch import adopt_process
    entry = adopt_process(store, parent, table, ttl, "owner", claim.get("description") or None, who,
                          argv=[command] if isinstance(command, str) else None, own_parent=True,
                          extra={"auto": AUTO, "tool_use_id": tool_use, **({} if who.get("agent") else
                                                                            {"no_ttl": True})}, now=now)
    (watcher or start_watcher)(store, dict(entry))
    return 0


# The watcher

def start_watcher(store: Store, entry: Dict[str, Any]) -> None:
    """Fork a watcher on the entry's shell and return once it watches (or after HANDSHAKE seconds). The shell is this
    junkfather's parent and waits for it, so the watcher puts its eye on that very process before the agent's
    command runs. The watcher leaves the shell's session and closes every file the shell gave it, so it neither
    holds the call's output open nor dies with the shell's group."""
    if not watchable():
        return
    ready_r, ready_w = os.pipe()
    try:
        child = os.fork()
    except OSError:
        return
    if child:
        os.close(ready_w)
        select.select([ready_r], [], [], HANDSHAKE)
        os.close(ready_r)
        return
    try:
        os.close(ready_r)
        os.setsid()
        os.chdir("/")
        null = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(null, fd)
        os.closerange(3, ready_w)
        os.closerange(ready_w + 1, 1024)

        def ready() -> None:
            with contextlib.suppress(OSError):
                os.write(ready_w, b"1")
                os.close(ready_w)
        watch(store, str(entry["id"]), int(entry["pid"]), str(entry["start"]), ready)
    except BaseException:  # noqa: BLE001 - a watcher that fails only stops watching
        pass
    os._exit(0)


def watchable() -> bool:
    """Whether a watcher may run: not with JUNKFATHER_WATCH=off (the tests of what happens without one)."""
    return os.environ.get("JUNKFATHER_WATCH") != "off"


def watch(store: Store, entry_id: str, pid: int, start: str, ready: Callable[[], None] = lambda: None,
          mode: Optional[str] = None, eyes: Optional[Events] = None) -> None:
    """Follow the shell and what it leaves until none of it runs or the entry ends.

    The watched processes are the shell and, as they are found, the processes of its group and their descendants:
    each one by its pid and start time, with the kernel reporting its exec and exit (kqueue on macOS, a pidfd on
    Linux; elsewhere a look every POLL_EVERY s). After every event, and every WAKE s, the entry's known members
    become the watched processes that run, with their commands as they are now, so a reap recognizes an `exec`'d
    one. A process joins only through an identity that ties it to the shell: a descendant (parent chain, in the
    same table) of a watched process, or a member of the shell's group while a watched member of that group runs,
    so the group still exists and its id cannot have been taken again. When the last watched member of the group
    exits under the kernel's eye, the group's processes that started no later than that second join too (`sleep
    600 &` after the shell's exit). What another entry holds directly is left to it.

    When the entry was reaped (or failed) while some of it still runs (an `exec` the reap did not recognize yet),
    the watcher stops those of its processes the reap policy allows (reap.targets_of: never what another entry
    holds more firmly) and ends. With nothing left it marks the entry gone."""
    eyes = eyes or Eyes(mode or os.environ.get("JUNKFATHER_WATCH") or "")
    try:
        table = _procs.Table.read()
        shell = table.get(pid)
        if shell is None or shell.start != _procs.norm(start) or not eyes.add(pid):
            return
        me = table.get(os.getpid())
        _entry_supervised(store, entry_id, {"pid": os.getpid(), "start": me.start if me else None})
    finally:
        ready()
    watched: Dict[int, Tuple[str, int]] = {pid: (shell.start, shell.pgid)}  # pid -> (start, group id)
    while True:
        events = eyes.wait(WAKE)
        seen_at = time.time()
        entry = store.load(entry_id)
        if entry is None:
            return
        while entry is not None and entry.get("state") == _entry.REAPING:  # a reap is at work: let it finish
            time.sleep(0.5)
            entry = store.load(entry_id)
        if entry is None:
            return
        try:
            table = _procs.Table.read()
        except _procs.ProcError:
            continue
        if entry.get("state") != _entry.RUNNING:
            if entry.get("state") in (_entry.REAPED, _entry.FAILED):
                _stop_left(store, entry, table, watched)
            return
        group = int(entry.get("pgid") or 0) if entry.get("group") else 0
        exited_group = eyes.precise and any(event == "exit" and which in watched and watched[which][1] == group
                                            for event, which in events)
        watched = {p: held for p, held in watched.items() if table.alive(p, held[0])}
        for proc in _joining(store, entry, table, watched, exited_group, seen_at):
            if eyes.add(proc.pid) and _procs.identities([proc.pid]).get(proc.pid) == proc.start:
                watched[proc.pid] = (proc.start, proc.pgid)
        running = [table.procs[p] for p in watched if p in table.procs]
        if running:
            _entry.known(store, entry_id, running)
        else:
            _entry.gone(store, entry_id)
            return


def _joining(store: Store, entry: Entry, table: _procs.Table, watched: Dict[int, Tuple[str, int]], exited_group: bool,
             seen_at: float) -> List[_procs.Proc]:
    """The processes that join the watched ones now (see `watch`)."""
    found: Dict[int, _procs.Proc] = {}
    group = int(entry.get("pgid") or 0) if entry.get("group") else 0
    if group:
        witness = any(held[1] == group for held in watched.values())  # alive in this very table
        last_second = int(seen_at)
        for proc in table.group(group):
            began = proc.started
            if proc.pid in watched or began is None:
                continue
            if witness or (exited_group and int(began) <= last_second and table.get(group) is None):
                found[proc.pid] = proc
    for pid in [*watched, *found]:
        for proc in table.descendants(pid, prune=_safety.unsafe):
            if proc.pid not in watched:
                found.setdefault(proc.pid, proc)
    claimed = _reap.claimed_elsewhere(store, entry, table)
    return [p for p in _safety.signallable(found.values(), table)
            if (claimed.get(p.pid) or (0, False))[0] < _reap.DIRECT]


def _stop_left(store: Store, entry: Entry, table: _procs.Table, watched: Dict[int, Tuple[str, int]]) -> None:
    """The entry ended while some of it runs: stop what the reap policy allows of the watched processes (pid and
    start time as watched, the command as it is now, all read again just before each signal)."""
    left = [table.procs[p] for p in watched if p in table.procs and table.alive(p, watched[p][0])]
    if not left:
        return
    ended = cast(Entry, {**entry, "known": [[p.pid, p.start] for p in left],
                         "known_seen": [[p.pid, p.start, p.cmd] for p in left], "pid": None})
    targets, _ = _reap.targets_of(store, ended, table)
    if targets:
        _reap.stop(targets, _settings.value("reap.grace", store.root))


def _entry_supervised(store: Store, entry_id: str, supervisor: Dict[str, Any]) -> None:
    def change(e: Any) -> Optional[bool]:
        if e.get("state") != _entry.RUNNING:
            return False
        e["supervisor"] = supervisor
        return None
    store.update(entry_id, change)


_SELECT: Any = select  # kqueue and pidfd_open exist on some systems only: looked up where they do
_OS: Any = os


class Events(Protocol):
    """What the watcher needs of its event source (Eyes; a test's script)."""
    precise: bool

    def add(self, pid: int) -> bool: ...

    def wait(self, timeout: float) -> List[Tuple[str, int]]: ...


def _kq(name: str) -> int:
    """A kqueue constant (select has them only where kqueue exists)."""
    return int(getattr(_SELECT, name))


class Eyes:
    """Exec and exit events of the processes it watches: kqueue (macOS, BSD), pidfds (Linux; exit only, exec by a
    look every POLL_EVERY s), or looks every POLL_EVERY s (`poll`, also the tests' way). `precise`: an exit is
    reported as it happens."""

    def __init__(self, mode: str = ""):
        self.mode = mode or ("kqueue" if hasattr(select, "kqueue") else
                             "pidfd" if hasattr(os, "pidfd_open") else "poll")
        self.precise = self.mode in ("kqueue", "pidfd")
        self.queue: Any = _SELECT.kqueue() if self.mode == "kqueue" else None
        self.fds: Dict[int, int] = {}  # pidfd -> pid
        self.commands: Dict[int, Tuple[str, str]] = {}  # pid -> (start, command) as last looked at

    def add(self, pid: int) -> bool:
        """Watch `pid` from now on; False when it no longer runs."""
        seen = _procs.identities([pid], command=True).get(pid)
        if seen is None:
            return False
        self.commands[pid] = seen
        try:
            if self.mode == "kqueue":
                flags = _kq("KQ_EV_ADD") | _kq("KQ_EV_ENABLE") | _kq("KQ_EV_CLEAR")
                self.queue.control([_SELECT.kevent(pid, _kq("KQ_FILTER_PROC"), flags,
                                                   _kq("KQ_NOTE_EXIT") | _kq("KQ_NOTE_EXEC"))], 0)
            elif self.mode == "pidfd":
                self.fds[_OS.pidfd_open(pid)] = pid
        except OSError:
            self.commands.pop(pid, None)
            return False
        return True

    def wait(self, timeout: float) -> List[Tuple[str, int]]:
        """The events that came within `timeout` seconds: ("exec" | "exit", pid); [] on a quiet wake."""
        if self.mode == "kqueue":
            found: List[Tuple[str, int]] = []
            for event in self.queue.control(None, 64, timeout):
                if event.fflags & _kq("KQ_NOTE_EXIT"):
                    found.append(("exit", int(event.ident)))
                    self.commands.pop(int(event.ident), None)
                elif event.fflags & _kq("KQ_NOTE_EXEC"):
                    found.append(("exec", int(event.ident)))
            return found
        deadline = time.monotonic() + timeout
        while True:
            found = self._looked(min(POLL_EVERY, max(0.0, deadline - time.monotonic())))
            if found or time.monotonic() >= deadline:
                return found

    def _looked(self, pause: float) -> List[Tuple[str, int]]:
        found: List[Tuple[str, int]] = []
        if self.mode == "pidfd" and self.fds:
            readable, _, _ = select.select(list(self.fds), [], [], pause)
            for fd in readable:
                found.append(("exit", self.fds.pop(fd)))
                os.close(fd)
        else:
            time.sleep(pause)
        gone = {pid for _, pid in found}
        now = _procs.identities(list(self.commands), command=True)
        for pid, (began, command) in list(self.commands.items()):
            current = now.get(pid)
            if pid in gone or current is None or current[0] != began:
                if pid not in gone:
                    found.append(("exit", pid))
                self.commands.pop(pid, None)
            elif current[1] != command:
                self.commands[pid] = current
                found.append(("exec", pid))
        return found


def answer(output: Dict[str, Any]) -> None:
    """The hook's one line of stdout."""
    sys.stdout.write(json.dumps(output) + "\n")
    sys.stdout.flush()
