"""The client of slotfather, the machine-wide scheduler of slot requests and leases (maxvyshniakov/slotfather).

lanefather only calls the installed command:

  slotfather run -n K|all --label L [--meta K=V]... --priority P [--ticket ID] [--wait S] -- COMMAND...
  slotfather advise [--max N] [--exclusive]      JSON advice on the verification worker budget
  slotfather slots                               the shared capacity

A verify run queues itself: lanefather hands it the label, priority, optional ticket and metadata of its slot request as
<slot_env>_LABEL, _PRIORITY, _TICKET, _META (a JSON object) and _YIELDABLE, slot_env from `.lanefather.toml`.
Without slotfather on PATH a command runs directly, under no lease.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from typing import Any

from . import config

TIMED_OUT = 75  # slotfather run --wait gave up; the command did not run
PRIORITIES = ("interactive", "normal", "batch")
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
    value = os.environ.get("SLOTFATHER_COUNT", "")
    return int(value) if leased() and value.isdigit() else None


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
         ticket: str | None = None, required: bool = False) -> list[str]:
    """argv run under a lease of `count` slots (a number or "all"); argv itself under a lease or without
    slotfather. Required executions always enter the real CLI: it validates and deduplicates outer leases."""
    if priority not in PRIORITIES:
        raise ValueError(f"request priority {priority!r} is not one of {', '.join(PRIORITIES)}")
    ticket = ticket_value(ticket)
    tags = lease_meta(meta)
    found = command()
    if required and found is None:
        raise Unavailable("slotfather is not on PATH; a required execution lease cannot be acquired")
    if found is None or (leased() and not required):
        return list(argv)
    flags = [part for key, value in tags.items() for part in ("--meta", f"{key}={value}")]
    return [found, "run", "-n", str(count), "--label", label, *flags, "--priority", priority,
            *(("--ticket", ticket) if ticket else ()),
            *(("--yieldable",) if yieldable else ()), *(("--wait", f"{wait:g}") if wait is not None else ()),
            "--", *argv]


def queue_environment(label: str, priority: str = "normal", yieldable: bool = False, *,
                      ticket: str | None = None, **meta: Any) -> dict[str, str]:
    """The label, priority, optional ticket and tags for a verify run that queues itself."""
    if priority not in PRIORITIES:
        raise ValueError(f"request priority {priority!r} is not one of {', '.join(PRIORITIES)}")
    prefix = config.current().slot_env
    return {f"{prefix}_LABEL": label, f"{prefix}_PRIORITY": priority,
            f"{prefix}_TICKET": ticket_value(ticket) or "",
            f"{prefix}_META": json.dumps(clean_meta(meta), sort_keys=True),
            f"{prefix}_YIELDABLE": "1" if yieldable else ""}


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
