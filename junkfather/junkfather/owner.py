"""Who owns an entry, and whether that owner still lives.

An owner is the agent session that ran `junkfather own` (Claude Code or Codex), the subagent inside it when the
PreToolUse hook saw which one ran the command, and the lane, when one is known. Outside an agent the owner is the
process that ran junkfather (a terminal's shell, a script).

  claude   CLAUDE_CODE_SESSION_ID; the session's process is CLAUDE_PID. Claude Code keeps a registry of live
           sessions, ~/.claude/sessions/<pid>.json ({pid, sessionId, procStart, startedAt}); a session lives while
           its recorded process runs or the registry names it with a running process (a resumed session gets a
           new process). $JUNKFATHER_CLAUDE_SESSIONS moves the registry (tests).
  codex    CODEX_THREAD_ID (or CODEX_SESSION_ID); the session's process is the nearest `codex` ancestor.
  shell    the nearest ancestor with a terminal (a person's shell); none outside a terminal (a script, cron), and
           then only the TTL ends the entry.

The subagent comes from a claim: the PreToolUse hook writes the session, agent id and command of every Bash call
that names junkfather (hook.py); `own` takes the newest claim of its session whose command holds `own` and every
one of its arguments, and marks it used for those arguments; anything less leaves the entry to the session alone (a
session-level entry lives longer, never shorter). Two
agents of one session running the same command within CLAIM_AMBIGUOUS seconds make the claim ambiguous: the entry
then belongs to the session alone, and says so.
"""
from __future__ import annotations

import calendar
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Dict, List, Mapping, Optional, Tuple

from . import procs as _procs
from .state import Store, read_json, write_atomic

CLAUDE = re.compile(r"(^|/)claude(\s|$)|/claude/versions/[^/\s]+(\s|$)|/claude\.app/Contents/MacOS/claude(\s|$)")
CODEX = re.compile(r"(^|/)(codex|CodexCLI)(\s|$)|/codex-[\w-]+/vendor/[^\s]*/codex(\s|$)")
CLAIM_WINDOW = 1800.0  # seconds a claim may wait for its command (a permission prompt can take a while)
CLAIM_AMBIGUOUS = 60.0  # two agents' matching claims this close together: ambiguous
CLAIM_KEEP = 86400.0  # claims older than this are pruned

Owner = Dict[str, Any]


def registry_dir() -> Path:
    return Path(os.environ.get("JUNKFATHER_CLAUDE_SESSIONS") or Path.home() / ".claude" / "sessions").expanduser()


def lane_of(env: Mapping[str, str], cwd: str) -> Optional[str]:
    """LANEFATHER_LANE or JUNKFATHER_LANE, else the lane whose worktree holds cwd in the lane records
    ($JUNKFATHER_LANE_RECORDS, or `lane_records` in ~/.config/mastack/hooks.json)."""
    for name in ("JUNKFATHER_LANE", "LANEFATHER_LANE"):
        if env.get(name):
            return env[name]
    records = env.get("JUNKFATHER_LANE_RECORDS")
    if not records:
        config = read_json(Path(env.get("MASTACK_CONFIG") or Path.home() / ".config" / "mastack") / "hooks.json")
        records = config.get("lane_records") if isinstance(config, dict) else None
    if not isinstance(records, str) or not records:
        return None
    root = os.path.realpath(cwd)
    try:
        names = sorted(os.listdir(os.path.expanduser(records)))
    except OSError:
        return None
    best: Optional[Tuple[int, str]] = None
    for name in names:
        data = read_json(Path(os.path.expanduser(records)) / name) if name.endswith(".json") else None
        if not isinstance(data, dict) or not isinstance(data.get("worktree"), str) or not data.get("id"):
            continue
        tree = os.path.realpath(data["worktree"])
        if (root == tree or root.startswith(tree.rstrip("/") + "/")) and data.get("status") not in ("done", "released"):
            if best is None or len(tree) > best[0]:
                best = (len(tree), str(data["id"]))
    return best[1] if best else None


