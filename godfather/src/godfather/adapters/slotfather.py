"""Slotfather as a source: which contract versions this Godfather reads, and the snapshot decoded into the model
the overview draws (docs/contracts/slotfather.md).

`slotfather status --json --full` carries the dashboard's model as Slotfather builds it (scheduler logic: plan,
admission, reserved start, served class, draining, occupancy); this only turns its JSON back into the Python values
the renderers moved from Slotfather expect: times into aware datetimes, arrays back into tuples. It computes nothing.
"""
from __future__ import annotations

import datetime
from typing import Any, Optional, cast

from ..model import (DASHBOARD_REQUIRED, REQUEST_REQUIRED, Dashboard, Expected, HistoryStep, Request, TreeRow,
                     missing)
from . import settings as _settings

TAG = "slot"
CAPABILITIES_VERSIONS = (1,)  # the capabilities object's `v`
DASHBOARD_VERSIONS = (1,)  # capabilities' `dashboard.v`, and the snapshot's `dashboard.v`
SNAPSHOT_VERSIONS = (2,)  # `status --json` versions whose dashboard block this reads
TIMES = ("since", "granted", "asked_at", "idle_since", "est_start")


class Unsupported(ValueError):
    """A snapshot this Godfather cannot read: an unknown version, or not the documented shape."""


def _object(value: Any) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return cast(list[Any], value) if isinstance(value, list) else []


def unsupported(capabilities: dict[str, Any]) -> Optional[str]:
    """Why a capabilities answer names no contract this Godfather reads, None when it names one."""
    block: Any = capabilities.get("dashboard")
    if capabilities.get("v") not in CAPABILITIES_VERSIONS or not isinstance(block, dict):
        return "no dashboard contract; update slotfather"
    dashboard = _object(block)
    snapshot: Any = dashboard.get("snapshot")
    if dashboard.get("v") not in DASHBOARD_VERSIONS or not isinstance(snapshot, dict) \
            or not _list(_object(snapshot).get("argv")):
        return "no dashboard contract; update slotfather"
    versions: Any = _object(snapshot).get("versions")
    if not isinstance(versions, list) or not any(v in SNAPSHOT_VERSIONS for v in _list(versions)):
        return "needs a newer godfather"
    return None


def settings_supported(capabilities: Optional[dict[str, Any]]) -> bool:
    return _settings.supported(capabilities)


