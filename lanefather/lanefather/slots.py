"""The client of slotfather, the machine-wide scheduler of slot requests and leases (maxvyshniakov/slotfather).

lanefather only calls the installed command:

  slotfather run -n K|all --label L [--meta K=V]... --priority P [--ticket ID] [--wait S] -- COMMAND...
  slotfather advise [--max N] [--exclusive]      JSON advice on the verification worker budget
  slotfather slots                               the shared capacity

A verify run queues itself: lanefather hands it the label, priority, optional ticket and metadata of its slot request as
<slot_env>_LABEL, _PRIORITY, _TICKET, _META (a JSON object) and _YIELDABLE, slot_env from `.lanefather.toml`.
Without slotfather on PATH a command runs directly, under no lease.

Each run's priority (lanefather#24), first wins: LANEFATHER_PRIORITY_<PHASE> (POST_MERGE for post-merge),
LANEFATHER_PRIORITY, this machine's `slots.priority.<phase>` setting (lanefather.settings), `[slots.priority]`
<phase> and then `default` in `.lanefather.toml`, the built-in below. The environment names also read the project's
legacy prefix and `[env]` aliases, as every setting does (config.names).
Each request carries the priority and its source as metadata `lanefather.priority`, which `slotfather status` shows.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from typing import Any, cast

from . import config, settings
from .errors import LaneError

TIMED_OUT = 75  # slotfather run --wait gave up; the command did not run
PRIORITIES = config.PRIORITIES
PHASES = config.SLOT_PHASES
# Each run's built-in priority (#650). The orchestrator waits on a post-merge smoke, a batch gate and a rerun; a
# lane check is ordinary work; the perf suite measures, which slotfather ranks batch.
DEFAULT_PRIORITIES = {"check": "normal", "post-merge": "interactive", "gate": "interactive", "rerun": "interactive",
                      "perf": "batch"}
PRIORITY_META = "lanefather.priority"
META_KEY = re.compile(r"[a-z0-9_.-]{1,32}")
META_VALUE_LIMIT, META_KEYS_LIMIT = 200, 16  # slotfather's limits


class Unavailable(Exception):
    """slotfather is not installed or did not answer."""


def command() -> str | None:
    """The slotfather on PATH, or None."""
    return shutil.which("slotfather")


def leased() -> bool:
    """Whether this process runs under a lease (an outer slotfather run handed it one)."""
    return bool(os.environ.get("SLOTFATHER_LEASE"))


def lease_count() -> int | None:
    """The slots of the lease this process runs under, or None."""
    if not leased():
        return None
    for var in ("SLOTFATHER_SLOTS", "SLOTFATHER_COUNT"):
        value = os.environ.get(var, "")
        if value.isdigit() and int(value) > 0:
            return int(value)
    return None


def warn_outer_lease(command: str, wanted: int | None = None, *, prog: str = "lanefather") -> bool:
    """Warn on stderr when a self-leasing command runs inside an outer Slotfather lease (fathers#253).
    Returns True when a warning was emitted."""
    if not leased():
        return False
    held = lease_count()
    if held is not None:
        slot_word = "slot" if held == 1 else "slots"
        budget = f" (needed {wanted})" if wanted is not None and held < wanted else ""
        detail = (
            f"an outer Slotfather lease of {held} {slot_word}{budget}, which pins the whole {command} "
            f"(and its verifier) to {held} {slot_word}"
        )
    else:
        detail = f"an outer Slotfather lease, which pins the whole {command} (and its verifier) to that lease"
    print(
        f"lane: warning: `{prog} {command}` is running inside {detail}; `{prog} check`, `{prog} land` and "
        f"`{prog} promote` lease for themselves and must not be wrapped in `slotfather run --`",
        file=sys.stderr,
    )
    return True


def priority_setting(phase: str) -> str:
    """The setting of a phase's priority: PRIORITY_GATE, PRIORITY_POST_MERGE (LANEFATHER_<setting> in the
    environment)."""
    return "PRIORITY_" + phase.upper().replace("-", "_")


def phase_priority(phase: str, fallback: str | None = None, *,
                   project: config.Config = config.DEFAULT) -> tuple[str, str]:
    """The Slotfather priority of a run of `phase` and where it comes from: LANEFATHER_PRIORITY_<PHASE>,
    LANEFATHER_PRIORITY, the machine setting slots.priority.<phase>, `[slots.priority]` <phase>, `[slots.priority]`
    default, then `fallback` or the built-in.
    An empty variable is unset; a value that is not a Slotfather priority, or a LANEFATHER_PRIORITY_<NAME> that
    names no phase, is refused."""
    if phase not in PHASES:
        raise ValueError(f"unknown slot phase {phase!r}")
    known = {"LANEFATHER_" + priority_setting(name) for name in PHASES}
    stray = sorted(key for key in os.environ if key.startswith("LANEFATHER_PRIORITY_") and key not in known)
    if stray:
        raise LaneError(f"{', '.join(stray)} names no phase; the phases are {', '.join(sorted(known))}")
    for setting in (priority_setting(phase), "PRIORITY"):
        found = project.lookup(setting)
        if found and found[1]:
            if found[1] not in PRIORITIES:
                raise LaneError(f"{found[0]}={found[1]!r} is not a Slotfather priority; use {', '.join(PRIORITIES)}")
            return found[1], found[0]
    machine = settings.value(f"slots.priority.{phase}")
    if machine in PRIORITIES:
        return machine, f"lanefather settings slots.priority.{phase}"
    table = dict(project.slot_priority)
    for key in (phase, "default"):
        if key in table:
            return table[key], f"{config.CONFIG_NAME} [slots.priority] {key}"
    return fallback or DEFAULT_PRIORITIES[phase], "built-in"


def priority_meta(priority: str, source: str) -> dict[str, str]:
    """The request's metadata naming its effective priority and where it comes from, for `slotfather status`."""
    return {PRIORITY_META: f"{priority} ({source})"}