def nearest(table: Optional[_procs.Table], pid: int, pattern: "re.Pattern[str]") -> Optional[_procs.Proc]:
    if table is None:
        return None
    for here in [pid, *table.ancestors(pid)]:
        proc = table.get(here)
        if proc is not None and pattern.search(proc.cmd):
            return proc
    return None


def detect(env: Mapping[str, str], table: Optional[_procs.Table], cwd: str, me: Optional[int] = None) -> Owner:
    me = os.getpid() if me is None else me
    owner: Owner = {"kind": "shell", "session": None, "agent": None, "agent_type": None, "pid": None,
                    "pid_start": None, "lane": lane_of(env, cwd)}
    session = env.get("CLAUDE_CODE_SESSION_ID")
    codex = env.get("CODEX_THREAD_ID") or env.get("CODEX_SESSION_ID")
    if session:
        owner.update(kind="claude", session=session)
        pid = int(env["CLAUDE_PID"]) if (env.get("CLAUDE_PID") or "").isdigit() else None
        proc = table.get(pid) if table is not None and pid else nearest(table, me, CLAUDE)
        if proc is None and table is not None:
            proc = nearest(table, me, CLAUDE)
    elif codex:
        owner.update(kind="codex", session=codex)
        proc = nearest(table, me, CODEX)
    else:
        proc = None
        if table is not None and me in table.procs:
            proc = next((table.procs[p] for p in table.ancestors(me) if table.procs[p].tty), None)
    if proc is None and owner["kind"] == "claude" and table is not None:
        for record in registry() or []:
            if record.get("sessionId") == session and registered_alive(record, table):
                proc = table.get(record["pid"])
                break
    if proc is not None:
        owner.update(pid=proc.pid, pid_start=proc.start)
    return owner


def describe(owner: Owner) -> str:
    if owner.get("kind") == "shell":
        text = f"shell pid {owner.get('pid')}"
    else:
        text = f"{owner.get('kind')} {str(owner.get('session') or '?')[:8]}"
        if owner.get("agent"):
            text += f"/{str(owner['agent'])[:8]}"
    return text + (f" lane {owner['lane']}" if owner.get("lane") else "")


# Liveness

def registry(directory: Optional[Path] = None) -> Optional[List[Dict[str, Any]]]:
    """Claude Code's live-session records; None when the registry directory does not exist."""
    directory = directory or registry_dir()
    try:
        names = os.listdir(directory)
    except OSError:
        return None
    found = []
    for name in names:
        if name.endswith(".json"):
            data = read_json(directory / name)
            if isinstance(data, dict) and isinstance(data.get("pid"), int):
                found.append(data)
    return found


def registered_alive(record: Dict[str, Any], table: _procs.Table) -> bool:
    """Whether a registry record's process still runs: its pid, started when the record says (procStart is UTC;
    startedAt, milliseconds since the epoch, is taken a moment after the process started)."""
    proc = table.get(record.get("pid"))
    if proc is None or proc.started is None:
        return False
    if isinstance(record.get("procStart"), str):
        try:
            return abs(calendar.timegm(time.strptime(_procs.norm(record["procStart"]), _procs.LSTART))
                       - proc.started) <= 1  # both UTC
        except ValueError:
            pass
    if isinstance(record.get("startedAt"), (int, float)):
        return -2 <= record["startedAt"] / 1000 - proc.started <= 120
    return bool(CLAUDE.search(proc.cmd))


def alive(owner: Owner, table: Optional[_procs.Table],
          sessions: Optional[List[Dict[str, Any]]] = None) -> Optional[bool]:
    """True: the owner runs; False: it is gone; None: unknown (never a reason to reap)."""
    if table is None:
        return None
    if owner.get("pid") and table.alive(owner.get("pid"), owner.get("pid_start")):
        return True
    if owner.get("kind") == "claude" and owner.get("session"):
        if sessions is None:
            sessions = registry()
        if sessions is not None and any(r.get("sessionId") == owner["session"] and registered_alive(r, table)
                                        for r in sessions):
            return True
    # Without a recorded process only a positive answer counts: an unregistered session is unknown, not gone.
    return False if owner.get("pid") else None


