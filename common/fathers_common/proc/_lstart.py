"""`ps -o lstart` text and epoch seconds, in either clock (fathers#171).

`ps -o lstart` prints a start time in the C locale as "Mon Nov 23 01:21:03 2026", with a space-padded day
("Tue Nov  3 ..."); every reader compares it with the spaces normalised ("Tue Nov 3 ..."). Which time zone it is in
depends on the TZ ps ran with: Spawnfather reads it in UTC, Slotfather in the caller's local time (and its queue
writes that text, so older clients keep reading it byte for byte). Text is presentation; the identity that crosses
between tools is the epoch second.

UTC text names exactly one epoch. Local text names none (the hour skipped when the clocks go forward), one, or two (the
hour repeated when they go back): `epochs()` gives every candidate, `parse()` the earliest. Rendering an epoch is never
ambiguous. No `time.mktime`: its choice in the repeated hour is the C library's.
"""
from __future__ import annotations

import time
from typing import Literal

Clock = Literal["utc", "local"]

FORMAT = "%a %b %d %H:%M:%S %Y"  # the C locale's lstart, for time.strftime/strptime
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
DAY = 86400

Fields = tuple[int, int, int, int, int, int]  # year, month, day, hour, minute, second


def norm(text: object) -> str:
    """lstart text with its spaces normalised ("Tue Nov  3" -> "Tue Nov 3"), as every reader stores and compares it."""
    return " ".join(str(text).split())


def _leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def _month_days(year: int, month: int) -> int:
    return (31, 29 if _leap(year) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)[month - 1]


def fields(text: object) -> Fields | None:
    """C-locale lstart text as (year, month, day, hour, minute, second), or None when it is not one. The weekday is
    not checked: the date names it."""
    words = norm(text).split()
    if len(words) != 5 or words[1] not in MONTHS:
        return None
    day, clock, year = words[2], words[3].split(":"), words[4]
    if not (day.isdigit() and year.isdigit() and len(clock) == 3 and all(part.isdigit() for part in clock)):
        return None
    month = MONTHS.index(words[1]) + 1
    hour, minute, second = (int(part) for part in clock)
    if not (1 <= int(day) <= _month_days(int(year), month) and hour < 24 and minute < 60 and second < 62):
        return None
    return int(year), month, int(day), hour, minute, second


def timegm(moment: Fields) -> int:
    """Epoch seconds of a UTC calendar time (calendar.timegm, without importing calendar)."""
    year, month, day, hour, minute, second = moment
    shifted = year - (month <= 2)
    era = shifted // 400
    of_era = shifted - era * 400
    of_year = (153 * (month + (-3 if month > 2 else 9)) + 2) // 5 + day - 1
    of_era_days = of_era * 365 + of_era // 4 - of_era // 100 + of_year
    days = era * 146097 + of_era_days - 719468
    return days * DAY + hour * 3600 + minute * 60 + second


def epochs(text: object, clock: Clock = "utc") -> tuple[int, ...]:
    """Every epoch second lstart `text` can name in `clock`, earliest first: () for text that is not lstart (or local
    time the clocks skipped), two in the local hour that repeats when the clocks go back."""
    moment = fields(text)
    if moment is None:
        return ()
    naive = timegm(moment)
    if clock == "utc":
        return (naive,)
    found: set[int] = set()
    for probe in (naive - DAY, naive, naive + DAY):  # the offsets in force around it (one change in two days at most)
        try:
            offset = time.localtime(probe).tm_gmtoff
        except (OverflowError, OSError, ValueError):
            continue
        candidate = naive - offset
        try:
            if tuple(time.localtime(candidate)[:6]) == moment:
                found.add(candidate)
        except (OverflowError, OSError, ValueError):
            continue
    return tuple(sorted(found))


def parse(text: object, clock: Clock = "utc") -> int | None:
    """The epoch second lstart `text` names in `clock` (the earliest of two in a repeated local hour), else None."""
    found = epochs(text, clock)
    return found[0] if found else None


def render(epoch: float, clock: Clock = "utc", *, padded: bool = False) -> str:
    """The lstart text `ps` prints for `epoch` in `clock` and the C locale: normalised (an unpadded day) by default,
    with `padded` the day space-padded exactly as ps prints it."""
    value = time.gmtime(epoch) if clock == "utc" else time.localtime(epoch)
    day = f"{value.tm_mday:2d}" if padded else str(value.tm_mday)
    return (f"{WEEKDAYS[value.tm_wday]} {MONTHS[value.tm_mon - 1]} {day} "
            f"{value.tm_hour:02}:{value.tm_min:02}:{value.tm_sec:02} {value.tm_year}")


def names(text: object, epoch: float, clock: Clock = "utc", *, slack: int = 0) -> bool:
    """Whether lstart `text` in `clock` can name `epoch`, within `slack` seconds."""
    return any(abs(candidate - epoch) <= slack for candidate in epochs(text, clock))
