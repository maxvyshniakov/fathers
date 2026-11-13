"""Machine-local settings: the knobs that belong to this machine rather than to a project, kept in a file Lanefather
owns, `~/.config/lanefather/settings.json` (`LANEFATHER_SETTINGS` names another), and the `settings` command
Godfather's Settings section reads and writes them through.

  lanefather settings [--json]                                  the schema and the current values
  lanefather settings check KEY VALUE [--json]                  validate without saving; a preview
  lanefather settings set KEY VALUE [--revision R] [--json]     save; refused when R no longer matches
  lanefather settings reset (KEY | --all) [--revision R] [--json]

Exit codes: 0 done, 2 invalid, 3 conflict (a stale --revision: the reply is the current settings), 4 read-only.

The knobs, each below the environment, which wins and makes its row read-only:
  review.start               auto | manual: whether land starts the review in the background (LANEFATHER_REVIEW_START)
  rerun.wait                 seconds a red check's rerun waits for its lease (LANEFATHER_RERUN_WAIT)
  slots.priority.<phase>     project | interactive | normal | batch: this machine's slot priority of a phase, above
                             the project's `[slots.priority]` (LANEFATHER_PRIORITY_<PHASE>, LANEFATHER_PRIORITY)
A project's `[slots.priority]` values are shown as read-only rows of the project the command runs in.
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, cast

from . import config, events, registry, state
from .errors import LaneError

VERSION = 1
CONFLICT, INVALID, READ_ONLY = 3, 2, 4
DEFAULT_RERUN_WAIT = 3300  # checks.DEFAULT_RERUN_WAIT reads it
MAX_WAIT = 86400


class SettingError(Exception):
    def __init__(self, message: str, code: int = INVALID):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Knob:
    key: str
    label: str
    type: str
    default: Any
    variables: tuple[str, ...]  # the settings (config.names) of the environment, first wins
    description: str
    choices: tuple[tuple[str, str], ...] = ()


def priority_knob(phase: str) -> Knob:
    setting = "PRIORITY_" + phase.upper().replace("-", "_")
    return Knob(f"slots.priority.{phase}", f"{phase} priority", "choice", "project", (setting, "PRIORITY"),
                f"The Slotfather priority of this machine's {phase} runs; project follows the project's "
                "[slots.priority] and the built-in.",
                (("project", "Project"), ("interactive", "Interactive"), ("normal", "Normal"), ("batch", "Batch")))


KNOBS: tuple[Knob, ...] = (
    Knob("review.start", "Review start", "choice", "auto", ("REVIEW_START",),
         "Whether land starts the lane's review in the background; manual waits for lanefather review ID.",
         (("auto", "Automatic"), ("manual", "Manual"))),
    Knob("rerun.wait", "Rerun waits", "duration", DEFAULT_RERUN_WAIT, ("RERUN_WAIT",),
         "How long a red check's rerun waits for its slot lease before its checks stay unclassified."),
    *(priority_knob(phase) for phase in config.SLOT_PHASES),
)
BY_KEY = {knob.key: knob for knob in KNOBS}


def path() -> Path:
    configured = os.environ.get("LANEFATHER_SETTINGS")
    if configured:
        return Path(os.path.expanduser(configured))
    return Path.home() / ".config" / "lanefather" / "settings.json"


def _raw() -> bytes:
    try:
        return path().read_bytes()
    except OSError:
        return b""


def revision(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def saved(raw: bytes | None = None) -> dict[str, Any]:
    """The saved values this Lanefather knows; an unreadable file or value counts as unset."""
    try:
        data = json.loads(raw if raw is not None else _raw() or b"{}")
    except ValueError:
        return {}
    if not isinstance(data, dict) or cast(dict[str, Any], data).get("v") != VERSION:
        return {}
    values = cast(dict[str, Any], data).get("settings")
    if not isinstance(values, dict):
        return {}
    found: dict[str, Any] = {}
    for key, value in cast(dict[str, Any], values).items():
        knob = BY_KEY.get(key)
        with contextlib.suppress(SettingError):
            if knob is not None:
                found[key] = parse(knob, str(value) if not isinstance(value, str) else value)
    return found


def value(key: str) -> Any:
    """The saved value of a knob, or None when this machine leaves it at its default."""
    return saved().get(key)


def parse(knob: Knob, text: str) -> Any:
    """The value of `text` for `knob`, as the command line and the settings file give it; SettingError if invalid."""
    text = text.strip()
    if knob.type == "choice":
        if text not in {choice for choice, _ in knob.choices}:
            raise SettingError(f"{knob.key}: {text!r} is not one of {', '.join(c for c, _ in knob.choices)}")
        return text
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([smh]?)", text)
    if not match:
        raise SettingError(f"{knob.key}: {text!r} is not a duration; give seconds, or a number with s, m or h")
    seconds = float(match[1]) * {"": 1, "s": 1, "m": 60, "h": 3600}[match[2]]
    if not 0 < seconds <= MAX_WAIT:
        raise SettingError(f"{knob.key}: {text!r} must be more than 0 and at most {MAX_WAIT} seconds")
    return int(seconds) if seconds == int(seconds) else seconds


def environment(knob: Knob) -> tuple[str, str] | None:
    """The variable that sets the knob in this environment and its value; an empty variable is unset."""
    for setting in knob.variables:
        try:
            found = config.current().lookup(setting)
        except LaneError:
            # The project here is unreadable (or two names of the setting differ): the machine settings stay
            # readable, with the environment's own LANEFATHER_ name.
            value = os.environ.get(f"LANEFATHER_{setting}")
            found = (f"LANEFATHER_{setting}", value) if value else None
        if found and found[1]:
            return found
    return None


def row(knob: Knob, values: dict[str, Any], rev: str) -> dict[str, Any]:
    found: dict[str, Any] = {"key": knob.key, "label": knob.label, "type": knob.type,
                             "value": values.get(knob.key, knob.default), "default": knob.default,
                             "source": "saved" if knob.key in values else "default", "writable": True,
                             "revision": rev, "description": knob.description}
    if knob.choices:
        found["choices"] = [{"value": choice, "label": label} for choice, label in knob.choices]
    if knob.type == "duration":
        found.update(min=1, max=MAX_WAIT, step=300, unit="s")
    env = environment(knob)
    if env:
        found.update(source=f"env {env[0]}", writable=False)
        try:
            found["value"] = parse(knob, env[1])
        except SettingError as error:
            found.update(value=env[1], error=str(error))
    return found


def project_rows() -> list[dict[str, Any]]:
    """The project's own slot priorities, read-only: they live in its tracked .lanefather.toml."""
    project = config.current()
    return [{"key": f"project.slots.priority.{key}", "label": f"{key} priority", "type": "choice", "value": priority,
             "default": None, "source": f"{config.CONFIG_NAME} [slots.priority] {key}", "writable": False,
             "description": f"{project.path}"} for key, priority in project.slot_priority]


