"""How junkfather reads and writes durations and times: `--ttl 2h` in, `2h05m` and RFC 3339 UTC out."""
from __future__ import annotations

import argparse
import datetime
import re
from typing import Any, Optional

UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def seconds(text: str) -> float:
    """A duration on the command line: seconds, or 90s, 30m, 2h, 1d."""
    found = re.fullmatch(r"(\d+(?:\.\d+)?)([smhd]?)", text.strip())
    if not found or float(found[1]) <= 0:
        raise argparse.ArgumentTypeError(f"not a duration: {text!r} (use 90s, 30m, 2h, 1d)")
    return float(found[1]) * UNITS[found[2]]


def duration(value: float) -> str:
    """A duration for people: 45s, 90m, 5h05m, 3d4h."""
    value = int(max(0, value))
    if value < 120:
        return f"{value}s"
    minutes = value // 60
    if minutes < 120:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours}h{minutes:02d}m" if minutes else f"{hours}h"
    return f"{hours // 24}d{hours % 24}h"


def iso(epoch: Any) -> Optional[str]:
    """Epoch seconds as RFC 3339 UTC with milliseconds (Godfather's time format); None for anything else."""
    if not isinstance(epoch, (int, float)):
        return None
    moment = datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"
