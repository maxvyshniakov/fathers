"""The event log: one JSON line per ledger entry, in Godfather's shared event schema, version 1.

Every project's ledger entries also go to one machine-wide log, `~/.local/state/lanefather/events.jsonl`
(`LANEFATHER_EVENTS` names another file), so a dashboard tails one file for every project; `subject.project` and
`subject.state_dir` tell the projects apart. `lanefather capabilities --json` gives the path under
`dashboard.events`.

A line is written after the change it describes, with one `write` on the file opened with `O_APPEND`, mode 0600,
under an `flock` on `events.lock` beside it (a lock held for more than a second drops the line); past 4 MiB the
log becomes `events.jsonl.1` first. A line that cannot be written is dropped: the event log never fails or alters
an action. `ledger.log` stays the record the orchestration metrics read, byte for byte as before (lanefather.journal
writes both).

A project whose state directory is in a temporary directory (a test's) writes no machine-wide event unless
`LANEFATHER_EVENTS` names the log, so test runs of other projects never reach the machine's feed.
"""
from __future__ import annotations

from collections.abc import Mapping
import contextlib
import datetime
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, cast

from fathers_common import events as _common_events

SCHEMA = _common_events.SCHEMA_VERSION
SOURCE = "lanefather"
TAG = "lane"
MAX_LINE = _common_events.MAX_LINE
ROTATE_AT = _common_events.ROTATE_AT
MAX_DEPTH = _common_events.MAX_DEPTH
MAX_STRING = _common_events.MAX_STRING
MAX_COMMAND = _common_events.MAX_COMMAND
LOCK_WAIT = _common_events.LOCK_WAIT

# The kinds Lanefather writes, each at one kind of ledger entry. Godfather's per-source formatters read this list.
KINDS: dict[str, str] = {
    "lane.started": "start or next --start registered a lane",
    "lane.adopted": "adopt, or check, recorded the lane's worktree",
    "claims.added": "check widened the lane's claims to the paths it changes",
    "lane.checked": "check wrote the lane's receipt (warn when red)",
    "landing.reviewed": "land onto dev or main found the lane reviewed",
    "lane.conflict": "land's merge conflicted and was aborted (error)",
    "lane.landed": "land merged the lane",
    "lane.post_merge": "the post-merge smoke finished (error when red)",
    "lane.released": "a green smoke freed the lane while its agent still holds the worktree",
    "lane.done_refused": "done did not run by itself on a finished lane (warn)",
    "lane.unregistered": "start, adopt or a fix lane's hand-over could not register the lane's paths with Spawnfather "
                         "(warn)",
    "lane.kept": "keep, or keep --off, changed whether auto-done leaves the lane",
    "lane.scope_changed": "scope changed a lane's tickets, decisions or specs",
    "lane.done": "done removed the lane's worktree, branches and scratch",
    "lane.dropped": "drop removed a lane that added nothing to its base",
    "lane.parked": "park saved the lane's branch tip on park/<id> and freed its worktree and claims",
    "lane.abandoned": "abandon removed an unlanded lane and freed its worktree and claims",
    "lane.rechecked": "recheck reran a red post-merge smoke on the integration tip (error when red)",
    "process.stopped": "done stopped a process the lane left behind",
    "review.started": "the review started in the background",
    "review.kept": "land kept a review that already ran",
    "review.exempted": "land exempted the lane from review",
    "review.finished": "a review finished or failed (warn when failed)",
    "finding.marked": "review --mark marked one finding",
    "review.marked": "every finding of a review is marked",
    "review.handed_over": "review --fix-lane handed the worktree to the fix lane",
    "review.pre_land_fix": "review --fix-lane opened one pre-land fix round on an active candidate",
    "review.ticket_filed": "the overall review filed its ticket (warn when filing failed)",
    "review.audit_alert": "a post-hoc audit of a none/evidence lane found a P0/P1 finding (warn)",
    "review.recovered": "historical review evidence was recovered from external sources (warn when partial)",
    "gate.finished": "the batch gate finished (error when red)",
    "perf.finished": "the perf suite finished (error when red)",
    "promote.step": "an install or deploy step finished (error on a non-zero exit)",
    "promote.finished": "promote moved dev (and main) (warn when a local branch did not move)",
    "sync.finished": "sync moved the local branches (warn when one failed)",
    "load.waited": "a verify run waited for the load to drop",
    "landing.lock_taken": "a stale landing lock was taken over (warn)",
    "ticket.marked": "the lane's issue was labelled or commented after its land",
    "ticket.skipped": "the lane's issue was not marked: its smoke is not green",
    "ticket.release_commented": "a release commented on the issues it fixes",
    "ticket.failed": "a ticket rule failed; the action went on (warn)",
    "ticket.trailer_conflict": "a lane commit or the merge says Fixes for a partial or umbrella ticket (warn)",
    "queue.updated": "a project policy rewrote the queue",
    "override": "a --force-reason overrode a refusal (warn)",
}