def document() -> dict[str, Any]:
    raw = _raw()
    rev = revision(raw)
    values = saved(raw)
    sections: list[dict[str, Any]] = [
        {"id": "review", "title": "Review", "settings": [row(BY_KEY["review.start"], values, rev),
                                                         row(BY_KEY["rerun.wait"], values, rev)]},
        {"id": "slots", "title": "Slot priorities",
         "settings": [row(BY_KEY[f"slots.priority.{phase}"], values, rev) for phase in config.SLOT_PHASES]}]
    with contextlib.suppress(Exception):
        rows = project_rows()
        if rows:
            name = config.current().project
            sections.append({"id": "project", "title": f"Project {name}", "settings": rows})
    reason = writable()
    return {"v": VERSION, "tool": "lanefather", "writable": reason is None, "reason": reason, "revision": rev,
            "status": [{"label": "settings", "value": str(path())},
                       {"label": "events", "value": str(events.path())},
                       {"label": "projects", "value": f"{len(registry.read())} registered in {registry.path()}"}],
            "sections": sections}


def preview(knob: Knob, new: Any) -> str | None:
    if knob.key == "review.start":
        return ("land starts each lane's review in the background" if new == "auto"
                else "land starts no review; run lanefather review ID")
    if knob.key == "rerun.wait":
        return f"a rerun waits up to {new} s for its lease"
    phase = knob.key.removeprefix("slots.priority.")
    if new == "project":
        return f"{phase} runs follow the project's [slots.priority] and the built-in"
    return f"{phase} runs ask Slotfather at {new} in every project, unless the environment sets it"


