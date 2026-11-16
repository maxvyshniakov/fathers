"""The queue as stored, read (fathers#36): the process table a look is judged on, the live entries (reaping those
whose holder is gone), the earlier queues' holders, the live entries this client does not know, the lease this
process runs under, and the estimator a look hands the scheduler. `leases` (the transactions), `query` (the views)
and the sweep read the queue through it, so that `query` does not import `leases`."""
from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any

from . import config as _config
from . import entry as _entry
from . import events as _events
from . import idle as _idle
from . import lineage as _lineage
from . import platform as _platform
from . import process as _process
from . import schedule as _schedule
from . import settings as _settings
from . import state as _state


Table = dict[int, tuple[int, str]]  # the process table: pid -> (parent pid, start time)


_skipping: list[str] = []  # the reason this process last logged a skipped look for, while ps stays unusable


def process_table(directory: Path, log: bool = True) -> Table:
    """process.processes() for a look that reaps or sweeps the queue at `directory`. When ps gives no table that
    can be trusted (config.ProcessTableError, a SlotError) the look reaps nothing and this raises; the caller skips
    that pass (a waiter's next look tries again). With `log`, a look that changes the queue says so in queue.log
    and the event log once per failure streak of this process (`skipped reaping`, reap.skipped), and once when ps
    answers again (`reaping resumed`, reap.resumed). Read-only looks (status, the dashboard) pass log=False: they
    must not flood the log a lasting failure fills already (fathers#121 review)."""
    own = _state.foreign_owner(directory) is None
    try:
        table = _process.processes()
    except _config.ProcessTableError as error:
        if log and own and not _skipping:
            _skipping.append(str(error))
            _events.record(directory, f"skipped reaping: {error}", "reap.skipped", {"type": "queue", "id": "processes"}, {"reason": str(error)},
                         _events.actor(kind="tool"), level="warn")
        raise
    if log and own and _skipping:
        reason = _skipping.pop()
        _events.record(directory, "reaping resumed: ps answers again", "reap.resumed", {"type": "queue", "id": "processes"}, {"after": reason},
                     _events.actor(kind="tool"))
    return table


def reap(directory: Path, table: Table) -> list[tuple[Path, dict[str, Any]]]:
    """Live entries in queue order; removes those whose holder exited or crashed (in another user's queue it only
    leaves them out). An entry this client does not know (entry.unknown(); fathers#19) is in neither: it is left
    alone and never reaped; the plan reserves its slots while its holder runs (reserved(), used_elsewhere())."""
    live: list[tuple[Path, dict[str, Any]]] = []
    readonly = _state.foreign_owner(directory) is not None
    for path, entry in _state.entries(directory):
        if _entry.unknown(entry) is not None:
            continue  # perhaps a newer client's live lease: `status` shows it (state.unknown())
        if _platform.alive(entry, table):
            live.append((path, entry))
        elif not readonly:
            path.unlink(missing_ok=True)
            _state.marker(path).unlink(missing_ok=True)
            _idle.path(path).unlink(missing_ok=True)
            path.with_suffix(".session").unlink(missing_ok=True)  # a session's runs (session.py)
            # queue.log first, as before the one writer: a crash before the history keeps the line (fathers#37 review)
            _events.record(directory, f"reaped {entry.get('state')} lease of {entry.get('count')} for "
                           f"{entry.get('label')} (pid {entry.get('pid')} exited)")
            item = _state.record(directory, entry, _entry.outcome(entry, reaped=True), path=path,
                                 capacity=_settings.total, above=run_above)
            _events.record(directory, None, "lease.reaped" if _entry.is_held(entry) else "request.reaped",
                           _events.request(entry, path),
                           {"count": entry.get("count"), "ran": item.get("ran"), "waited": item.get("waited"),
                            "reason": "owner exited"}, _events.actor(kind="tool"), level="warn")
    return live


def legacy_holders(table: Table) -> list[tuple[Path, dict[str, Any], int]]:
    """Held leases with a live holder in the earlier queues: (path, entry, slots here). Read only: their own
    clients reap them."""
    found: list[tuple[Path, dict[str, Any], int]] = []
    for directory in _config.legacy_dirs():
        for path, entry in _state.entries(directory):
            if _entry.is_held(entry) and _platform.alive(entry, table):
                found.append((path, entry, min(_settings.total(), math.ceil(entry["count"] * _settings.total() / _config.LEGACY_TOTAL))))
    return found


def legacy_used(table: Table) -> int:
    return sum(count for _, _, count in legacy_holders(table))


def reserved(directory: Path, table: Table) -> list[tuple[Path, dict[str, Any], int]]:
    """Entries at `directory` this client does not know (state.unknown()) whose holder is alive: (path, entry, the
    slots it reserves, entry.reserved()). Nothing else may take those slots: the entry may be a newer client's lease
    (fathers#19 review). One whose holder cannot be checked reserves nothing."""
    found: list[tuple[Path, dict[str, Any], int]] = []
    for path, entry in _state.unknown(directory):
        count = _entry.reserved(entry)
        if count and _platform.alive(entry, table):
            found.append((path, entry, count))
    return found


def used_elsewhere(directory: Path, table: Table) -> int:
    """The slots held without a known entry of this queue: by earlier queues' leases and by live unknown entries."""
    return legacy_used(table) + sum(count for _, _, count in reserved(directory, table))


def inherited_lease(table: Table) -> tuple[Path, dict[str, Any], int] | None:
    """The held lease this process runs under, here or in an earlier queue: (path, entry, slots here)."""
    mine = {os.getpid(), *_lineage.ancestors(_lineage.parents_of(table), os.getpid())}
    own = _config.setting("LEASE")
    for path, entry in _state.entries(_config.state_dir()):
        mine_now = _entry.phase(entry).name in (_entry.HELD, _entry.YIELDED)  # a yielded lease is still this tree's
        if mine_now and _platform.alive(entry, table) and (own == str(path) or entry["pid"] in mine):
            return path, entry, entry["count"]
    for path, entry, count in legacy_holders(table):
        if own == str(path) or entry["pid"] in mine:
            return path, entry, count
    return None


def run_above(ran: float, p90: Any) -> bool:
    """runtype.above(), for state.record(): loaded only when a run is judged, never on admission."""
    from . import runtype as _runtype
    return _runtype.above(ran, p90)


def estimator(directory: Path, p90: bool = False) -> _schedule.Estimate:
    """estimates.estimator(directory), whose module is imported only when plan() first asks for an estimate: `run`
    with nothing ahead of it never does, which keeps the admission path's imports as they were (fathers#35)."""
    loaded: list[_schedule.Estimate] = []

    def estimate(entry: _schedule.Entry) -> float | None:
        if not loaded:
            from . import estimates as _estimates
            loaded.append(_estimates.p90_estimator(directory) if p90 else _estimates.estimator(directory))
        return loaded[0](entry)
    return estimate
