"""Junkfather's settings, for Godfather's Settings section and anyone else (godfather/SPEC.md, Settings contract):
three tunables of defaults.py that a person may change, kept in `settings.json` in the state directory.

  junkfather settings [--json]                                  the schema and the current values
  junkfather settings check KEY VALUE [--json]                  validate without saving: {"ok", "error", "preview"}
  junkfather settings set KEY VALUE [--revision R] [--json]     save; refused when R no longer matches
  junkfather settings reset (KEY | --all) [--revision R] [--json]

  own.default_ttl   the TTL of `own`, `own-resource` and `adopt` without --ttl
  sweep.interval    the launchd sweep agent's interval (`sweep --plist` without --interval); the loaded agent keeps
                    its interval until it is reloaded (install.py --launchd)
  reap.grace        between SIGTERM and SIGKILL when `reap` has no --grace, for the sweep and the supervisor too

Durations take seconds or 90s, 30m, 2h, 1d. The environment variables that move Junkfather's files are shown as
read-only rows (`env.JUNKFATHER_STATE`, ...). The file is written atomically and durably under the state lock;
`revision` is the SHA-256 of the file as read, and a `set` or `reset` with a stale one changes nothing.

Exit codes: 0 done, 2 invalid, 3 conflict (the reply is the current settings), 5 read-only. 4 is not used: it
means "another user's queue" in Slotfather's settings (fathers#118, owner decision of 2026-11-13).

Every reader calls `value()` when it runs, so a saved value applies to the next command without a restart.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

from . import defaults
from .state import Store, as_dict, read_json, state_dir
from .text import UNITS, duration, iso

VERSION = 1
FILE = "settings.json"
INVALID, CONFLICT, READ_ONLY = 2, 3, 5


class SettingError(Exception):
    def __init__(self, message: str, code: int = INVALID):
        super().__init__(message)
        self.code = code


class Knob(NamedTuple):
    key: str
    section: str
    label: str
    default: float
    least: float
    most: float
    step: float
    whole: bool  # whole seconds only (launchd's StartInterval)
    description: str


KNOBS: Tuple[Knob, ...] = (
    Knob("own.default_ttl", "own", "Default TTL", defaults.TTL, 1, defaults.TTL_MAX, 300, False,
         "How long own, own-resource and adopt keep what they start when the command gives no --ttl."),
    Knob("sweep.interval", "sweep", "Sweep every", defaults.SWEEP_INTERVAL, defaults.SWEEP_INTERVAL_MIN,
         defaults.SWEEP_INTERVAL_MAX, 300, True,
         "The hourly sweep agent's interval; the loaded agent takes a new one when reinstalled "
         "(install.py --launchd)."),
    Knob("reap.grace", "reap", "Grace before SIGKILL", defaults.GRACE, 0, defaults.GRACE_MAX, 1, False,
         "Between SIGTERM and SIGKILL when reap has no --grace; the sweep and the supervisor use it too."),
)
BY_KEY = {knob.key: knob for knob in KNOBS}
SECTIONS = (("own", "Owning"), ("sweep", "Sweep"), ("reap", "Reaping"))
ENVIRONMENT = (("JUNKFATHER_STATE", "State directory", "Where entries, logs, events and these settings live."),
               ("JUNKFATHER_SCRATCH_ROOTS", "Extra scratch roots",
                "Directories (colon-separated) the sweep treats like an agent's scratchpad."),
               ("JUNKFATHER_CLAUDE_SESSIONS", "Claude Code sessions", "Claude Code's live-session registry."),
               ("JUNKFATHER_LANE_RECORDS", "Lane records", "Where the lane of a working directory is looked up."))


def path(root: Optional[Path] = None) -> Path:
    return (root or state_dir()) / FILE


def _raw(root: Optional[Path] = None) -> bytes:
    try:
        return path(root).read_bytes()
    except OSError:
        return b""


def revision(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def number(value: float) -> float:
    """A whole number as an int (3600.0 is shown and saved as 3600)."""
    return int(value) if float(value).is_integer() else value


def parse(knob: Knob, text: str) -> float:
    """The seconds `text` gives for `knob`; SettingError (invalid) with the reason when it gives none."""
    found = re.fullmatch(r"(\d+(?:\.\d+)?)([smhd]?)", str(text).strip())
    if not found:
        raise SettingError(f"{knob.key}: {text!r} is not a duration; give seconds, or 90s, 30m, 2h, 1d")
    seconds = float(found[1]) * UNITS[found[2]]
    if not knob.least <= seconds <= knob.most:
        raise SettingError(f"{knob.key}: {text!r} must be from {duration(knob.least)} to {duration(knob.most)}"
                           if knob.least >= 1 else
                           f"{knob.key}: {text!r} must be from {number(knob.least)} to {number(knob.most)} seconds")
    if knob.whole and not seconds.is_integer():
        raise SettingError(f"{knob.key}: {text!r} must be whole seconds")
    return number(seconds)


def _stored(raw: bytes) -> Dict[str, Any]:
    """The saved mapping as the file holds it (keys this release does not know kept); SettingError (read-only)
    for a file that cannot be read as Junkfather's settings, which is then never overwritten."""
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        raise SettingError(f"{FILE} is not JSON; fix or remove it", READ_ONLY) from None
    found = as_dict(data)
    if type(found.get("v")) is not int:
        raise SettingError(f"{FILE} is not a Junkfather settings file; fix or remove it", READ_ONLY)
    if found["v"] != VERSION:
        raise SettingError(f"{FILE} has version {found['v']}, which this Junkfather does not write: update it",
                           READ_ONLY)
    return dict(as_dict(found.get("settings")))


