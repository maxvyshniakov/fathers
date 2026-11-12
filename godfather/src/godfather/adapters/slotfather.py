"""Slotfather as a source: which contract versions this Godfather reads, and the snapshot decoded into the model
the overview draws (docs/contracts/slotfather.md).

`slotfather status --json --full` carries the dashboard's model as Slotfather builds it (scheduler logic: plan,
admission, reserved start, served class, draining, occupancy); this only turns its JSON back into the Python values
the renderers moved from Slotfather expect: times into aware datetimes, arrays back into tuples. It computes nothing.
"""
from __future__ import annotations

import datetime
from typing import Any, Optional

TAG = "slot"
CAPABILITIES_VERSIONS = (1,)  # the capabilities object's `v`
DASHBOARD_VERSIONS = (1,)  # capabilities' `dashboard.v`, and the snapshot's `dashboard.v`
SNAPSHOT_VERSIONS = (2,)  # `status --json` versions whose dashboard block this reads
SETTINGS_VERSIONS = (1,)
TIMES = ("since", "granted", "asked_at", "idle_since", "est_start")


class Unsupported(ValueError):
    """A snapshot this Godfather cannot read: an unknown version, or not the documented shape."""


def unsupported(capabilities: dict[str, Any]) -> Optional[str]:
    """Why a capabilities answer names no contract this Godfather reads, None when it names one."""
    block = capabilities.get("dashboard")
    if capabilities.get("v") not in CAPABILITIES_VERSIONS or not isinstance(block, dict):
        return "no dashboard contract; update slotfather"
    snapshot = block.get("snapshot")
    if block.get("v") not in DASHBOARD_VERSIONS or not isinstance(snapshot, dict) \
            or not isinstance(snapshot.get("argv"), list) or not snapshot["argv"]:
        return "no dashboard contract; update slotfather"
    versions = snapshot.get("versions")
    if not isinstance(versions, list) or not any(v in SNAPSHOT_VERSIONS for v in versions):
        return "needs a newer godfather"
    return None


def settings_supported(capabilities: Optional[dict[str, Any]]) -> bool:
    block = (capabilities or {}).get("dashboard") or {}
    versions = (block.get("settings") or {}).get("versions") if isinstance(block, dict) else None
    return isinstance(versions, list) and any(v in SETTINGS_VERSIONS for v in versions)


def changelog(capabilities: Optional[dict[str, Any]]) -> Optional[str]:
    block = (capabilities or {}).get("dashboard") or {}
    path = block.get("changelog") if isinstance(block, dict) else None
    return path if isinstance(path, str) and path.startswith("/") else None


def parse_time(value: Any) -> Optional[datetime.datetime]:
    """An RFC 3339 time (`2026-11-12T04:25:56Z`, with or without fractions, `Z` or an offset) as an aware UTC
    datetime; None for null or anything else."""
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        moment = datetime.datetime.fromisoformat(text)
    except ValueError:
        # Python 3.9's fromisoformat takes 3 or 6 fraction digits only
        head, dot, rest = text.partition(".")
        if not dot:
            return None
        digits = rest[:len(rest) - len(rest.lstrip("0123456789"))]
        try:
            moment = datetime.datetime.fromisoformat(f"{head}.{(digits + '000000')[:6]}{rest[len(digits):]}")
        except ValueError:
            return None
    if moment.tzinfo is None:
        return None
    return moment.astimezone(datetime.timezone.utc)


