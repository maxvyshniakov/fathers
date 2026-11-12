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

import contextlib
import datetime
import fcntl
import json
import math
import os
from pathlib import Path
import re
import socket
import tempfile
import time
from typing import Any, cast

SCHEMA = 1
SOURCE = "lanefather"
TAG = "lane"
MAX_LINE = 16 * 1024
ROTATE_AT = 4 * 2**20
MAX_DEPTH = 4  # details itself is level 1
MAX_STRING = 1000
MAX_COMMAND = 400

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
    "lane.done_refused": "done did not run by itself after landing (warn)",
    "lane.done": "done removed the lane's worktree, branches and scratch",
    "process.stopped": "done stopped a process the lane left behind",
    "review.started": "the review started in the background",
    "review.kept": "land kept a review that already ran",
    "review.exempted": "land exempted the lane from review",
    "review.finished": "a review finished or failed (warn when failed)",
    "finding.marked": "review --mark marked one finding",
    "review.marked": "every finding of a review is marked",
    "review.handed_over": "review --fix-lane handed the worktree to the fix lane",
    "review.ticket_filed": "the overall review filed its ticket (warn when filing failed)",
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
    "queue.updated": "a project policy rewrote the queue",
    "override": "a --force-reason overrode a refusal (warn)",
}

# C0, DEL and C1, and the line and paragraph separators, which text readers split lines on.
_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029]")
LOCK_WAIT = 1.0


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
    name = re.sub(r"[^a-z0-9-]", "-", socket.gethostname().split(".", 1)[0].lower()).strip("-")
    return name[:63].strip("-") or "localhost"


def timestamp(moment: datetime.datetime) -> str:
    """RFC 3339 UTC with milliseconds."""
    utc = moment.astimezone(datetime.UTC)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def actor() -> dict[str, Any]:
    """Who causes the event: the agent session this process runs for, else the process. A command Godfather runs
    carries `via: godfather` (GODFATHER_ACTOR is godfather/<version>)."""
    found: dict[str, Any] = {"type": "process", "pid": os.getpid()}
    for variable, name in (("CLAUDE_CODE_SESSION_ID", "claude-code"), ("CODEX_THREAD_ID", "codex")):
        session = os.environ.get(variable, "").strip()
        if session:
            found.update(type="agent", session=clean(session, 1, 200), name=name)
            break
    via = os.environ.get("GODFATHER_ACTOR", "").split("/", 1)[0].strip()
    if re.fullmatch(r"[a-z][a-z0-9-]{0,31}", via):
        found["via"] = via
    return found


def clean(value: Any, depth: int = 1, limit: int = MAX_STRING) -> Any:
    """A JSON value the schema accepts: strings without control characters and cut to `limit`, finite numbers,
    containers no deeper than the schema allows (a deeper one becomes its JSON text)."""
    if isinstance(value, str):
        text = _CONTROL.sub(" ", value)
        return text if len(text) <= limit else text[:limit - 1] + "…"
    if isinstance(value, bool) or value is None or isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (dict, list, tuple)):
        if depth >= MAX_DEPTH:
            return clean(json.dumps(value, default=str, sort_keys=True), depth, limit)
        if isinstance(value, dict):
            mapping = cast(dict[Any, Any], value)
            return {clean(str(key), depth, 64): clean(item, depth + 1, limit) for key, item in mapping.items()}
        items: list[Any] = list(cast(Any, value))
        return [clean(item, depth + 1, limit) for item in items]
    return clean(str(value), depth, limit)


def command(argv: list[str] | tuple[str, ...]) -> str:
    """A command line for an event: joined and cut to 400 characters."""
    return clean(" ".join(argv), 1, MAX_COMMAND)


def event(kind: str, subject: dict[str, Any], details: dict[str, Any], level: str = "info",
          moment: datetime.datetime | None = None) -> dict[str, Any]:
    """One event of `kind` about `subject`, caused by this process."""
    found: dict[str, Any] = {
        "v": SCHEMA, "time": timestamp(moment or datetime.datetime.now(datetime.UTC)), "host": host_name(),
        "source": SOURCE, "kind": kind,
        "subject": {key: clean(item, MAX_DEPTH) for key, item in subject.items() if item is not None},
        "actor": actor(), "details": clean(details, 1)}
    if level != "info":
        found["level"] = level
    return found


def encode(found: dict[str, Any]) -> bytes:
    """The event's line; a line over 16 KiB keeps the shorter strings, then only the ledger message."""
    for limit in (MAX_STRING, 200):
        line = (json.dumps(dict(found, details=clean(found["details"], 1, limit)), ensure_ascii=False,
                           separators=(",", ":")) + "\n").encode()
        if len(line) <= MAX_LINE:
            return line
    details = {"message": clean(found["details"].get("message", ""), 1, 2000), "truncated": True}
    return (json.dumps(dict(found, details=details), ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def hold(handle: Any, wait: float) -> bool:
    """Take an exclusive flock on `handle` within `wait` seconds; False when another process keeps it."""
    deadline = time.monotonic() + wait
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)


def append(found: dict[str, Any], log: Path | None = None) -> bool:
    """Append one event; False when it could not be written, which never stops the caller."""
    target = log or path()
    with contextlib.suppress(Exception):
        line = encode(found)
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target.with_name("events.lock"), "a") as lock:
            if not hold(lock, LOCK_WAIT):
                return False  # a writer that stopped holding the lock never stalls an action
            with contextlib.suppress(FileNotFoundError):
                if target.stat().st_size + len(line) > ROTATE_AT:
                    target.replace(target.with_name(target.name + ".1"))
            descriptor = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(descriptor, line)
            finally:
                os.close(descriptor)
        return True
    return False
