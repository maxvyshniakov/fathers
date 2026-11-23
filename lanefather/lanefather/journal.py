"""The ledger: one line per action and override in <state>/ledger.log, which orchestration metrics read; the same
entry as an event in the machine-wide event log (lanefather.events); and the run logs beside the ledger."""
from __future__ import annotations

import datetime
import os
from pathlib import Path
import subprocess
from typing import Any

from . import config, events, state
from .gitops import main_checkout

LEDGER_TIME = "%Y-%m-%dT%H:%M:%SZ"


def now() -> str:
    return datetime.datetime.now(datetime.UTC).strftime(LEDGER_TIME)


def parse_time(text: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))


def project_name(project: config.Config | None = None) -> str:
    """The project as events and status name it: its `project` key, else its repository's directory name (the
    main checkout's, for a project without a configuration file)."""
    cfg = project or config.DEFAULT
    if cfg.project != config.DEFAULT.project:
        return cfg.project
    if cfg.path is not None:
        return cfg.path.parent.name
    common = git_output(Path.cwd(), "rev-parse", "--path-format=absolute", "--git-common-dir")
    return Path(main_checkout(common)).name if common else cfg.project


def git_output(cwd: Path, *args: str) -> str:
    """git's output in `cwd`, empty when it fails."""
    try:
        found = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return found.stdout.strip() if found.returncode == 0 else ""


def lane_subject(lane: state.Record | str, directory: Path) -> dict[str, Any]:
    """The subject of an event about a lane: a record, or a lane id whose record supplies its ticket."""
    record: state.Record | None = lane if isinstance(lane, dict) else None
    lane_id = str(lane["id"]) if isinstance(lane, dict) else lane
    if record is None and state.ID.match(lane_id):
        record = state.read_json(directory / f"{lane_id}.json")
    ticket = str((record or {}).get("ticket", "none")).lstrip("#")
    return {"type": "lane", "id": lane_id, **({"ticket": ticket} if ticket and ticket != "none" else {})}


def override(command: str, record: state.Record | None, reason: str | None, refusals: list[str], *,
             subject: dict[str, Any] | None = None, qualifier: str = "", described: str | None = None,
             details: dict[str, Any] | None = None, audited: bool = True, store: state.Store | None = None,
             directory: Path | None = None, project: config.Config | None = None) -> dict[str, Any]:
    """Audit one `--force-reason`: append it to the lane record's `overrides` (not when `audited` is false, as
    for `done` and a repeated `review`; nor without a record: `promote` and `findings` override a commit's gate, named
    by `subject`) and write the warn ledger line of kind `override`, `override <command> <id>[ <qualifier>]: <reason>
    (<described>, by default the refusals joined by '; ')`. Returns the override entry."""
    entry: dict[str, Any] = {"at": now(), "reason": reason, "refusals": refusals}
    if record is not None and audited:
        record.setdefault("overrides", []).append(entry)
    about = str(record["id"] if record is not None else (subject or {}).get("id"))
    target = f"{about} {qualifier}" if qualifier else about
    ledger(f"override {command} {target}: {reason} ({'; '.join(refusals) if described is None else described})",
           kind="override", level="warn", lane=record, subject=None if record is not None else subject,
           details={"command": command, "reason": reason, "refusals": refusals, **(details or {})},
           store=store, directory=directory, project=project)
    return entry


def ledger(message: str, *, kind: str, lane: state.Record | str | None = None,
           subject: dict[str, Any] | None = None, details: dict[str, Any] | None = None, level: str = "info",
           store: state.Store | None = None, directory: Path | None = None,
           project: config.Config | None = None) -> None:
    """Write one ledger line, exactly as before, then the event of `kind` that mirrors it.

    The ledger line is `<time> <message>`. The event carries the same moment with milliseconds, the message as
    `details.message`, its subject (the lane's, else `subject`, else the project's) with the project and state
    directory, and `level`. It never fails the caller (lanefather.events)."""
    moment = datetime.datetime.now(datetime.UTC)
    cfg = store.project if store is not None else (project or config.DEFAULT)
    where = store.directory if store is not None else (directory or state.Store(cfg).directory)
    where.mkdir(parents=True, exist_ok=True)
    with open(where / "ledger.log", "a", encoding="utf-8") as handle:
        handle.write(f"{moment.strftime(LEDGER_TIME)} {message}\n")
    try:
        if events.temporary(where) and not os.environ.get("LANEFATHER_EVENTS"):
            return
        about = dict(subject if subject is not None else lane_subject(lane, where) if lane is not None
                     else {"type": "project", "id": str(where)})
        about.setdefault("project", project_name(cfg))
        about.setdefault("state_dir", str(where))
        events.append(events.event(kind, about, {"message": message, **(details or {})}, level, moment))
    except Exception:
        return