def priorities(project: config.Config = config.DEFAULT) -> dict[str, dict[str, str]]:
    """Every phase's effective priority and its source, for `list`."""
    return {phase: dict(zip(("priority", "source"), phase_priority(phase, project=project), strict=True))
            for phase in PHASES}


def clean_meta(meta: dict[str, Any]) -> dict[str, str]:
    """The metadata slotfather accepts: lowercase keys of at most 32 characters, printable values of at most 200,
    at most 16 keys; empty values are left out."""
    result: dict[str, str] = {}
    for key, value in meta.items():
        if key in ("class", "priority", "ticket"):
            raise ValueError(f"{key} is a request field, not metadata")
        if value is None or value == "" or not META_KEY.fullmatch(str(key)):
            continue
        result[str(key)] = "".join(c if c.isprintable() else " " for c in str(value))[:META_VALUE_LIMIT]
    return dict(list(result.items())[:META_KEYS_LIMIT])


def lease_meta(meta: dict[str, Any] | None = None) -> dict[str, str]:
    """Who asks and why: the session, then `meta` (lane, phase, kind, check)."""
    found: dict[str, Any] = {}
    session = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if session:
        found["session"] = session
    found.update(meta or {})
    return clean_meta(found)


def ticket_value(ticket: str | None) -> str | None:
    if not ticket or ticket == "none":
        return None
    if len(ticket) > META_VALUE_LIMIT or not ticket.isprintable():
        raise ValueError("ticket must be a short printable identifier")
    return ticket.lstrip("#") if ticket.lstrip("#").isdigit() else ticket


def wrap(count: int | str, label: str, argv: list[str], wait: float | None = None,
         meta: dict[str, Any] | None = None, priority: str = "normal", yieldable: bool = False,
         ticket: str | None = None, required: bool = False, min_count: int | None = None) -> list[str]:
    """argv run under a lease of `count` slots (a number, `MIN-MAX` when `min_count` is set and supported, or "all");
    argv itself under a lease or without slotfather. Required executions always enter the real CLI: it validates and
    deduplicates outer leases."""
    if priority not in PRIORITIES:
        raise ValueError(f"request priority {priority!r} is not one of {', '.join(PRIORITIES)}")
    ticket = ticket_value(ticket)
    tags = lease_meta(meta)
    found = command()
    if required and found is None:
        raise Unavailable("slotfather is not on PATH; a required execution lease cannot be acquired")
    if found is None or (leased() and not required):
        return list(argv)
    slot_spec = (f"{max(1, min_count)}-{count}"
                 if min_count is not None and type(count) is int and 1 <= min_count < count and supports_range()
                 else str(count))
    flags = [part for key, value in tags.items() for part in ("--meta", f"{key}={value}")]
    return [found, "run", "-n", slot_spec, "--label", label, *flags, "--priority", priority,
            *(("--ticket", ticket) if ticket else ()),
            *(("--yieldable",) if yieldable else ()), *(("--wait", f"{wait:g}") if wait is not None else ()),
            "--", *argv]


