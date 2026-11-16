"""Durations as Slotfather's commands accept them (fathers#34): one module, three grammars, each the one a surface
has always accepted, so that none accepts more or less than it did.

- clock(): `90`, `90s`, `30m`, `1.5h`, `1h30m` (hours, minutes, seconds; no days): `slotfather order`, `settings`,
  `session start --idle`.
- span(): one number with one unit of s, m, h or d (`90`, `30m`, `2h`, `1d`; no compounds): `slotfather sweep
  config`.
- since(): an age before now (`90m`, `36h`, `7d`, a unit required) or a local ISO date or time: `stats --since`.
"""
from __future__ import annotations

import datetime
import re
import time

UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def clock(text: str) -> float:
    """Seconds from `90`, `90s`, `30m`, `1.5h` or `1h30m`; raises ValueError for anything else."""
    words = re.fullmatch(r"\s*(?:(\d+(?:\.\d+)?)h)?\s*(?:(\d+(?:\.\d+)?)m)?\s*(?:(\d+(?:\.\d+)?)s?)?\s*", text or "")
    if not words or not any(words.groups()):
        raise ValueError(f"not a duration: {text!r}")
    hours, minutes, rest = (float(part) if part else 0.0 for part in words.groups())
    return hours * 3600 + minutes * 60 + rest


def span(text: str) -> float:
    """Seconds from one number with an optional unit of s, m, h or d; raises ValueError for anything else."""
    found = re.fullmatch(r"(\d+(?:\.\d+)?)([smhd]?)", text.strip())
    if not found:
        raise ValueError(f"not a duration: {text!r} (seconds, or 90s, 30m, 2h, 1d)")
    return float(found[1]) * UNITS[found[2]]


def since(text: str, moment: float | None = None) -> float:
    """An age (90m, 36h, 7d) before `moment` (default now), or a local ISO date or time, as epoch seconds; raises
    ValueError for anything else."""
    moment = time.time() if moment is None else moment
    age = re.fullmatch(r"(\d+(?:\.\d+)?)([smhd])", text.strip())
    if age:
        return moment - float(age[1]) * UNITS[age[2]]
    value = datetime.datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    return value.timestamp()  # a naive value is local time
