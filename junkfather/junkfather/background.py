"""A background Bash command, owned on its own (fathers#166).

Claude Code's Bash tool runs each call in a shell of its own process group (`eval '<command>' && pwd -P ...`); with
`run_in_background` nobody waits for it, and a wait loop left behind runs for hours. So the PreToolUse hook answers
such a call with `updatedInput`: the same input, the command preceded by one line,

    <junkfather> background <tool_use_id> >/dev/null 2>&1 </dev/null
    <the agent's command, unchanged>

and writes a claim for the call (owner.write_claim: session, subagent, command, description). The line's junkfather
adopts the shell that runs it, its parent, as an `adopted` entry marked `auto: "background"`: owned by the session
and the subagent of the claim, kept for the owner, with the TTL of the `own.background_ttl` setting. Then the agent's
command runs in that same shell, so its output, exit status and shell state are what they would have been: the
line prints nothing, and its failure (no junkfather, no claim) only leaves the command unowned. Its end is any
entry's: the subagent's stop (`SubagentStop`), the session's end, the TTL (the `Stop` hook, the sweep), `reap`. A
shell that ended on its own is marked gone by the sweep, or by the session's next background call (`tidy`).

Left alone: a foreground call, a call that runs junkfather (`runs_junkfather`: it owns what it needs itself, and
the claim is the one `own` reads), a call without a session or a tool-use id fit for a file name. The hook gives no
permission decision, so Claude Code's permission rules, Mastack's checks and auto mode judge the call as usual
(the rules see the rewritten command, Mastack's checks, which run beside this hook, the agent's).

`background` adopts only its own parent, once per claim, and only a claim the hook wrote for a background call of
the same session in the last CLAIM_WINDOW seconds; the safety rules of `adopt` apply (no terminal's process, no
agent session, nothing above junkfather but that one shell).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import sys
import time
from typing import Any, Dict, Mapping, Optional

from . import entry as _entry
from . import events as _events
from . import owner as _owner
from . import procs as _procs
from . import reap as _reap
from . import safety as _safety
from . import settings as _settings
from .defaults import CLAIM_WINDOW
from .errors import NOT_FOUND, REFUSED, Refused
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
    if runs_junkfather(command):
        return None
    session, tool_use = data.get("session_id"), data.get("tool_use_id")
    if not (isinstance(session, str) and isinstance(tool_use, str)):
        return None
    return command


OPERATORS = "();<>|&\n"  # shell punctuation; a token of these alone separates commands or redirects
STARTERS = {"!", "{", "then", "do", "else", "elif", "if", "while", "until", "time", "exec", "command", "builtin",
            "nohup", "env"}  # after these a word is still in a command's program position
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
PYTHON = re.compile(r"^python[0-9.]*$")
SUBSTITUTED = re.compile(r"(\$\(|`)\s*(?:[^\s;|&()`]*/)?junkfather(?=[\s;|&()`]|$)")


def runs_junkfather(command: str) -> bool:
    """Whether a simple command of `command` runs junkfather: its program (after assignments and `env`, `exec`,
    `nohup`...) is junkfather by name or path, `python -m junkfather`, or a command substitution starting with it.
    A command that only names it (`cd ~/fathers/junkfather && make`) does not. One that cannot be split (an open
    quote) counts when it names junkfather at all, as every such command did before."""
    if "junkfather" not in command:
        return False
    if SUBSTITUTED.search(command):
        return True
    lexer = shlex.shlex(command, posix=True, punctuation_chars=OPERATORS)
    lexer.whitespace, lexer.whitespace_split = " \t\r", True
    try:
        tokens = list(lexer)
    except ValueError:
        return True
    start, redirected, python = True, False, False
    for token in tokens:
        if token and all(ch in OPERATORS for ch in token):
            if any(ch not in "<>" for ch in token):
                start, python = True, False
            else:
                redirected = True  # the next word is a file
            continue
        if redirected:
            redirected = False
            continue
        if python:
            if token == "junkfather":
                return True
            python = token.startswith("-") and token != "-c"
            continue
        if not start:
            continue
        if ASSIGNMENT.match(token) or token in STARTERS or (token.startswith("-") and token != "-"):
            continue
        program = os.path.basename(token)
        if program == "junkfather":
            return True
        python = PYTHON.match(program) is not None
        start = False
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


def adopt(store: Store, tool_use: str, env: Mapping[str, str], parent: Optional[int] = None,
          now: Optional[float] = None) -> int:
    """`junkfather background TOOL_USE_ID`: own the shell that runs this junkfather (its parent) for the call the
    claim names. Prints nothing; Refused when there is nothing it may own."""
    now = time.time() if now is None else now
    parent = os.getppid() if parent is None else parent
    claim = take_claim(store, tool_use, env.get("CLAUDE_CODE_SESSION_ID") or None, now)
    read_at = time.time()
    try:
        table = _procs.Table.read()
    except _procs.ProcError as error:
        raise Refused(str(error)) from None
    tidy(store, str(claim["session"]), table, read_at)
    proc = table.get(parent)
    refusal = _safety.adopt_refusal(parent, table, lambda candidate: any(
        e.get("state") == _entry.RUNNING and e.get("pid") == candidate for e in store.entries()), parent=parent)
    if refusal or proc is None:
        raise Refused(f"refusing the shell {parent}: {refusal or 'no such process'}", REFUSED)
    session_env = {**env, "CLAUDE_CODE_SESSION_ID": str(claim["session"])}  # an older Claude Code sets none
    who = _owner.resolve(session_env, table, os.getcwd(), store, agent=claim.get("agent") or None, claims=False)
    who["agent_type"] = claim.get("agent_type") if who.get("agent") else None
    ttl = _settings.value("own.background_ttl", store.root)
    label = claim.get("description") or None
    made = _entry.new(store, [str(claim.get("command") or proc.cmd)], ttl, "owner", label, who, "adopted",
                      os.getcwd(), now)
    made["auto"] = AUTO
    me = os.getpid()
    entry = _entry.adopted(store, made, proc,
                           [proc, *(p for p in table.descendants(parent, prune=_safety.unsafe) if p.pid != me)])
    _events.write(store.root, "entry.adopted", _events.subject(entry), _events.actor_of(who),
                  {"cmd": proc.cmd, "ttl": ttl, "keep": "owner", "pgid": proc.pgid, "group": entry["group"],
                   "auto": AUTO, "tool_use_id": tool_use})
    return 0


def tidy(store: Store, session: str, table: _procs.Table, read_at: float) -> None:
    """The session's background entries whose shells ended are marked gone, as the sweep would mark them, so `list`
    shows what runs. Only those made before `table` was read: a shell adopting itself meanwhile is not in it."""
    for entry in store.entries():
        if entry.get("auto") == AUTO and entry.get("state") == _entry.RUNNING \
                and (entry.get("owner") or {}).get("session") == session \
                and float(entry.get("created") or read_at) < read_at and not _reap.members(entry, table)[0]:
            _entry.gone(store, entry["id"])


def answer(output: Dict[str, Any]) -> None:
    """The hook's one line of stdout."""
    sys.stdout.write(json.dumps(output) + "\n")
    sys.stdout.flush()