def knob_for(key: str | None) -> Knob:
    knob = BY_KEY.get(key or "")
    if knob is None:
        raise SettingError(f"unknown setting {key!r}; the settings are {', '.join(BY_KEY)}")
    if environment(knob):
        env = environment(knob)
        assert env is not None
        raise SettingError(f"{knob.key} is set by {env[0]} in the environment; unset it to change the saved value",
                           READ_ONLY)
    return knob


def write(change: dict[str, Any | None], expected: str | None) -> None:
    """Apply `change` (None removes a key) under the settings lock; refused when `expected` is stale."""
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target.with_name("settings.lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        raw = _raw()
        if expected is not None and expected != revision(raw):
            raise SettingError(f"the settings changed since revision {expected}; read them again", CONFLICT)
        values = stored(raw)
        for key, item in change.items():
            if item is None:
                values.pop(key, None)
            else:
                values[key] = item
        state.atomic_write(target, json.dumps({"v": VERSION, "settings": dict(sorted(values.items()))}, indent=2)
                           + "\n", mode=0o600)


def stored(raw: bytes) -> dict[str, Any]:
    """The saved mapping as the file holds it, keys this release does not know included, so a write by an older
    Lanefather keeps a newer one's settings. A file of a newer version is never rewritten."""
    if not raw.strip():
        return {}
    try:
        data: Any = json.loads(raw)
    except ValueError:
        raise SettingError(f"{path()} is not JSON; fix or remove it", READ_ONLY) from None
    if not isinstance(data, dict) or type(cast(dict[str, Any], data).get("v")) is not int:
        raise SettingError(f"{path()} is not a Lanefather settings file; fix or remove it", READ_ONLY)
    found = cast(dict[str, Any], data)
    if found["v"] != VERSION:
        raise SettingError(f"{path()} has version {found['v']}, which this Lanefather does not write: update it",
                           READ_ONLY)
    values = found.get("settings", {})
    return dict(cast(dict[str, Any], values)) if isinstance(values, dict) else {}


def writable() -> str | None:
    """Why the settings cannot be saved here, or None."""
    target = path()
    try:
        stored(_raw())
    except SettingError as error:
        return str(error)
    folder = target.parent
    while not folder.exists() and folder != folder.parent:
        folder = folder.parent
    return None if os.access(folder, os.W_OK) else f"{folder} is not writable"


def reply(found: dict[str, Any], as_json: bool, code: int = 0) -> int:
    if as_json:
        print(json.dumps(found))
    elif "sections" in found:
        if found.get("error"):
            print(f"lanefather: {found['error']}", file=sys.stderr)
        for section in found["sections"]:
            print(section["title"])
            for item in section["settings"]:
                lock = "" if item["writable"] else " (read-only)"
                print(f"  {item['key']} = {item['value']}  [{item['source']}{lock}]")
    elif found.get("error"):
        print(f"lanefather: {found['error']}", file=sys.stderr)
    else:
        print(found.get("preview") or "ok")
    return code


def settings(args: Any) -> int:
    try:
        if args.action is None:
            return reply(document(), args.json)
        if args.action == "check":
            knob = knob_for(args.key)
            if args.value is None:
                raise SettingError(f"settings check {knob.key}: give the value")
            new = parse(knob, args.value)
            return reply({"v": VERSION, "ok": True, "error": None, "preview": preview(knob, new)}, args.json)
        if args.action == "set":
            knob = knob_for(args.key)
            if args.value is None:
                raise SettingError(f"settings set {knob.key}: give the value")
            new = parse(knob, args.value)
            write({knob.key: None if new == knob.default else new}, args.revision)
        else:
            if args.all == bool(args.key):
                raise SettingError("settings reset: give one KEY or --all")
            keys = list(BY_KEY) if args.all else [knob_for(args.key).key]
            write(dict.fromkeys(keys), args.revision)
        return reply({**document(), "ok": True, "error": None}, args.json)
    except SettingError as error:
        if error.code == CONFLICT:
            return reply({**document(), "ok": False, "error": str(error)}, args.json, CONFLICT)
        return reply({"v": VERSION, "ok": False, "error": str(error), "preview": None}, args.json, error.code)
    except (OSError, LaneError) as error:
        # A file that cannot be written, or a project here that cannot be read: nothing was saved.
        return reply({"v": VERSION, "ok": False, "error": f"settings: {error}", "preview": None}, args.json, READ_ONLY)
