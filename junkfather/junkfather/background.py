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
Its end is any entry's: the subagent's stop (`SubagentStop`), the session's end, the TTL (the `Stop` hook, the
sweep), `reap`.

Left alone: a foreground call, a call that is one explicit `junkfather own`, `own-resource` or `adopt`
(`owns_itself`: it owns what it starts, and the claim is the one `own` reads), a call without a session or a tool-use
id fit for a file name. The answer gives no permission decision, so Claude Code's permission rules and auto mode
judge the rewritten call, line by line; Mastack's checks judge the agent's command before it is rewritten.

`background` adopts only its own parent, once per claim, and only a claim the hook wrote for a background call of
the same session in the last CLAIM_WINDOW seconds; the safety rules of `adopt` apply (no terminal's process, no
agent session, nothing above junkfather but that one shell).
"""
from __future__ import annotations

import itertools
import json
import os
from pathlib import Path
import re
import select
import shlex
import sys
import time
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional

from . import entry as _entry
from . import owner as _owner
from . import procs as _procs
from . import safety as _safety
from . import settings as _settings
from .defaults import CLAIM_WINDOW, NO_TTL, POLL_EVERY
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
    claim names, and leave a watcher on it (`start_watcher`). A subagent's call ends with the subagent or its TTL
    (own.background_ttl); a main-session call has no TTL and ends with the session. Prints nothing; Refused when
    there is nothing it may own."""
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
    """Fork a watcher on the entry's shell and return at once. The watcher leaves the shell's session and closes
    every file the shell gave it, so it neither holds the call's output open nor dies with the shell's group."""
    if not watchable():
        return
    try:
        child = os.fork()
    except OSError:
        return
    if child:
        return
    try:
        os.setsid()
        os.chdir("/")
        null = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(null, fd)
        os.closerange(3, 1024)
        watch(store, str(entry["id"]), int(entry["pid"]), str(entry["start"]), str(entry["seen"]))
    except BaseException:  # noqa: BLE001 - a watcher that fails only stops watching
        pass
    os._exit(0)


def watchable() -> bool:
    """Whether a watcher may run: not with JUNKFATHER_WATCH=off (the tests of what happens without one)."""
    return os.environ.get("JUNKFATHER_WATCH") != "off"


def watch(store: Store, entry_id: str, pid: int, start: str, seen: str, mode: Optional[str] = None) -> None:
    """Follow one shell until it exits. On exec (the agent's command ran `exec ...`) the entry's members are
    recorded again with the leader's new command, so a reap still recognizes it; on exit the members left in its
    group (`sleep 600 &`) are recorded as known members, or, with none, the entry is gone. Both observe the very
    process the kernel names (kqueue) or one whose start time held between two looks a few seconds apart (poll),
    never a pid alone. Ends when the shell exits or the entry stops running."""
    mode = mode or os.environ.get("JUNKFATHER_WATCH") or ("kqueue" if hasattr(select, "kqueue") else "poll")
    # kqueue (macOS, BSD) reports the shell's exit and exec; elsewhere (Linux) the watcher looks every POLL_EVERY s.
    # The watch starts before the first look, so what happens after that look is reported and what happened before
    # it (the shell ran `true`, or exec'd at once) shows in it.
    events = _kqueue_events(pid) if mode == "kqueue" else _poll_events(pid, start, seen)
    table = _procs.Table.read()
    shell = table.get(pid)
    me = table.get(os.getpid())
    _entry_supervised(store, entry_id, {"pid": os.getpid(), "start": me.start if me else None})
    if shell is None or shell.start != _procs.norm(start):
        events = iter(["exit"])
    elif shell.cmd != seen:
        events = itertools.chain(["exec"], events)
    for event in events:
        entry = store.load(entry_id)
        if entry is None or entry.get("state") != _entry.RUNNING:
            return
        table = _procs.Table.read()
        if event == "exec":
            leader = table.get(pid)
            if leader is not None and leader.start == _procs.norm(start):
                _entry.known(store, entry_id, _members(entry, table, leader))
            continue
        left = _members(entry, table, None)
        if left:
            _entry.known(store, entry_id, left)
        else:
            _entry.gone(store, entry_id)
        return


def _members(entry: Entry, table: _procs.Table, leader: Optional[_procs.Proc]) -> List[_procs.Proc]:
    """The shell (when it runs), what is in its group, and their descendants, that a reap may signal; the shell's
    group only by its id and only with a start no earlier than the shell's, the moment the kernel said the shell
    exited or exec'd (the group still exists then, so its id cannot have been taken again)."""
    found: Dict[int, _procs.Proc] = {}
    if leader is not None:
        found[leader.pid] = leader
    shell_start = _procs.started_at(str(entry.get("start") or ""))
    if entry.get("group"):
        for proc in table.group(int(entry.get("pgid") or 0)):
            began = proc.started
            if proc.pid != entry.get("pid") and shell_start is not None and began is not None and began >= shell_start:
                found[proc.pid] = proc
    for pid in list(found):
        for proc in table.descendants(pid, prune=_safety.unsafe):
            found.setdefault(proc.pid, proc)
    return sorted(_safety.signallable(found.values(), table), key=lambda p: p.pid)


def _entry_supervised(store: Store, entry_id: str, supervisor: Dict[str, Any]) -> None:
    def change(e: Any) -> Optional[bool]:
        if e.get("state") != _entry.RUNNING:
            return False
        e["supervisor"] = supervisor
        return None
    store.update(entry_id, change)


def _kqueue_events(pid: int) -> Iterator[str]:
    """"exec" and "exit" of `pid` as the kernel reports them, watched from this call on."""
    queue = select.kqueue()
    try:
        flags = select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_CLEAR
        queue.control([select.kevent(pid, select.KQ_FILTER_PROC, flags, select.KQ_NOTE_EXIT | select.KQ_NOTE_EXEC)],
                      0)
    except OSError:  # it exited already
        return iter(["exit"])
    return _kqueue_wait(queue)


def _kqueue_wait(queue: Any) -> Iterator[str]:
    while True:
        for event in queue.control(None, 4, None):
            if event.fflags & select.KQ_NOTE_EXIT:
                yield "exit"
                return
            if event.fflags & select.KQ_NOTE_EXEC:
                yield "exec"


def _poll_events(pid: int, start: str, command: str, every: float = POLL_EVERY) -> Iterator[str]:
    """"exec" and "exit" of `pid` by looking every few seconds: gone or another start time is an exit, the same
    start time with another command an exec."""
    while True:
        time.sleep(every)
        seen = _procs.identities([pid], command=True).get(pid)
        if seen is None or seen[0] != _procs.norm(start):
            yield "exit"
            return
        if seen[1] != command:
            command = seen[1]
            yield "exec"


def answer(output: Dict[str, Any]) -> None:
    """The hook's one line of stdout."""
    sys.stdout.write(json.dumps(output) + "\n")
    sys.stdout.flush()
