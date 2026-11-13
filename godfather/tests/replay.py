"""A recorded scene's queue operations as Slotfather writes them to its event log (fathers#84).

The demo scene (tools/fixtures/navigation.json) recorded what EVENTS showed from Slotfather's snapshot: each grant
and release with its time, slots, label and pid. Slotfather's event log says the same operations with structured
fields (slotfather/src/slotfather/leases.py): `lease.granted` with `waited`, `lease.released` with `exit` and `ran`.
lines() writes those lines, so the goldens, the README media and the replay test (tests/test_feed.py) draw EVENTS
from a log as the dashboard does. The recording has no waits or exit codes: a lease's run is the time from its
grant to its release, a grant the queue made the moment another lease ended waited a fixed, made-up time per pid,
the others not at all, and the leases in FAILED exited 1. Each is a fixture's fact, never a measurement.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any, Callable, Optional

FAILED = (21199,)  # leases the scene shows as failed (exit 1), as tests/fixtures/expected did
HOST = "mbp"


def stamp(moment: dt.datetime) -> str:
    utc = moment.astimezone(dt.timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def when_of(value: Any) -> dt.datetime:
    return value if isinstance(value, dt.datetime) else dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def lines(events: list[dict[str, Any]], live: Optional[dict[int, tuple[Any, Any]]] = None,
          lease: Optional[Callable[[int], Optional[str]]] = None) -> list[str]:
    """Slotfather's event-log lines for snapshot-style events ({"when", "verb", "count", "label", "pid"}), in
    their order. `live`: {pid: (since, granted)} of the leases still running, whose wait is known; `lease` names a
    pid's lease id (fathers#101) where the scene has them."""
    granted: dict[int, dt.datetime] = {}
    released = {when_of(event["when"]) for event in events if event["verb"] == "released"}
    found: list[str] = []
    for number, event in enumerate(events, 1):
        when, pid, count = when_of(event["when"]), int(event["pid"]), int(event["count"])
        subject: dict[str, Any] = {"type": "request", "id": f"{number:010d}-{pid}", "label": event["label"],
                                   "pid": pid}
        if lease is not None and lease(pid):
            subject["lease_id"] = lease(pid)
        if event["verb"] == "granted":
            granted[pid] = when
            since = (live or {}).get(pid, (None, None))[0]
            waited = round((when - when_of(since)).total_seconds()) if since else \
                (pid * 37 % 540 + 6 if when in released else 0)
            kind, details = "lease.granted", {"count": count, "priority": "normal", "waited": waited}
        elif event["verb"] == "released":
            start = granted.pop(pid, None)
            ran = round((when - start).total_seconds()) if start else None
            kind, details = "lease.released", {"count": count, "exit": 1 if pid in FAILED else 0, "ran": ran}
        else:
            continue
        found.append(json.dumps({"v": 1, "time": stamp(when), "host": HOST, "source": "slotfather", "kind": kind,
                                 "subject": subject, "actor": {"type": "process", "pid": pid}, "details": details},
                                sort_keys=True))
    return found