def _item(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise Unsupported("a request is not an object")
    item = dict(raw)
    for key in TIMES:
        item[key] = parse_time(raw.get(key))
    key = raw.get("key")
    item["key"] = tuple(key) if isinstance(key, list) else (raw.get("pid"), raw.get("since"))
    tree = raw.get("tree") or []
    if not isinstance(tree, list) or not all(isinstance(row, list) and len(row) == 4 for row in tree):
        raise Unsupported("a request's process tree is not [[pid, cpu, rss, name]]")
    item["tree"] = [tuple(row) for row in tree]
    for name in ("cpu", "rss", "procs", "count", "yields"):
        if not isinstance(item.get(name, 0), (int, float, type(None))):
            raise Unsupported(f"a request's {name} is not a number")
    item.setdefault("context", {})
    item.setdefault("meta", "")
    item.setdefault("cmd", "")
    item.setdefault("queue", "")
    item.setdefault("label", "")
    item.setdefault("asked", "")
    item["style"] = "text" if item.get("kind") == "waiting" else "legacy"  # holder colours: the app's
    return item


def _event(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise Unsupported("an event is not an object")
    event = dict(raw)
    event["when"] = parse_time(raw.get("when"))
    if event["when"] is None:
        raise Unsupported("an event has no time")
    for name in ("count", "pid", "delta"):
        event.setdefault(name, 0)
    event.setdefault("label", "")
    return event


def decode(snapshot: Any) -> dict[str, Any]:
    """The overview's model from one `status --json --full` reply; Unsupported when its version is unknown or it
    is not the documented shape (the last good model then stays on screen, stale)."""
    if not isinstance(snapshot, dict):
        raise Unsupported("the snapshot is not a JSON object")
    if snapshot.get("v") not in SNAPSHOT_VERSIONS:
        raise Unsupported(f"status --json version {snapshot.get('v')!r}; needs a newer godfather")
    block = snapshot.get("dashboard")
    if not isinstance(block, dict):
        raise Unsupported("status --json --full has no dashboard block; update slotfather")
    if block.get("v") not in DASHBOARD_VERSIONS:
        raise Unsupported(f"dashboard block version {block.get('v')!r}; needs a newer godfather")
    model = dict(block)
    try:
        model["holders"] = [_item(raw) for raw in block.get("holders") or []]
        model["waiters"] = [_item(raw) for raw in block.get("waiters") or []]
        model["events"] = [_event(raw) for raw in block.get("events") or []]
        history = []
        for step in block.get("history") or []:
            start, end = parse_time(step[0]), parse_time(step[1])
            if start is None or end is None or not isinstance(step[2], (int, float)):
                raise Unsupported("a history step is not [start, end, slots]")
            history.append((start, end, int(step[2])))
        model["history"] = history
        for name in ("start", "reserved"):
            model[name] = parse_time(block.get(name))
        model["updated"] = parse_time(block.get("updated")) or parse_time(snapshot.get("time")) \
            or datetime.datetime.now(datetime.timezone.utc)
        averages = block.get("averages")
        model["averages"] = tuple(float(value) for value in averages) if isinstance(averages, list) else (0.0, 0.0, 0.0)
        if not isinstance(model.get("overtake_window"), (int, float)) or isinstance(model["overtake_window"], bool):
            raise Unsupported("dashboard.overtake_window is not a number of seconds")
        for name in ("slots", "cores", "used", "free"):
            if not isinstance(model.get(name), int) or isinstance(model.get(name), bool):
                raise Unsupported(f"dashboard.{name} is not a whole number")
        if model["slots"] < 1 or model["cores"] < 1:
            raise Unsupported("dashboard.slots and dashboard.cores must be at least 1")
        if not isinstance(model.setdefault("basis", {}), dict):
            raise Unsupported("dashboard.basis is not an object")
        for name in ("advice", "memory"):
            if not isinstance(model.get(name), dict):
                raise Unsupported(f"dashboard.{name} is not an object")
    except (TypeError, IndexError, KeyError, AttributeError, ValueError) as error:
        if isinstance(error, Unsupported):
            raise
        raise Unsupported(f"malformed dashboard block ({error})") from None
    model.setdefault("draining", 0)
    model.setdefault("order", "window")
    model.setdefault("limit", 0)
    model.setdefault("admission", None)
    model.setdefault("memory_block", None)
    model.setdefault("saved", False)
    model.setdefault("directory", "")
    model["host"] = snapshot.get("host")
    return model


class SettingsError(Exception):
    """A settings call that failed or was refused: the message is the tool's own, shown in red."""


class Conflict(SettingsError):
    """`settings set` with a revision that no longer matches (exit 3); `reply` is the current settings."""

    def __init__(self, reply: Optional[dict[str, Any]]) -> None:
        super().__init__("changed elsewhere")
        self.reply = reply


class SettingsClient:
    """`slotfather settings` (SF-2): read, check, set and reset, each a short CLI call with the tool's exit codes:
    0 done, 2 invalid, 3 conflict, 4 read-only."""

    TIMEOUT = 5.0

    def __init__(self, executable: Optional[str] = None) -> None:
        self.executable = executable

    def call(self, argv: list[str]) -> tuple[int, Any]:
        from .. import sources  # here, so the adapter stays importable without the I/O module's state

        code, value, problem = sources.run_json("slotfather", ["settings", *argv, "--json"], self.TIMEOUT,
                                                self.executable)
        if value is None and code in (0, 3):
            raise SettingsError(problem or f"slotfather settings {' '.join(argv)} printed no JSON")
        if code not in (0, 2, 3, 4):
            raise SettingsError(problem or self.error(value, f"slotfather settings exited {code}"))
        return code, value

    def read(self) -> dict[str, Any]:
        code, value = self.call([])
        if code != 0 or not isinstance(value, dict):
            raise SettingsError(self.error(value, "slotfather settings cannot be read"))
        if value.get("v") not in SETTINGS_VERSIONS:
            raise SettingsError(f"slotfather settings version {value.get('v')!r}; needs a newer godfather")
        return value

    def check(self, key: str, value: str) -> dict[str, Any]:
        """{"ok", "error", "preview", "slots"?}; a refusal (exit 2) is an answer, not an error."""
        code, reply = self.call(["check", key, value])
        if not isinstance(reply, dict):
            return {"ok": False, "error": f"slotfather settings check exited {code}", "preview": None}
        return reply

    def changed(self, code: int, value: Any) -> dict[str, Any]:
        if code == 3:
            raise Conflict(value if isinstance(value, dict) and value.get("sections") else None)
        if code != 0 or not isinstance(value, dict):
            raise SettingsError(self.error(value, "not saved"))
        return value

    def set(self, key: str, value: str, revision: Optional[str] = None) -> dict[str, Any]:
        """Saves one value; the settings as they are now. Raises Conflict or SettingsError."""
        return self.changed(*self.call(["set", key, value, *(["--revision", revision] if revision else [])]))

    def reset(self, key: Optional[str], revision: Optional[str] = None) -> dict[str, Any]:
        """Resets one key, or every setting (None)."""
        target = [key] if key else ["--all"]
        return self.changed(*self.call(["reset", *target, *(["--revision", revision] if revision else [])]))

    @staticmethod
    def error(value: Any, fallback: str) -> str:
        found = value.get("error") if isinstance(value, dict) else None
        return str(found) if found else fallback
