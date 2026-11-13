"""`junkfather hook`: the agent lifecycle hook. Claude Code (and Codex, which has the same JSON contract) pipes the
event's JSON on stdin; Mastack's hook dispatcher runs this for the events it is registered for.

  PreToolUse    a Bash call whose command names junkfather: write a claim (session, agent id, command) so that
                `own` learns which subagent ran it (owner.py)
  SubagentStop  reap what that subagent owns, except entries kept for the session
  SessionEnd    reap everything the session owns that was not handed to another session
  Stop          (end of a main-agent turn, not of the session) reap the session's entries whose TTL passed

The hook never blocks, never prints and always exits 0: a reap that has work to do runs in a detached process
(`junkfather reap ...`), so the agent never waits for a grace period. It fails open on unreadable input.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Any, Callable, Dict, List, Optional

from . import entry as _entry
from . import owner as _owner
from .state import Store, as_dict, as_list


def plan(data: Dict[str, Any], store: Store, now: Optional[float] = None) -> Optional[List[str]]:
    """The reap arguments this event calls for, or None when there is nothing of its owner to reap."""
    now = time.time() if now is None else now
    event, session = data.get("hook_event_name"), data.get("session_id")
    if not isinstance(session, str) or not session:
        return None
    running = [e for e in store.entries() if e.get("state") == _entry.RUNNING
               and (e.get("owner") or {}).get("session") == session]
    if event == "SubagentStop":
        agent = data.get("agent_id")
        if not isinstance(agent, str) or not agent:
            return None
        if not any((e.get("owner") or {}).get("agent") == agent and e.get("keep") == "owner" for e in running):
            return None
        return ["reap", "--owner", session, "--agent", agent, "--reason", "subagent-stop", "--quiet", "--via", "hook"]
    if event == "SessionEnd":
        if not running:
            return None
        return ["reap", "--owner", session, "--reason", f"session-end ({data.get('reason') or '?'})", "--quiet",
                "--via", "hook"]
    if event == "Stop":
        if not any(now >= float(e.get("expires") or 0) for e in running):
            return None
        return ["reap", "--owner", session, "--expired", "--reason", "ttl expired (stop)", "--quiet", "--via", "hook"]
    return None


def handle(data: Dict[str, Any], store: Store, spawn: Optional[Callable[[List[str]], None]] = None
           ) -> Optional[List[str]]:
    event = data.get("hook_event_name")
    if event == "PreToolUse":
        command = _owner_command(data)
        if command is not None and "junkfather" in command:
            _owner.write_claim(store, data)
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
        subprocess.Popen(argv, stdin=null, stdout=out, stderr=out, start_new_session=True, close_fds=True)


def main() -> int:
    try:
        data = json.load(sys.stdin)
        if isinstance(data, dict):
            handle(as_dict(data), Store())
    except Exception as error:  # noqa: BLE001 - a hook fails open
        from . import events as _events
        _events.write(Store().root, "hook.failed", {"type": "hook", "id": "junkfather-hook"}, _events.tool("hook"),
                      {"error": repr(error)}, "error")
    return 0
