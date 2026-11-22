"""The shared queue settings (fathers#34): capacity.json, order.json and sweep.json beside the queue, read,
validated, tightened by a client's environment and saved in one place.

- Capacity: capacity_basis() (the slots and how they were found), capacity_text(), capacity_change() (what
  `slotfather slots` saves for a mode, the one computation `slots` and `settings` share) and total().
- Order: order_settings() (shared, or tightened by this client's environment), checked_order(), order_bytes().
- Sweep: sweep_settings() and checked_sweep().
- configure(): the only write path for the three files, under the queue lock, with the ownership check, the
  per-file conflict check and rollback. What a change starts (a grant) is the caller's callback; the change events
  are emitted here. `slotfather settings` is settings_cli.py.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Callable, Mapping, cast

from . import config as _config
from . import events as _events
from . import platform as _platform
from . import state as _state

FILES = ("capacity.json", "order.json", "sweep.json")


# Capacity

def memory_slots(gib: float | None, per_slot: float) -> int | None:
    """One slot per `per_slot` GiB of `gib`, at least one; None when the memory is unknown. The memory counts in whole
    GiB, rounded: Linux's MemTotal is a little under the size a VM was given (11.7 for 12)."""
    if gib is None:
        return None
    return max(1, math.floor(round(gib) / per_slot + 1e-9))