def path() -> Path:
    """The machine-wide log: LANEFATHER_EVENTS, else events.jsonl in Lanefather's default state directory."""
    configured = os.environ.get("LANEFATHER_EVENTS")
    return Path(os.path.expanduser(configured)) if configured else machine_dir() / "events.jsonl"


def machine_dir() -> Path:
    """Lanefather's machine-wide state: the event log and the project registry, beside the default lanes."""
    return Path.home() / ".local" / "state" / "lanefather"


def temporary(directory: Path) -> bool:
    """Whether a project's state directory lives in a temporary directory, as a test's does."""
    try:
        resolved = directory.expanduser().resolve()
    except OSError:
        return False
    roots = {Path(tempfile.gettempdir()).resolve(), Path("/tmp").resolve(), Path("/var/folders").resolve()}
    return any(resolved == root or root in resolved.parents for root in roots)


def host_name() -> str:
    """The machine's short host name, lowercase, as every event and snapshot names it."""
    return _common_events.host_name()


def timestamp(moment: datetime.datetime) -> str:
    """RFC 3339 UTC with milliseconds."""
    return _common_events.timestamp(moment)


# The agent sessions a process can run for, the most specific first: a Codex thread started from a Claude Code
# session inherits CLAUDE_CODE_SESSION_ID, never the other way round (fathers#150 review); Antigravity inside T3 Code
# carries ANTIGRAVITY_CONVERSATION_ID alongside T3 Code's thread id (fathers#200); T3 Code sets T3CODE_THREAD_ID,
# the other T3 names are older guesses kept for callers that set them (fathers#201).
AGENTS = (
    ("ANTIGRAVITY_CONVERSATION_ID", "antigravity"),
    ("ANTIGRAVITY_SESSION_ID", "antigravity"),
    ("CODEX_THREAD_ID", "codex"),
    ("CLAUDE_CODE_SESSION_ID", "claude-code"),
    ("T3CODE_THREAD_ID", "t3-code"),
    ("T3_THREAD_ID", "t3-code"),
    ("T3_CODE_THREAD_ID", "t3-code"),
    ("T3_SESSION_ID", "t3-code"),
)
AGENT_KIND_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def _first_env(env: Mapping[str, str], names: tuple[str, ...]) -> str | None:
    for name in names:
        value = env.get(name, "").strip()
        if value:
            return value
    return None


def agent_session(env: Mapping[str, str] | None = None) -> tuple[str, str] | None:
    """(agent name, session id) of the agent this process runs for, or None."""
    source = os.environ if env is None else env
    explicit_kind = source.get("LANEFATHER_AGENT_KIND", "").strip().lower()
    if explicit_kind and AGENT_KIND_RE.fullmatch(explicit_kind):
        kind_vars = tuple(var for var, name in AGENTS if name == explicit_kind)
        explicit_session = _first_env(source, ("LANEFATHER_AGENT_SESSION", *kind_vars))
        if explicit_session:
            return explicit_kind, explicit_session
    for variable, name in AGENTS:
        session = source.get(variable, "").strip()
        if session:
            return name, session
    return None