def saved(root: Optional[Path] = None, raw: Optional[bytes] = None) -> Dict[str, float]:
    """The valid saved values; an unreadable file or value counts as unset."""
    try:
        stored = _stored(_raw(root) if raw is None else raw)
    except SettingError:
        return {}
    found: Dict[str, float] = {}
    for key, item in stored.items():
        knob = BY_KEY.get(key)
        if knob is not None and isinstance(item, (int, float)) and not isinstance(item, bool):
            with contextlib.suppress(SettingError):
                found[key] = parse(knob, str(item))
    return found


def value(key: str, root: Optional[Path] = None) -> float:
    """The value of a setting now: the saved one, else its default. `root` is the state directory (default
    $JUNKFATHER_STATE). Read on every call, at the point of use."""
    return saved(root).get(key, BY_KEY[key].default)


def _writable(root: Path) -> Optional[str]:
    """Why the settings cannot be saved here, or None."""
    try:
        _stored(_raw(root))
    except SettingError as error:
        return f"{path(root)}: {error}"
    folder = root
    while not folder.exists() and folder != folder.parent:
        folder = folder.parent
    return None if os.access(folder, os.W_OK) else f"{folder} is not writable"


def _row(knob: Knob, values: Dict[str, float], rev: str, writable: bool) -> Dict[str, Any]:
    shown = number(values.get(knob.key, knob.default))
    return {"key": knob.key, "label": knob.label, "type": "duration", "value": shown,
            "default": number(knob.default), "min": number(knob.least), "max": number(knob.most),
            "step": number(knob.step), "unit": "s", "source": "saved" if knob.key in values else "default",
            "writable": writable, "revision": rev, "description": knob.description}


def _environment_rows(root: Path) -> List[Dict[str, Any]]:
    from .owner import registry_dir
    shown = {"JUNKFATHER_STATE": str(root), "JUNKFATHER_CLAUDE_SESSIONS": str(registry_dir())}
    rows: List[Dict[str, Any]] = []
    for name, label, description in ENVIRONMENT:
        given = os.environ.get(name)
        rows.append({"key": f"env.{name}", "label": label, "type": "text",
                     "value": given if given else shown.get(name), "default": None,
                     "source": f"env {name}" if given else "default", "writable": False,
                     "description": description + f" Set by {name}; read-only here."})
    return rows


def document(root: Optional[Path] = None) -> Dict[str, Any]:
    root = root or state_dir()
    raw = _raw(root)
    rev = revision(raw)
    values = saved(root, raw)
    reason = _writable(root)
    sections: List[Dict[str, Any]] = [
        {"id": ident, "title": title,
         "settings": [_row(knob, values, rev, reason is None) for knob in KNOBS if knob.section == ident]}
        for ident, title in SECTIONS]
    sections.append({"id": "environment", "title": "Environment", "settings": _environment_rows(root)})
    last = as_dict(read_json(root / "sweep.json"))
    return {"v": VERSION, "tool": "junkfather", "writable": reason is None, "reason": reason, "revision": rev,
            "status": [{"label": "settings", "value": str(path(root))},
                       {"label": "state", "value": str(root)},
                       {"label": "last sweep", "value": iso(last.get("at")) or "never"}],
            "sections": sections}