def capacity_basis() -> dict[str, Any]:
    """The capacity and how it was found: {"slots", "saved", "mode", "cores", "cpu_slots", "memory_gib",
    "gib_per_slot", "memory_slots"}. Mode "fixed" is a saved count, "memory" one slot per gib_per_slot GiB, "lower"
    the lower of the CPU and memory budgets, "automatic" the lower at AUTO_GIB_PER_SLOT; atomic publication keeps
    readers consistent."""
    path = _config.state_dir() / "capacity.json"
    machine = _config.cores()
    cpu = max(1, machine // _config.CORES_PER_SLOT)
    found: dict[str, Any] = {"saved": True, "cores": machine, "cpu_slots": cpu, "memory_gib": None,
                             "gib_per_slot": None, "memory_slots": None, "cgroup": None}
    try:
        data: Any = json.loads(path.read_text())
        value = data["slots"]
    except FileNotFoundError:
        data, value = {"mode": "automatic", "gib_per_slot": _config.AUTO_GIB_PER_SLOT}, None
        found["saved"] = False
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise _config.SlotError(f"cannot read capacity from {path}: {error}") from None
    invalid = _config.SlotError(f"invalid saved capacity in {path}; use slotfather slots reset")
    if value is not None and (type(value) is not int or value < 1):
        raise invalid
    mode = data.get("mode", "fixed")
    if mode == "fixed":
        return {**found, "slots": value, "mode": mode}
    per_slot = data.get("gib_per_slot")
    if mode not in _config.CAPACITY_MODES + ("automatic",) or type(per_slot) not in (int, float) or not per_slot > 0:
        raise invalid
    group = data.get("cgroup") if isinstance(data.get("cgroup"), str) else None
    gib = _platform.memory_gib(group)
    by_memory = memory_slots(gib, per_slot)
    found.update(mode=mode, gib_per_slot=per_slot, memory_slots=by_memory,
                 memory_gib=None if gib is None else round(gib, 1), cgroup=group or _platform.memory_cgroup())
    if by_memory is None:  # memory unknown: the count saved with the mode, or the CPU budget
        slots = value or cpu
    else:
        slots = by_memory if mode == "memory" else min(cpu, by_memory)
    return {**found, "slots": slots}


def automatic_capacity() -> int:
    """The capacity with nothing saved: the lower of the CPU budget and one slot per AUTO_GIB_PER_SLOT GiB."""
    by_memory = memory_slots(_platform.memory_gib(), _config.AUTO_GIB_PER_SLOT)
    cpu = max(1, _config.cores() // _config.CORES_PER_SLOT)
    return cpu if by_memory is None else min(cpu, by_memory)


def capacity() -> tuple[int, bool]:
    """The shared saved capacity, or the automatic default: (slots, saved)."""
    found = capacity_basis()
    return found["slots"], found["saved"]


def total() -> int:
    return capacity()[0]


def capacity_text(found: Mapping[str, Any]) -> str:
    """`slotfather slots`' line: "N slots (saved)", "N slots (automatic)", with the budgets where memory counts."""
    slots, mode = found["slots"], found["mode"]
    if mode == "fixed":
        return f"{slots} slots (saved)"
    memory = f"{found['memory_gib']:g} GiB" if found["memory_gib"] is not None else "unknown memory"
    if found.get("cgroup") and found["memory_gib"] is not None and not _config.setting("MEMORY_GIB"):
        memory += f" in {found['cgroup']}"
    per = f"one per {found['gib_per_slot']:g} GiB of {memory}"
    cpu = f"{found['cpu_slots']} by {found['cores']} cores"
    if mode == "automatic":
        if found["memory_slots"] is None or found["memory_slots"] >= found["cpu_slots"]:
            return f"{slots} slots (automatic)"
        return f"{slots} slots (automatic, by memory: {per}; {cpu})"
    if mode == "memory":
        return f"{slots} slots (saved: {per})"
    return f"{slots} slots (saved: the lower of {cpu} and {found['memory_slots'] or '?'} by memory, {per})"


NO_CGROUP = ("slotfather: note: no cgroup with a memory limit here or in SLOTFATHER_CGROUP: the count follows MemTotal, "
             "which a VM's balloon moves and which differs from what clients inside a limited cgroup see; name the "
             "cgroup the leases share (SLOTFATHER_CGROUP=/a.slice/b.slice, or install.py --cgroup)")


def capacity_change(argv: list[str], note: Callable[[str], None] | None = None) -> dict[str, Any] | None:
    """What `slotfather slots ARGV` saves: {"slots": N}, {"mode", "gib_per_slot", "slots"} or None for reset; `note`
    gets what the user should know (`slots` prints it). The memory modes also save the count they give now as
    "slots", which slotfather 0af7546 and older read alone. Raises SlotError for anything `slots` refuses."""
    if len(argv) == 1 and argv[0] == "reset":
        return None
    if len(argv) == 1 and argv[0].isascii() and argv[0].isdigit() and int(argv[0]) >= 1:
        return {"slots": int(argv[0])}
    if len(argv) == 2 and argv[0] in _config.CAPACITY_MODES:
        try:
            per_slot: float = float(argv[1])
        except ValueError:
            per_slot = 0.0
        if math.isfinite(per_slot) and per_slot > 0:
            per_slot = int(per_slot) if per_slot.is_integer() else per_slot
            # The cgroup is the saver's (SLOTFATHER_CGROUP, or its outermost limited one), not the one saved before.
            group = (_config.setting("CGROUP") or _config.DEFAULT_CGROUP or _platform.limited_cgroup()) \
                if _platform.linux() else None
            if _platform.linux() and _platform.cgroup_limit(group) is None and note is not None:
                note(NO_CGROUP)
            by_memory = memory_slots(_platform.memory_gib(group or ""), per_slot)
            if by_memory is None:
                raise _config.SlotError("the machine's memory is unknown here; save a count instead (slotfather slots N)")
            cpu = max(1, _config.cores() // _config.CORES_PER_SLOT)
            found: dict[str, Any] = {"slots": by_memory if argv[0] == "memory" else min(cpu, by_memory),
                                     "mode": argv[0], "gib_per_slot": per_slot}
            if _platform.cgroup_limit(group) is not None:
                found["cgroup"] = group
            return found
    raise _config.SlotError("slots takes a positive integer, memory GIB or lower GIB (GiB per slot), or reset")


# Order

def order_extension_keys(saved: dict[str, Any]) -> list[str]:
    """Non-default keys that older Slotfather releases may reject in order.json."""
    return [key for key, default in _config.ORDER_DEFAULTS.items()
            if key not in _config.ORDER_BASE + ("limit",) and saved.get(key, default) != default]


def order_compatibility(keys: list[str], *, historical: bool = False) -> str:
    warning = f"Older Slotfather clients may use Arrival order. New keys in order.json: {', '.join(keys)}."
    if historical:
        warning += (" Releases 3e3a621 and earlier know no classes. Releases before ageing know no aging or warn "
                    "settings. Releases before the memory warning allowance know no warning_admits. Releases before "
                    "fathers#160 know no reservation_timeout. Releases before "
                    "fathers#184 know no load_gate. An unknown key makes their looks use the window order.")
    return warning


def order_settings(directory: Path | None = None, environment: bool = True) -> dict[str, Any]:
    """The queue's order and backfill settings: order.json beside the queue (`slotfather order`), else
    ORDER_DEFAULTS, with "saved" saying which. A client's environment only tightens them: SLOTFATHER_QUEUE=window
    falls back to the window order, a higher SLOTFATHER_BACKFILL_SAFETY or _FALLBACK, a lower _OVERRUN and a lower
    (nonzero) _LIMIT count; looser values are ignored, since every client's look grants for the whole queue. A file
    that cannot be read gives the window order."""
    found: dict[str, Any] = dict(_config.ORDER_DEFAULTS, saved=False)
    try:
        data = json.loads(((directory or _config.state_dir()) / "order.json").read_text())
        found.update(checked_order(data), saved=True)
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError, AttributeError):
        found.update(order="window", saved=True)
    if not environment:  # what every client shares; yield requests are decided on it alone
        return found
    if _config.setting("QUEUE") == "window":
        found["order"] = "window"
    for key, (name, least, tighten) in _config.ORDER_KNOBS.items():
        try:
            value = float(_config.setting(name) or "nan")
        except ValueError:
            continue
        if not math.isfinite(value) or value < least:
            continue
        if tighten == "up":
            found[key] = max(found[key], value)
        elif tighten == "down":
            found[key] = min(found[key], value)
        elif tighten == "delay":  # 0 asks nobody (ages nobody), the tightest
            found[key] = 0.0 if not value or not found[key] else max(found[key], value)
        elif tighten == "own":  # decides nothing for other clients: this client's environment sets it
            found[key] = value
        elif value > 0:  # limit: 0 is no limit, the loosest
            found[key] = value if not found[key] else min(found[key], value)
    return found


def checked_order(data: Any) -> dict[str, Any]:
    """Saved order settings, validated: raises ValueError for anything `slotfather order` would refuse."""
    if not isinstance(data, dict) or set(cast(dict[str, Any], data)) - set(_config.ORDER_DEFAULTS) or \
            cast(dict[str, Any], data).get("order", "backfill") not in _config.QUEUE_ORDERS:
        raise ValueError(f"not order settings: {data!r}")
    found: dict[str, Any] = {}
    for key, value in cast(dict[str, Any], data).items():
        if key != "order":
            least = _config.ORDER_KNOBS[key][1]
            if type(value) not in (int, float) or not math.isfinite(value) or value < least:
                raise ValueError(f"{key} must be a number of at least {least:g}, not {value!r}")
            if key == "warning_admits" and value not in (0, 1):
                raise ValueError(f"warning_admits must be 1 or 0, not {value!r}")
            if key == "memory_share" and value > 1.0:
                raise ValueError(f"memory_share must be at most 1, not {value!r}")
        found[key] = value
    return found


def order_bytes(values: Mapping[str, Any]) -> dict[str, Any]:
    """order.json as it is saved: the keys before classes always; a class knob only when it differs from its
    default, since a slotfather before them refuses a file with keys it does not know (and falls back to the window
    order)."""
    defaults = _config.ORDER_DEFAULTS
    return {k: values.get(k, defaults[k]) if k == "order" else float(values.get(k, defaults[k]))
            for k in defaults if k in _config.ORDER_BASE or values.get(k, defaults[k]) != defaults[k]}


# Sweep

SWEEP_BASE = ("classes", "removed_age", "orphan_age", "dev_server_age", "session_removed_age", "grace",
              "container_minutes", "container_restarts", "swap_free_mb", "swap_free_pct", "memory_top")
SWEEP_DEFAULTS: dict[str, Any] = {
    "classes": list(_config.SWEEP_CLASSES), "removed_age": 600.0, "orphan_age": 7200.0, "dev_server_age": 28800.0,
    "session_removed_age": 21600.0, "grace": 10.0, "container_minutes": 30.0, "container_restarts": 3.0,
    "swap_free_mb": 1024.0, "swap_free_pct": 20.0, "memory_top": 8.0,
    "disk_classes": list(_config.SWEEP_DISK_CLASSES),
    "docker_anonymous_age": 3600.0, "docker_dangling_age": 86400.0,
    "docker_superseded": [], "docker_superseded_keep": 2.0,
    "claude_scratch_age": 86400.0, "claude_scratch_gb": 2.0,
    "cache_free_gb": 100.0, "disk_free_gb": 100.0,
}
SWEEP_DURATIONS = ("removed_age", "orphan_age", "dev_server_age", "session_removed_age", "grace",
                   "container_minutes", "docker_anonymous_age", "docker_dangling_age", "claude_scratch_age")


def sweep_defaults() -> dict[str, Any]:
    return dict(SWEEP_DEFAULTS, classes=list(SWEEP_DEFAULTS["classes"]),
                disk_classes=list(SWEEP_DEFAULTS["disk_classes"]),
                docker_superseded=list(SWEEP_DEFAULTS["docker_superseded"]))


def sweep_bytes(values: Mapping[str, Any]) -> dict[str, Any]:
    """sweep.json as saved: the pre-disk base keys always, and disk keys only when they differ from defaults
    (fathers#6: sweep.json keeps the bytes it has today when no disk setting is changed)."""
    found: dict[str, Any] = {}
    for key, default in SWEEP_DEFAULTS.items():
        val = values.get(key, default)
        if key in SWEEP_BASE or val != default:
            found[key] = list(cast(list[Any], val)) if isinstance(val, list) else val
    return found


def sweep_from(text: str | bytes | None) -> dict[str, Any]:
    """sweep.json's text (None: no file) over SWEEP_DEFAULTS, as sweep_settings() reads it."""
    found = dict(sweep_defaults(), saved=False, warnings=[])
    if text is None:
        return found
    try:
        data: Any = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError(f"not sweep settings: {data!r}")
        data = cast(dict[str, Any], data)
        unknown = sorted(set(data) - set(SWEEP_DEFAULTS))
        if unknown:
            found["warnings"].append(f"sweep.json: unknown setting(s) {', '.join(unknown)} ignored")
        found.update(checked_sweep({k: v for k, v in data.items() if k in SWEEP_DEFAULTS}), saved=True)
    except (ValueError, TypeError) as error:
        found["error"] = f"sweep.json ignored: {error}"
    return found


def sweep_settings(directory: Path | None = None) -> dict[str, Any]:
    """sweep.json beside the queue over SWEEP_DEFAULTS. Unknown keys are ignored with a "warnings" entry (a newer
    slotfather may have written them); a file that cannot be read or holds a bad value gives the defaults with
    "error", and a live sweep then stops nothing."""
    try:
        return sweep_from(((directory or _config.state_dir()) / "sweep.json").read_text())
    except FileNotFoundError:
        return sweep_from(None)
    except OSError as error:
        return dict(sweep_from(None), error=f"sweep.json ignored: {error}")


def checked_sweep(data: Any) -> dict[str, Any]:
    """Sweep settings, validated: raises ValueError for anything `slotfather sweep config` would refuse."""
    if not isinstance(data, dict) or set(cast(dict[str, Any], data)) - set(SWEEP_DEFAULTS):
        raise ValueError(f"not sweep settings: {data!r}")
    given = cast(dict[str, Any], data)
    found: dict[str, Any] = {}
    for key, value in given.items():
        if key == "classes":
            if not isinstance(value, list) or any(not isinstance(c, str) for c in cast(list[Any], value)) \
                    or set(cast(list[str], value)) - set(_config.SWEEP_ALL_CLASSES):
                raise ValueError(f"classes are some of {', '.join(_config.SWEEP_ALL_CLASSES)}, not {value!r}")
            chosen = cast(list[str], value)
            found["classes"] = [c for c in _config.SWEEP_CLASSES if c in chosen]
            if "disk_classes" not in given and any(c in _config.SWEEP_DISK_CLASSES for c in chosen):
                found["disk_classes"] = [c for c in _config.SWEEP_DISK_CLASSES if c in chosen]
        elif key == "disk_classes":
            if not isinstance(value, list) or any(not isinstance(c, str) for c in cast(list[Any], value)) \
                    or set(cast(list[str], value)) - set(_config.SWEEP_DISK_CLASSES):
                raise ValueError(f"disk_classes are some of {', '.join(_config.SWEEP_DISK_CLASSES)}, not {value!r}")
            chosen_disk = cast(list[str], value)
            found["disk_classes"] = [c for c in _config.SWEEP_DISK_CLASSES if c in chosen_disk]
        elif key == "docker_superseded":
            if not isinstance(value, list) or any(
                    not isinstance(item, str) or not item.strip() or not item.isprintable()
                    for item in cast(list[Any], value)):
                raise ValueError(f"docker_superseded must be a list of image patterns, not {value!r}")
            found["docker_superseded"] = [item.strip() for item in cast(list[str], value)]
        elif (type(value) not in (int, float) or not math.isfinite(value)
              or value < (1 if key in ("memory_top", "docker_superseded_keep") else 0)):
            least = 1 if key in ("memory_top", "docker_superseded_keep") else 0
            raise ValueError(f"{key} must be a number of at least {least}, not {value!r}")
        else:
            found[key] = float(value)
    return found


# Saving

def configuration_bytes(directory: Path, names: tuple[str, ...] = FILES[:2]) -> dict[str, bytes | None]:
    """Read shared settings without creating a queue or lock; bytes are an optimistic edit token."""
    result: dict[str, bytes | None] = {}
    for name in names:
        try:
            result[name] = (directory / name).read_bytes()
        except FileNotFoundError:
            result[name] = None
    return result


def revision(data: bytes | None) -> str:
    """An opaque token for a settings file as read (configuration_bytes()): its SHA-256, or "absent"."""
    return "absent" if data is None else "sha256:" + hashlib.sha256(data).hexdigest()


class Conflict(_config.SlotError):
    """A settings file changed since the caller read it (configure()'s `expected`): nothing was saved."""


def configure(changes: Mapping[str, Mapping[str, Any] | None], *,
              expected: Mapping[str, bytes | None] | None = None, revisions: Mapping[str, str] | None = None,
              granted: Callable[[Path], None] | None = None, what: str = "a settings change",
              directory: Path | None = None) -> None:
    """The one write path of capacity.json, order.json and sweep.json, shared by the CLI and the dashboard:
    validation, ownership, conflict checking and the queue lock. A change applies at once, also while requests run
    or wait. order.json and sweep.json changes merge over the saved values (a sweep.json that cannot be read is
    replaced by the defaults and the changes); None resets a file. `expected` names files as their bytes were read,
    `revisions` as revision() tokens (`settings set --revision`); a file changed since is a Conflict and nothing is
    saved. `granted(directory)` runs under the lock once saved: the caller's grant of what the change lets start.
    `what` names the change in the refusal for another user's queue."""
    directory = directory or _config.state_dir()
    if set(changes) - set(FILES):
        raise _config.SlotError("unknown configuration file")
    if not changes:
        return
    _state.require_own(directory, what)
    with _state.locked(directory):
        # Only the files this change touches or names: a broken sweep.json must not stop a capacity change. A file
        # that cannot be read (its mode, a directory in its place) is replaced as a broken one is.
        before: dict[str, bytes | None] = {}
        unreadable: set[str] = set()
        names: list[str] = sorted({*changes, *(expected or {}), *(revisions or {})})
        for name in names:
            try:
                before.update(configuration_bytes(directory, (name,)))
            except OSError:
                before[name] = None
                unreadable.add(name)
        changed_since = [name for name, value in (expected or {}).items() if before.get(name) != value]
        changed_since += [name for name, token in (revisions or {}).items()
                          if name not in changed_since and revision(before.get(name)) != token]
        if changed_since:  # per file: a caller expects only the files it read and means to replace
            raise Conflict(f"{', '.join(changed_since)} changed in another process; nothing was saved")
        values: dict[str, Any] = dict(changes)
        try:
            if values.get("order.json") is not None:
                saved = dict(_config.ORDER_DEFAULTS)
                with contextlib.suppress(OSError, ValueError, TypeError, AttributeError):
                    saved.update(checked_order(json.loads(before["order.json"] or b"{}")))
                values["order.json"] = order_bytes(checked_order({**saved, **values["order.json"]}))
            if values.get("sweep.json") is not None:
                current = sweep_defaults() if "sweep.json" in unreadable else sweep_from(before["sweep.json"])
                if "error" in current:  # a broken file is replaced by the defaults and these changes
                    current = sweep_defaults()
                kept = {k: v for k, v in current.items() if k in SWEEP_DEFAULTS}
                values["sweep.json"] = sweep_bytes(checked_sweep({**kept, **values["sweep.json"]}))
        except ValueError as error:
            raise _config.SlotError(str(error)) from None
        # A capacity change applies at the next grant, whatever the queue holds (#7): every client's plan reads it
        # under this lock. Running leases keep their slots; a shrink below them drains, a growth grants below.
        changed: list[str] = []
        try:
            for name, value in values.items():
                if value is None:
                    (directory / name).unlink(missing_ok=True)
                else:
                    _state.write(directory / name, value)
                changed.append(name)
        except OSError as error:
            for name in reversed([name for name in changed if name not in unreadable]):
                path, original = directory / name, before[name]
                if original is None:
                    path.unlink(missing_ok=True)
                else:
                    from fathers_common.atomic import atomic_write
                    atomic_write(path, original)
            raise _config.SlotError(f"cannot save settings: {error}") from None
        after: dict[str, bytes | None] = {}
        for name in changed:
            with contextlib.suppress(OSError):
                after.update(configuration_bytes(directory, (name,)))
        for name in [name for name in changed if name in unreadable or after.get(name) != before.get(name)]:  # a no-op save is no event
            if name == "capacity.json":
                with contextlib.suppress(_config.SlotError, OSError):
                    basis = capacity_basis()
                    _events.record(directory, None, "capacity.changed", {"type": "capacity", "id": "capacity"},
                                 {"saved": values[name], "mode": basis["mode"], "slots": basis["slots"]})
            elif name == "order.json":
                _events.record(directory, None, "order.changed", {"type": "order", "id": "order"},
                             {"reset": True} if changes[name] is None else {"saved": changes[name]})
        if granted is not None:  # what the new settings start, granted now as a release does
            with contextlib.suppress(_config.SlotError, OSError):
                granted(directory)
