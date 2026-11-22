"""lanefather wait: block until a lane is registered, landed or done (fathers#175), instead of a loop over `list`.

It reads the lane's record, then sleeps with a growing pause (FIRST seconds, up to LONGEST) until the state arrives
or the timeout passes: no busy loop, and one read of a small file per pause. Exit codes (docs/cli.md): 0 the state
is reached, 75 the timeout passed first (try again later), 3 the state cannot come (a lane done without landing
never lands), 2 usage.
"""
from __future__ import annotations

from collections.abc import Callable
import json
import sys
import time
from typing import Any

from . import lifecycle, state
from .state import Record

TARGETS = ("registered", "landed", "done")
DEFAULT_TIMEOUT = 3600.0
FIRST, GROWTH, LONGEST = 0.2, 1.5, 5.0
REACHED, TIMEOUT, NEVER, INTERRUPTED = 0, 75, 3, 130


def reached(record: Record | None, target: str, first: Record | None) -> bool | None:
    """Whether `record` is in `target`: True, False (not yet), or None when it cannot get there. `first` is the
    record when the wait began: a lane done before it is an earlier lane of the same id, which `registered` does
    not take for the one awaited."""
    if record is None:
        return False
    if target == "registered":
        if lifecycle.live(record):
            return True
        return lifecycle.finished(record) and not (lifecycle.finished(first)
                                                   and first is not None
                                                   and first.get("created") == record.get("created"))
    if target == "landed":
        if lifecycle.awaiting_done(record):
            return True
        if lifecycle.finished(record):
            return True if record.get("landed") else None
        return False
    return lifecycle.finished(record)


def wait_for(store: state.Store, lane_id: str, target: str, timeout: float, *,
             sleep: Callable[[float], None] = time.sleep,
             clock: Callable[[], float] = time.monotonic) -> tuple[bool | None, Record | None]:
    """Read the lane until it reaches `target` or `timeout` seconds pass; the outcome (as `reached`) and the last
    record read."""
    path = store.record_path(lane_id)
    first = state.read_json(path)
    record, began, pause = first, clock(), FIRST
    while True:
        outcome = reached(record, target, first)
        if outcome is not False:
            return outcome, record
        left = timeout - (clock() - began)
        if left <= 0:
            return False, record
        sleep(min(pause, left))
        pause = min(pause * GROWTH, LONGEST)
        record = state.read_json(path)


def describe(lane_id: str, record: Record | None) -> str:
    if record is None:
        return f"lane {lane_id} is not registered"
    return f"lane {lane_id} is {record.get('status')}"


def reached_line(lane_id: str, target: str, record: Record) -> str:
    status = record.get("status")
    if target == "landed":
        landed: dict[str, Any] = record.get("landed") or {}
        merge = str(landed.get("merge") or "")
        return f"Lane {lane_id} is {status} (merge {merge[:10]})" if merge else f"Lane {lane_id} is {status}"
    if target == "registered" and not lifecycle.finished(record):
        where = f", worktree {record['worktree']}" if record.get("worktree") else ""
        return f"Lane {lane_id} is registered ({status}{where})"
    return f"Lane {lane_id} is {status}"


def command(args: Any, store: state.Store) -> int:
    prog = store.prog
    started = time.monotonic()
    try:
        outcome, record = wait_for(store, args.id, args.target, args.timeout)
    except KeyboardInterrupt:
        return INTERRUPTED
    if args.json:
        landed: dict[str, Any] = (record.get("landed") or {}) if record else {}
        print(json.dumps({"v": 1, "lane": args.id, "for": args.target,
                          "status": record.get("status") if record else None, "reached": outcome is True,
                          "waited": round(time.monotonic() - started, 1), "merge": landed.get("merge")}))
    if outcome is True and record is not None:
        if not args.json:
            print(reached_line(args.id, args.target, record))
        return REACHED
    if outcome is None:
        print(f"{prog}: {describe(args.id, record)} without landing: it will not land", file=sys.stderr)
        return NEVER
    print(f"{prog}: {describe(args.id, record)}, not {args.target}, after {args.timeout:g} s", file=sys.stderr)
    return TIMEOUT