def queue_environment(label: str, priority: str = "normal", yieldable: bool = False, *,
                      project: config.Config = config.DEFAULT, ticket: str | None = None,
                      min_jobs: int | None = None, **meta: Any) -> dict[str, str]:
    """The label, priority, optional ticket and tags for a verify run that queues itself."""
    if priority not in PRIORITIES:
        raise ValueError(f"request priority {priority!r} is not one of {', '.join(PRIORITIES)}")
    prefix = project.slot_env
    env = {f"{prefix}_LABEL": label, f"{prefix}_PRIORITY": priority,
           f"{prefix}_TICKET": ticket_value(ticket) or "",
           f"{prefix}_META": json.dumps(clean_meta(meta), sort_keys=True),
           f"{prefix}_YIELDABLE": "1" if yieldable else ""}
    if min_jobs is not None:
        env[f"{prefix}_MIN_JOBS"] = str(min_jobs)
    return env


def ask(*args: str) -> str:
    found = command()
    if found is None:
        raise Unavailable("slotfather is not on PATH")
    try:
        result = subprocess.run([found, *args], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as error:
        raise Unavailable(f"slotfather {' '.join(args)}: {error}") from None
    if result.returncode:
        raise Unavailable(f"slotfather {' '.join(args)} exited {result.returncode}: "
                          f"{(result.stderr or result.stdout).strip()[:300]}")
    return result.stdout


def advise(maximum: int | None = None, exclusive: bool = False) -> dict[str, Any]:
    """`slotfather advise`: the worker budget for the load and the queue, with the reasons slotfather gives."""
    output = ask("advise", *(("--max", str(maximum)) if maximum else ()), *(("--exclusive",) if exclusive else ()))
    try:
        advice = json.loads(output)
        int(advice["jobs"])
    except (ValueError, KeyError, TypeError):
        raise Unavailable(f"slotfather advise printed no worker budget: {output.strip()[:300]}") from None
    return advice


def total() -> int:
    """The machine's slot capacity (`slotfather slots`)."""
    output = ask("slots")
    found = re.match(r"\s*(\d+)", output)
    if not found:
        raise Unavailable(f"slotfather slots printed no capacity: {output.strip()[:300]}")
    return int(found.group(1))


def capabilities(timeout: float = 2.0) -> dict[str, Any] | None:
    """Best-effort read of `slotfather capabilities --json`; None when unavailable or unsupported."""
    found = command()
    if found is None:
        return None
    try:
        result = subprocess.run([found, "capabilities", "--json"], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode:
        return None
    try:
        data: Any = json.loads(result.stdout)
    except ValueError:
        return None
    return cast(dict[str, Any], data) if isinstance(data, dict) else None


def supports_range() -> bool:
    """Whether the installed Slotfather advertises ranged slot requests (`run -n MIN-MAX`, fathers#128)."""
    data = capabilities()
    if not data:
        return False
    features: Any = data.get("features")
    if isinstance(features, list) and "slot-range" in features:
        return True
    run_info: Any = data.get("run")
    return isinstance(run_info, dict) and cast(dict[str, Any], run_info).get("slot_range") is True


def request_status(pid: int, timeout: float = 2.0) -> dict[str, Any] | None:
    """Best-effort read of `slotfather status --json --pid <pid>` for progress reporting; None when unavailable."""
    found = command()
    if found is None or pid <= 0:
        return None
    try:
        result = subprocess.run([found, "status", "--json", "--pid", str(pid)],
                                capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode:
        return None
    try:
        data: Any = json.loads(result.stdout)
    except ValueError:
        return None
    return cast(dict[str, Any], data) if isinstance(data, dict) else None