def agent_context(explicit_kind: str | None = None, env: Mapping[str, str] | None = None) -> dict[str, Any] | None:
    """Structured metadata for the agent running this command (fathers#200), or None when no agent is known."""
    source = os.environ if env is None else env
    detected = agent_session(source)
    kind = (explicit_kind or "").strip().lower() or source.get("LANEFATHER_AGENT_KIND", "").strip().lower()
    if not kind:
        kind = detected[0] if detected else ""
    if not kind or not AGENT_KIND_RE.fullmatch(kind):
        return None

    kind_session_vars = tuple(var for var, name in AGENTS if name == kind)
    session_id = _first_env(source, ("LANEFATHER_AGENT_SESSION", *kind_session_vars))
    if not session_id and detected and detected[0] == kind:
        session_id = detected[1]

    subagent_vars: list[str] = ["LANEFATHER_AGENT_SUBAGENT_ID"]
    if kind == "claude-code":
        subagent_vars.extend(["CLAUDE_CODE_SUBAGENT_ID", "CLAUDE_AGENT_ID"])
    elif kind == "codex":
        subagent_vars.append("CODEX_SUBAGENT_ID")
    elif kind == "antigravity":
        subagent_vars.append("ANTIGRAVITY_SUBAGENT_ID")
    subagent_id = _first_env(source, tuple(subagent_vars))

    thread_vars: list[str] = ["LANEFATHER_AGENT_THREAD_ID"]
    if kind in ("antigravity", "t3-code"):
        thread_vars.extend(["T3CODE_THREAD_ID", "T3_THREAD_ID", "T3_CODE_THREAD_ID"])
    thread_id = _first_env(source, tuple(thread_vars))

    transcript_vars: list[str] = ["LANEFATHER_AGENT_TRANSCRIPT", "AGENT_TRANSCRIPT_PATH"]
    if kind == "antigravity":
        transcript_vars.append("ANTIGRAVITY_TRANSCRIPT_PATH")
    elif kind == "claude-code":
        transcript_vars.extend(["CLAUDE_CODE_TRANSCRIPT_PATH", "CLAUDE_TRANSCRIPT_PATH"])
    elif kind == "codex":
        transcript_vars.append("CODEX_TRANSCRIPT_PATH")
    transcript_path = _first_env(source, tuple(transcript_vars))
    if not transcript_path and kind == "antigravity" and session_id:
        app_data = _first_env(source, ("ANTIGRAVITY_APP_DATA_DIR",))
        if app_data:
            transcript_path = str(Path(app_data).expanduser() / "brain" / session_id
                                  / ".system_generated" / "logs" / "transcript.jsonl")

    model_vars: list[str] = ["LANEFATHER_AGENT_MODEL"]
    if kind == "antigravity":
        model_vars.extend(["ANTIGRAVITY_MODEL", "T3_MODEL"])
    elif kind == "t3-code":
        model_vars.append("T3_MODEL")
    elif kind == "codex":
        model_vars.append("CODEX_MODEL")
    elif kind == "claude-code":
        model_vars.extend(["CLAUDE_MODEL", "ANTHROPIC_MODEL"])
    model = _first_env(source, tuple(model_vars))

    options: dict[str, Any] = {}
    raw_options = source.get("LANEFATHER_AGENT_OPTIONS", "").strip()
    if raw_options:
        with contextlib.suppress(ValueError):
            parsed = json.loads(raw_options)
            if isinstance(parsed, dict):
                for key, val in cast(dict[Any, Any], parsed).items():
                    if isinstance(key, str) and key and val is not None and val != "":
                        options[clean(key, 1, 64)] = clean(val, 2, 200)
    effort_vars: list[str] = ["LANEFATHER_AGENT_EFFORT"]
    if kind == "claude-code":
        effort_vars.extend(["CLAUDE_CODE_EFFORT_LEVEL", "CLAUDE_EFFORT"])
    elif kind == "codex":
        effort_vars.extend(["CODEX_REASONING_EFFORT", "CODEX_EFFORT"])
    elif kind in ("antigravity", "t3-code"):
        effort_vars.append("T3_EFFORT")
    effort = _first_env(source, tuple(effort_vars))
    if effort:
        options["effort"] = clean(effort, 1, 64)
    thought_vars: list[str] = ["LANEFATHER_AGENT_THOUGHT_LEVEL"]
    if kind == "antigravity":
        thought_vars.extend(["ANTIGRAVITY_THOUGHT_LEVEL", "THOUGHT_LEVEL"])
    thought_level = _first_env(source, tuple(thought_vars))
    if thought_level:
        options["thought_level"] = clean(thought_level, 1, 64)

    info: dict[str, Any] = {"kind": kind}
    if session_id:
        info["session_id"] = clean(session_id, 1, 200)
    if subagent_id:
        info["subagent_id"] = clean(subagent_id, 1, 200)
    if thread_id:
        info["thread_id"] = clean(thread_id, 1, 200)
    if transcript_path:
        info["transcript_path"] = clean(os.path.expanduser(transcript_path), 1, MAX_STRING)
    if model:
        info["model"] = clean(model, 1, 200)
    if options:
        info["options"] = options
    return info


def actor() -> dict[str, Any]:
    """Who causes the event: the agent session this process runs for, else the process. A command Godfather runs
    carries `via: godfather` (GODFATHER_ACTOR is godfather/<version>)."""
    found: dict[str, Any] = {"type": "process", "pid": os.getpid()}
    agent = agent_session()
    if agent:
        found.update(type="agent", session=clean(agent[1], 1, 200), name=agent[0])
    via = os.environ.get("GODFATHER_ACTOR", "").split("/", 1)[0].strip()
    if re.fullmatch(r"[a-z][a-z0-9-]{0,31}", via):
        found["via"] = via
    return found


def clean(value: Any, depth: int = 1, limit: int = MAX_STRING) -> Any:
    """Sanitize `value` for schema v1 via `fathers_common.events.clean`."""
    return _common_events.clean(value, depth, limit)


def command(argv: list[str] | tuple[str, ...]) -> str:
    """A command line for an event: joined and cut to 400 characters."""
    return _common_events.command(argv)


def event(kind: str, subject: dict[str, Any], details: dict[str, Any], level: str = "info",
          moment: datetime.datetime | None = None) -> dict[str, Any]:
    """One event of `kind` about `subject`, caused by this process."""
    return _common_events.build_event(SOURCE, kind, subject, actor(), details, level=level, moment=moment)


def encode(found: dict[str, Any]) -> bytes:
    """The event's line, kept within 16 KiB via `fathers_common.events.encode_event`."""
    return _common_events.encode_event(found)


def hold(handle: Any, wait: float) -> bool:
    return _common_events.hold_lock(handle, wait)


def append(found: dict[str, Any], log: Path | None = None) -> bool:
    """Append one event; False when it could not be written, which never stops the caller."""
    return _common_events.append_event(log or path(), found, rotate_at=ROTATE_AT, lock_wait=LOCK_WAIT)