# Claims

def claim_path(store: Store, session: str, tool_use: str) -> Path:
    safe = re.compile(r"^[\w.-]{1,128}$")
    if not (safe.match(session) and safe.match(tool_use)) or session.startswith(".") or tool_use.startswith("."):
        raise ValueError("unsafe claim name")
    return store.claims_dir / session / f"{tool_use}.json"


def write_claim(store: Store, data: Mapping[str, Any], now: Optional[float] = None) -> Optional[Path]:
    session, tool_use = data.get("session_id"), data.get("tool_use_id")
    command = (data.get("tool_input") or {}).get("command") if isinstance(data.get("tool_input"), dict) else None
    if not (isinstance(session, str) and isinstance(tool_use, str) and isinstance(command, str)):
        return None
    try:
        path = claim_path(store, session, tool_use)
    except ValueError:
        return None
    now = time.time() if now is None else now
    write_atomic(path, {"v": 1, "at": now, "session": session, "agent": data.get("agent_id"),
                        "agent_type": data.get("agent_type"), "tool_use_id": tool_use, "command": command})
    prune_claims(store, now)
    return path


def prune_claims(store: Store, now: float) -> None:
    try:
        sessions = os.listdir(store.claims_dir)
    except OSError:
        return
    for session in sessions:
        directory = store.claims_dir / session
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in names:
            path = directory / name
            try:
                if now - path.stat().st_mtime > CLAIM_KEEP:
                    path.unlink()
            except OSError:
                pass
        try:
            directory.rmdir()  # only when empty
        except OSError:
            pass


def score(command: str, words: List[str]) -> int:
    """How many of `words` (junkfather's own arguments) the Bash command holds, in order; -1 when it does not name
    junkfather."""
    if "junkfather" not in command:
        return -1
    position, found = command.find("junkfather"), 0
    for word in words:
        at = command.find(word, position)
        if at >= 0:
            found += 1
            position = at + len(word)
    return found


def find_claim(store: Store, session: str, words: List[str], now: Optional[float] = None
               ) -> Tuple[Optional[Dict[str, Any]], str]:
    """The claim of the Bash call that ran this junkfather, and a note: "" when it is clear, else why the entry
    belongs to the session alone. Only a claim holding every word, not yet used for these words, counts; the one
    taken is marked used for them."""
    now = time.time() if now is None else now
    directory = store.claims_dir / session
    try:
        names = os.listdir(directory)
    except OSError:
        return None, "no claim (is the junkfather PreToolUse hook installed?)"
    claims = []
    for name in names:
        data = read_json(directory / name) if name.endswith(".json") else None
        if isinstance(data, dict) and isinstance(data.get("command"), str) \
                and isinstance(data.get("at"), (int, float)) and 0 <= now - data["at"] <= CLAIM_WINDOW \
                and words not in (data.get("used") or []) and score(data["command"], words) == len(words):
            claims.append(data)
    if not claims:
        return None, "no claim names this command (is the junkfather PreToolUse hook installed?)"
    top = sorted(claims, key=lambda d: d["at"], reverse=True)
    newest = top[0]
    rivals = [d for d in top[1:] if newest["at"] - d["at"] <= CLAIM_AMBIGUOUS and d.get("agent") != newest.get("agent")]
    if rivals:
        return None, "ambiguous: agents of this session ran the same command together"
    newest["used"] = [*(newest.get("used") or []), list(words)]
    try:
        write_atomic(claim_path(store, session, str(newest.get("tool_use_id"))), newest)
    except (OSError, ValueError):
        pass
    return newest, ""


def dumps(owner: Owner) -> str:
    return json.dumps(owner, sort_keys=True)
