"""`spawnfather hook`: the agent lifecycle hook. Claude Code (and Codex, which has the same JSON contract) pipes the
event's JSON on stdin; Mastack's hook dispatcher runs this for the events it is registered for.

  PreToolUse    a Bash call whose command names spawnfather (or legacy junkfather): write a claim (session, agent id,
                command) so that `own` learns which subagent ran it (owner.py); a Bash call with `run_in_background`
                that is not one explicit `spawnfather own`: answer with the command rewritten to own its shell first
                (background.py), the one thing the hook prints
  SubagentStop  reap the subagent's explicit ownership, except entries kept for the session
  SessionEnd    for a terminal reason (`prompt_input_exit`, `clear`, `logout`, `bypass_permissions_disabled`, or an
                omitted reason), record that the session ended (leftovers.py: what the sweep may stop of a session that
                is gone) and reap everything the session owns that was not handed to another session; for `other` (sent
                both by T3 Code between turns and by Claude Code on CLI signal/`-p` exits), schedule a detached reap
                with `--if-owner-dead` that reaps and records the session ended only if its owning process has exited;
                transient or invalid reasons such as `resume` leave the session alive
  Stop          (end of a main-agent turn, not of the session) reap the session's entries whose TTL passed

The hook never blocks and always exits 0, and prints only that answer: a reap that has work to do runs in a detached
process (`spawnfather reap ...`), so the agent never waits for a grace period. It fails open on unreadable input.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Any, Callable, Dict, List, Optional

from . import background as _background
from . import entry as _entry
from . import owner as _owner
from . import reap as _reap
from .state import Store, as_dict, as_list

TERMINAL_SESSION_END_REASONS = frozenset({
    "prompt_input_exit",
    "clear",
    "logout",
    "bypass_permissions_disabled",
})


def _is_terminal_session_end(data: Dict[str, Any]) -> bool:
    """Whether a SessionEnd payload ends the session for good.

    Omitted/null `reason` is treated as terminal (Codex and older hooks). When present, `reason` must be a known
    terminal string; transient/harness reasons such as `"other"` (fired between turns in T3 Code) or `"resume"`,
    as well as non-string values, leave the session alive.
    """
    reason = data.get("reason")
    if reason is None:
        return True
    return isinstance(reason, str) and reason in TERMINAL_SESSION_END_REASONS


def _is_other_session_end(data: Dict[str, Any]) -> bool:
    """Whether a SessionEnd payload carries `reason: "other"` (ambiguous: T3 Code between turns or CLI signal exit)."""
    reason = data.get("reason")
    return isinstance(reason, str) and reason == "other"


def plan(data: Dict[str, Any], store: Store, now: Optional[float] = None) -> Optional[List[str]]:
    """The reap arguments this event calls for, or None when there is nothing of its owner to reap."""
    now = time.time() if now is None else now
    event, session = data.get("hook_event_name"), data.get("session_id")
    if not isinstance(session, str) or not session:
        return None
    running = [e for e in store.entries() if e.get("state") == _entry.RUNNING
               and (e.get("owner") or {}).get("session") == session]
    if event == _reap.Lifecycle.SUBAGENT_STOP:
        agent = data.get("agent_id")
        if not isinstance(agent, str) or not agent:
            return None
        lifecycle = _reap.Lifecycle.SUBAGENT_STOP
        if not any(_reap.matches_owner(e, session, agent, lifecycle=lifecycle) for e in running):
            return None
        return ["reap", "--owner", session, "--agent", agent, "--lifecycle", lifecycle.value,
                "--reason", "subagent-stop", "--quiet", "--via", "hook"]
    if event == _reap.Lifecycle.SESSION_END:
        if not running:
            return None
        if _is_terminal_session_end(data):
            reason = data.get("reason")
            label = reason if isinstance(reason, str) and reason else "?"
            return ["reap", "--owner", session, "--lifecycle", _reap.Lifecycle.SESSION_END.value,
                    "--reason", f"session-end ({label})", "--quiet", "--via", "hook"]
        if _is_other_session_end(data):
            return ["reap", "--owner", session, "--lifecycle", _reap.Lifecycle.SESSION_END.value,
                    "--if-owner-dead", "--reason", "session-end (other)", "--quiet", "--via", "hook"]
        return None
    if event == "Stop":
        if not any(_entry.due(e, now) for e in running):
            return None
        return ["reap", "--owner", session, "--expired", "--reason", "ttl expired (stop)", "--quiet", "--via", "hook"]
    return None


def handle(data: Dict[str, Any], store: Store, spawn: Optional[Callable[[List[str]], None]] = None,
           emit: Optional[Callable[[Dict[str, Any]], None]] = None) -> Optional[List[str]]:
    """Act on one event: write a claim, answer a background Bash call (`emit`, default stdout), or start a reap
    (`spawn`, default detached). Returns the reap's arguments."""
    event = data.get("hook_event_name")
    if event == "PreToolUse":
        command = _owner_command(data)
        if command is not None and ("spawnfather" in command or "junkfather" in command):
            _owner.write_claim(store, data)  # a background call's claim below replaces it, for the same file
        answer = _background.rewrite(data, store)
        if answer is not None:
            (emit or _background.answer)(answer)
        return None
    if event == _reap.Lifecycle.SESSION_END:
        if _is_terminal_session_end(data):
            from . import leftovers as _leftovers  # here: PreToolUse, on every Bash call, does not need it
            try:
                _leftovers.record_end(store, data.get("session_id"), data.get("reason"))
            except OSError:
                pass  # the sweep then has one proof less, and keeps that session's processes report-only
        elif not _is_other_session_end(data):
            return None
    arguments = plan(data, store)
    if arguments:
        from .launch import self_command
        (spawn or detached)(self_command(*arguments))
    return arguments


def _owner_command(data: Dict[str, Any]) -> Optional[str]:
    if data.get("tool_name") not in ("Bash", "shell", "local_shell", "exec_command"):
        return None
    command = as_dict(data.get("tool_input")).get("command")
    if isinstance(command, list):
        command = " ".join(str(word) for word in as_list(command))
    return command if isinstance(command, str) else None


def detached(argv: List[str]) -> None:
    log = Store().root / "hook.out"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(os.devnull, "rb") as null, open(log, "ab") as out:
        # In "/": the reaper must not hold the folder the hook ran in (a worktree its reap may remove).
        subprocess.Popen(argv, stdin=null, stdout=out, stderr=out, start_new_session=True, close_fds=True, cwd="/")


def main() -> int:
    try:
        data = json.load(sys.stdin)
        if isinstance(data, dict):
            handle(as_dict(data), Store())
    except Exception as error:  # noqa: BLE001 - a hook fails open
        from . import events as _events
        _events.write(Store().root, "hook.failed", {"type": "hook", "id": "spawnfather-hook"}, _events.tool("hook"),
                      {"error": repr(error)}, "error")
    return 0