def changelog(capabilities: Optional[dict[str, Any]]) -> Optional[str]:
    path: Any = _object((capabilities or {}).get("dashboard")).get("changelog")
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


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _whole(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _text(value: Any, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _optional_text(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def expected_of(raw: Any) -> Optional[Expected]:
    """A status entry's `expected` (fathers#106) when it is the documented shape: `key`, `p50` and `p90` seconds and
    `runs`, `fallback`; None for null or anything else."""
    value = _object(raw)
    if not _number(value.get("p50")) or not _number(value.get("p90")) or not _whole(value.get("runs")):
        return None
    return {"key": str(value.get("key") or ""), "p50": float(value["p50"]), "p90": float(value["p90"]),
            "runs": int(value["runs"]), "fallback": bool(value.get("fallback"))}


Entries = dict[tuple[Any, Any], list[dict[str, Any]]]


def _entries(snapshot: dict[str, Any]) -> Entries:
    """`status --json`'s entries by the dashboard rows' old key, [pid, since], each key's in the order given: the rows
    take their `lease_id`, `expected` and `overrun` from them (the dashboard block does not carry them)."""
    found: Entries = {}
    for raw in _list(snapshot.get("entries")):
        if isinstance(raw, dict):
            entry = _object(raw)
            found.setdefault((entry.get("pid"), entry.get("since")), []).append(entry)
    return found


def _tree(raw: Any) -> list[TreeRow]:
    rows = _list(raw)
    if not all(isinstance(row, list) and len(_list(row)) == 4 for row in rows):
        raise Unsupported("a request's process tree is not [[pid, cpu, rss, name]]")
    return [tuple(_list(row)) for row in rows]


def _item(raw: Any, entries: Optional[Entries] = None) -> Request:
    """One request, checked once (fathers#142): its required keys present and numbers, the rest defaulted."""
    if not isinstance(raw, dict):
        raise Unsupported("a request is not an object")
    given = _object(raw)
    lacking = missing(given, REQUEST_REQUIRED)
    if lacking is not None:
        raise Unsupported(f"a request has no {lacking}; update slotfather")
    for name in ("count", "pid", "procs", "yields", "rss"):
        if not _number(given[name]):
            raise Unsupported(f"a request's {name} is not a number")
    if given.get("cpu") is not None and not _number(given["cpu"]):
        raise Unsupported("a request's cpu is not a number")
    old: tuple[Any, ...] = tuple(_list(given["key"])) if isinstance(given.get("key"), list) else (
        given["pid"], given.get("since"))
    # The entry's facts in `status --json` (fathers#101, fathers#106): the first not yet taken with this row's old key
    same = (entries or {}).get((old[0], old[1]) if len(old) == 2 else (None, None)) \
        if given["kind"] != "legacy" else None
    entry: dict[str, Any] = same.pop(0) if same else {}
    lease: Any = given.get("lease_id", entry.get("lease_id"))
    lease_id = lease if isinstance(lease, str) and lease else None
    expected = expected_of(given.get("expected", entry.get("expected")))
    start: Any = given.get("start", entry.get("start"))  # the holder's process start, which an action passes back
    item: Request = {
        # Identity (fathers#21, fathers#101): the lease id, else, from an older Slotfather, [pid, since]
        "key": lease_id or old, "kind": str(given["kind"]), "count": int(given["count"]), "pid": int(given["pid"]),
        "procs": int(given["procs"]), "yields": int(given["yields"]), "rss": int(given["rss"]),
        "cpu": float(given["cpu"]) if given.get("cpu") is not None else None, "tree": _tree(given.get("tree")),
        "queue": _text(given.get("queue")), "label": _text(given.get("label")), "cmd": _text(given.get("cmd")),
        "meta": _text(given.get("meta")), "workdir": _text(given.get("workdir")), "asked": _text(given.get("asked")),
        "context": _object(given.get("context")), "priority": _text(given.get("priority"), "normal") or "normal",
        "since": parse_time(given.get("since")), "granted": parse_time(given.get("granted")),
        "asked_at": parse_time(given.get("asked_at")), "idle_since": parse_time(given.get("idle_since")),
        "est_start": parse_time(given.get("est_start")), "lease_id": lease_id, "expected": expected,
        "overrun": given.get("overrun", entry.get("overrun")) is True and expected is not None,
        "start": start if isinstance(start, str) and start else None,
        "style": "text" if given["kind"] == "waiting" else "legacy"}  # holder colours: the app's
    for name in ("ticket", "served", "reprioritized"):
        if name in given:
            item[name] = _optional_text(given[name])
    if _whole(given.get("uid")):
        item["uid"] = int(given["uid"])
    for flag in ("yieldable", "passable", "all"):
        if flag in given:
            item[flag] = given[flag] is True
    return item


def _history(raw: Any) -> list[HistoryStep]:
    history: list[HistoryStep] = []
    for value in _list(raw):
        step = _list(value)
        start, end = parse_time(step[0]), parse_time(step[1])
        if start is None or end is None or not _number(step[2]):
            raise Unsupported("a history step is not [start, end, slots]")
        history.append((start, end, int(step[2])))
    return history


def decode(snapshot: Any) -> Dashboard:
    """The overview's model from one `status --json --full` reply; Unsupported when its version is unknown or it
    is not the documented shape (the last good model then stays on screen, stale). Checked here, once: unknown keys
    are ignored, a missing required one is this notice (fathers#142)."""
    if not isinstance(snapshot, dict):
        raise Unsupported("the snapshot is not a JSON object")
    found = _object(snapshot)
    if found.get("v") not in SNAPSHOT_VERSIONS:
        raise Unsupported(f"status --json version {found.get('v')!r}; needs a newer godfather")
    if not isinstance(found.get("dashboard"), dict):
        raise Unsupported("status --json --full has no dashboard block; update slotfather")
    block = _object(found["dashboard"])
    if block.get("v") not in DASHBOARD_VERSIONS:
        raise Unsupported(f"dashboard block version {block.get('v')!r}; needs a newer godfather")
    lacking = missing(block, DASHBOARD_REQUIRED)
    if lacking is not None:
        raise Unsupported(f"dashboard.{lacking} is missing; update slotfather")
    try:
        entries = _entries(found)
        holders = [_item(raw, entries) for raw in _list(block.get("holders"))]
        waiters = [_item(raw, entries) for raw in _list(block.get("waiters"))]
        history = _history(block.get("history"))
        averages = tuple(float(value) for value in _list(block.get("averages"))) or (0.0, 0.0, 0.0)
    except (TypeError, IndexError, KeyError, AttributeError, ValueError) as error:
        if isinstance(error, Unsupported):
            raise
        raise Unsupported(f"malformed dashboard block ({error})") from None
    if not _number(block["overtake_window"]):
        raise Unsupported("dashboard.overtake_window is not a number of seconds")
    for name in ("slots", "cores", "used", "free"):
        if not _whole(block[name]):
            raise Unsupported(f"dashboard.{name} is not a whole number")
    if block["slots"] < 1 or block["cores"] < 1:
        raise Unsupported("dashboard.slots and dashboard.cores must be at least 1")
    if not isinstance(block.get("basis", {}), dict):
        raise Unsupported("dashboard.basis is not an object")
    for name in ("advice", "memory"):
        if not isinstance(block[name], dict):
            raise Unsupported(f"dashboard.{name} is not an object")
    admission: Any = block.get("admission")
    limit: Any = block.get("limit")
    draining: Any = block.get("draining")
    return {
        "v": int(block["v"]), "directory": _text(block.get("directory")), "slots": int(block["slots"]),
        "cores": int(block["cores"]), "saved": block.get("saved") is True, "basis": _object(block.get("basis")),
        "averages": averages, "draining": int(draining) if _whole(draining) else 0, "advice": _object(block["advice"]),
        "holders": holders, "waiters": waiters, "used": int(block["used"]), "free": int(block["free"]),
        "memory_block": _optional_text(block.get("memory_block")),
        "admission": int(admission) if _whole(admission) else None,
        "order": _text(block.get("order"), "window") or "window", "start": parse_time(block.get("start")),
        "reserved": parse_time(block.get("reserved")), "limit": float(limit) if _number(limit) else 0,
        "memory": _object(block["memory"]), "history": history,
        "events": [],  # EVENTS reads the event logs (fathers#84); the block's described events are not read
        "updated": parse_time(block.get("updated")) or parse_time(found.get("time"))
        or datetime.datetime.now(datetime.timezone.utc),
        "overtake_window": float(block["overtake_window"]), "host": _optional_text(found.get("host"))}


# Settings go through the tools' shared contract (adapters/settings.py); these names stay importable from here
SettingsError, Conflict, ReadOnly, SettingsClient = (_settings.SettingsError, _settings.Conflict, _settings.ReadOnly,
                                                     _settings.SettingsClient)
