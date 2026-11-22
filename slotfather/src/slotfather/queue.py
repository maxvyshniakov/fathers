"""The queue as stored, read (fathers#36): the process table a look is judged on, the live entries (reaping those
whose holder is gone), the earlier queues' holders, the live entries this client does not know, the lease this
process runs under, and the estimator a look hands the scheduler. `leases` (the transactions), `query` (the views)
and the sweep read the queue through it, so that `query` does not import `leases`."""
from __future__ import annotations

import contextlib
import math
import os
from pathlib import Path
import time
from typing import Any, Callable, Iterator, Mapping, cast

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
            if not readonly and stale_lock(entry, table):
                clear_lock(directory, path, entry)
            live.append((path, entry))
        elif not readonly:
            path.unlink(missing_ok=True)
            _state.marker(path).unlink(missing_ok=True)
            _idle.path(path).unlink(missing_ok=True)
            _state.session_path(path).unlink(missing_ok=True)
            # queue.log first, as before the one writer: a crash before the history keeps the line (fathers#37 review)
            _events.record(directory, f"reaped {entry.get('state')} lease of {entry.get('count')} for "
                           f"{entry.get('label')} (pid {entry.get('pid')} exited)")
            item = _state.record(directory, entry, _entry.decide_outcome(entry, reaped=True), path=path,
                                 capacity=_settings.total, above=run_above)
            _events.record(directory, None, "lease.reaped" if _entry.is_held(entry) else "request.reaped",
                           _events.request(entry, path),
                           {"count": entry.get("count"), "ran": item.get("ran"), "waited": item.get("waited"),
                            "reason": "owner exited"}, _events.actor(kind="tool"), level="warn")
    return live


@contextlib.contextmanager
def looking(directory: Path, processes: Callable[[], Table] | None = None, log: bool = False
            ) -> Iterator[tuple[Table, list[tuple[Path, dict[str, Any]]]]]:
    """Read the process table and reap the queue under one lock, held through the caller's transaction."""
    with _state.locked(directory):
        table = processes() if processes is not None else process_table(directory, log=log)
        yield table, reap(directory, table)


def named_session(live: list[tuple[Path, dict[str, Any]]], name: str) -> tuple[Path, dict[str, Any]] | None:
    for path, entry in live:
        spec: object = entry.get("session")
        if isinstance(spec, dict) and cast(Mapping[str, object], spec).get("name") == name:
            return path, entry
    return None

def stale_lock(entry: dict[str, Any], table: Table) -> bool:
    """Whether a lease's lock mark (`slotfather lock`, fathers#159) has no live waiter: its `lock_owner` is not the
    process running at that pid (killed, even by SIGKILL, before it cleared the mark), or it names none."""
    if _entry.lock_of(entry) is None:
        return False
    owner = _entry.lock_owner(entry)
    return owner is None or not _platform.alive({"pid": owner[0], "start": owner[1]}, table)


def clear_lock(directory: Path, path: Path, entry: dict[str, Any]) -> None:
    """Clears a stale lock mark under the queue lock (reap()): a yielded lease is grantable again at this look's
    grant, a held one no longer says it waits (fathers#159 review)."""
    lock, owner = _entry.lock_of(entry), _entry.lock_owner(entry)
    _entry.lock_taken(entry)
    _state.write(path, entry)
    who = f"pid {owner[0]}" if owner else "no recorded waiter"
    _events.record(directory, f"the waiter for lock {lock} is gone ({who}) for {entry.get('label')} "
                              f"(pid {entry.get('pid')})",
                   "lease.lock_ended", _events.request(entry, path),
                   {"lock": lock or "", "why": "gone", "yielded": not _entry.is_held(entry), "waited": None},
                   _events.actor(kind="tool"), level="warn")


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


def memory_estimator(directory: Path) -> _schedule.Estimate:
    """estimates.memory_estimator(directory), imported lazily on first memory estimate lookup (fathers#9)."""
    loaded: list[_schedule.Estimate] = []

    def estimate(entry: _schedule.Entry) -> float | None:
        if not loaded:
            from . import estimates as _estimates
            loaded.append(_estimates.memory_estimator(directory))
        return loaded[0](entry)
    return estimate


def _ram_mb() -> float | None:
    ram_gib = _platform.memory_gib()
    return None if ram_gib is None else ram_gib * 1024.0


def live_host(
    *,
    averages: tuple[float, float, float] | None = None,
    cores: int | None = None,
) -> _schedule.Host:
    """Host resource readers wired lazily to platform/config, with optional pre-sampled overrides."""
    return _schedule.Host(
        memory=_platform.memory_level,
        ram_mb=_ram_mb,
        averages=_platform.load_averages if averages is None else averages,
        cores=_config.cores if cores is None else cores,
    )


def admission(
    directory: Path,
    waiters: list[_schedule.Entry],
    held: list[_schedule.Entry],
    slots: int,
    other: int,
    *,
    moment: float | None = None,
    settings: dict[str, Any] | None = None,
    shared_settings: dict[str, Any] | None = None,
    estimate: _schedule.Estimate | None = None,
    host: _schedule.Host | None = None,
) -> _schedule.Admission:
    """Build an Admission over live queue state with lazy estimators and host readers."""
    return _schedule.Admission(
        waiters,
        held,
        slots,
        time.time() if moment is None else moment,
        other=other,
        settings=_settings.order_settings(directory) if settings is None else settings,
        shared_settings=_settings.order_settings(directory, environment=False)
        if shared_settings is None
        else shared_settings,
        window=_config.overtake_window(),
        host=live_host() if host is None else host,
        estimate=estimator(directory) if estimate is None else estimate,
        p90=estimator(directory, p90=True),
        memory_estimate=memory_estimator(directory),
    )