def preview(knob: Knob, new: float) -> str:
    if knob.key == "own.default_ttl":
        return f"own, own-resource and adopt without --ttl keep what they start for {duration(new)}"
    if knob.key == "sweep.interval":
        return (f"sweep --plist writes an interval of {duration(new)}; the loaded sweep agent takes it when "
                "reinstalled: python3 install.py --launchd")
    return f"a reap without --grace waits {number(new)} s between SIGTERM and SIGKILL"


def knob_for(key: Optional[str]) -> Knob:
    if key and key.startswith("env.") and key[4:] in {name for name, _, _ in ENVIRONMENT}:
        raise SettingError(f"{key} is set in the environment ({key[4:]}), not saved here; it is read-only",
                           READ_ONLY)
    knob = BY_KEY.get(key or "")
    if knob is None:
        raise SettingError(f"unknown setting {key!r}; the settings are {', '.join(BY_KEY)}")
    return knob


def write(store: Store, change: Dict[str, Optional[float]], expected: Optional[str]) -> None:
    """Apply `change` (None removes a key) under the state lock; refused when `expected` is stale."""
    reason = _writable(store.root)
    if reason is not None:
        raise SettingError(reason, READ_ONLY)
    with store.lock():
        raw = _raw(store.root)
        if expected is not None and expected != revision(raw):
            raise SettingError(f"the settings changed since revision {expected}; read them again", CONFLICT)
        values = _stored(raw)
        for key, item in change.items():
            if item is None:
                values.pop(key, None)
            else:
                values[key] = item
        from .state import write_atomic
        write_atomic(path(store.root), {"v": VERSION, "settings": dict(sorted(values.items()))})


def reply(found: Dict[str, Any], as_json: bool, code: int = 0) -> int:
    if as_json:
        print(json.dumps(found, sort_keys=True))
        return code
    if found.get("error"):
        print(f"junkfather settings: {found['error']}", file=sys.stderr)
    if "sections" in found:
        for section in found["sections"]:
            print(section["title"])
            for item in section["settings"]:
                shown = duration(item["value"]) if item["type"] == "duration" else item["value"] or "-"
                lock = "" if item["writable"] else " (read-only)"
                print(f"  {item['key']} = {shown}  [{item['source']}{lock}]")
    elif not found.get("error"):
        print(found.get("preview") or "ok")
    return code


def command(store: Store, action: Optional[str], key: Optional[str], text: Optional[str], everything: bool,
            expected: Optional[str], as_json: bool) -> int:
    """`junkfather settings ...` (cli.py parses, this decides)."""
    try:
        if action is None:
            if key or text or everything or expected:
                raise SettingError("settings takes check KEY VALUE, set KEY VALUE or reset (KEY | --all)")
            return reply(document(store.root), as_json)
        if action == "reset":
            if everything == bool(key) or text is not None:
                raise SettingError("settings reset: give one KEY or --all")
            keys = list(BY_KEY) if everything else [knob_for(key).key]
            write(store, dict.fromkeys(keys), expected)
        else:
            knob = knob_for(key)
            if text is None or everything:
                raise SettingError(f"settings {action} {knob.key}: give one VALUE")
            new = parse(knob, text)
            if action == "check":
                if expected is not None:
                    raise SettingError("settings check takes no --revision")
                if _writable(store.root) is not None:
                    raise SettingError(str(_writable(store.root)), READ_ONLY)
                return reply({"v": VERSION, "ok": True, "error": None, "preview": preview(knob, new)}, as_json)
            write(store, {knob.key: new}, expected)
        return reply({**document(store.root), "ok": True, "error": None}, as_json)
    except SettingError as error:
        if error.code == CONFLICT:
            return reply({**document(store.root), "ok": False, "error": str(error)}, as_json, CONFLICT)
        return reply({"v": VERSION, "ok": False, "error": str(error), "preview": None}, as_json, error.code)
    except OSError as error:  # a file that cannot be written: nothing was saved
        return reply({"v": VERSION, "ok": False, "error": f"settings: {error}", "preview": None}, as_json, READ_ONLY)
