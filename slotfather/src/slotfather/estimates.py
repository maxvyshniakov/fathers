"""Run-time estimates (fathers#35): how long a request is expected to run, from the history's finished runs of
the closest kind (Estimates), kept in estimates.json beside the history, and the one estimator() the scheduler is
handed. A run type's expectations (runtype.py, fathers#106) stay their own module and cache; estimator() asks them
first for a request that names a phase. The scheduler reads none of this itself: its callers hand it an estimate.

This module loads only when an estimate is first asked (leases.estimator()): `run` on a queue with nothing ahead of
it never does, which keeps the admission path's imports as they were."""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import re
from typing import Any, Iterable, Optional, cast

from . import config as _config
from . import entry as _entry
from . import runtype as _runtype
from . import schedule as _schedule
from . import state as _state


Key = tuple[Optional[str], ...]  # ("label", label, kind), ("family", family, kind), ("kind", kind), ...
Run = tuple[Optional[str], Optional[str], float]  # (label, command, seconds it ran)


class Estimates:
    """Run times of finished requests, for how long a request will run: the median of the last ESTIMATE_RUNS runs
    of the closest kind that ran before (same label and command kind; same label family and command kind; the same
    family with a check list of about the same size; the command kind under any label; about the same size under
    any label; same label; same family), or None for a request like nothing before."""

    def __init__(self, runs: Iterable[Any] = ()) -> None:  # Run tuples, as the history or a replay gives them
        self.runs: dict[Key, list[float]] = {}
        for label, command, ran in cast(Iterable[Run], runs):
            self.add(label, command, ran)

    @staticmethod
    def keys(label: str | None, command: str | None) -> list[Key]:
        found: list[Key] = []
        if command:
            what = _runtype.kind(command)
            # A list of checks keeps its size within a factor of two: 140 checks are not 4.
            rough = re.sub(r" \[(\d+)\]$", lambda m: f" [~{int(m[1]).bit_length()}]", what)
            found += [("label", label, what), ("family", _runtype.family(label), what), ("family", _runtype.family(label), rough),
                      ("kind", what), ("rough", rough)]
        return found + [("label", label), ("family", _runtype.family(label))]

    def add(self, label: str | None, command: str | None, ran: float) -> None:
        for key in self.keys(label, command):
            recent = self.runs.setdefault(key, [])
            recent.append(float(ran))
            del recent[:-_config.ESTIMATE_RUNS]

    def save(self) -> dict[str, list[float]]:
        return {json.dumps(key): runs for key, runs in self.runs.items()}

    @classmethod
    def load(cls, saved: dict[str, Any]) -> "Estimates":
        found = cls()
        found.runs = {tuple(cast(list[Any], json.loads(key))): [float(v) for v in cast(list[Any], runs)]
                      for key, runs in saved.items()}
        return found

    def of(self, label: str | None, command: str | None) -> float | None:
        for key in self.keys(label, command):
            recent = self.runs.get(key)
            if recent:
                ordered = sorted(recent)
                middle = len(ordered) // 2
                return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2
        return None


def history_estimates(directory: Path) -> Estimates:
    """Estimates from the end of history.jsonl (ESTIMATE_BYTES of it): every request that was granted and ran to its
    release. A reaped lease is left out: its holder died, and its time runs to whenever a look noticed, such as an
    orphan of a hung CI job that kept its slots for most of an hour; so is one that gave up (`left`). Requests whose
    holder ran a slotfather that records no history (before #635) are not in it."""
    path = directory / "history.jsonl"
    runs: list[Run] = []
    for part in (path.with_name(path.name + ".1"), path):
        try:
            with open(part, "rb") as handle:
                end = handle.seek(0, os.SEEK_END)
                handle.seek(max(0, end - _config.ESTIMATE_BYTES))
                lines = handle.read().splitlines()
        except FileNotFoundError:
            continue
        for item in _state.objects(lines[1:] if end > _config.ESTIMATE_BYTES else lines):
            if isinstance(item.get("ran"), (int, float)) and item.get("granted") and \
                    item.get("outcome") not in _config.UNFINISHED:
                runs.append((item.get("label"), item.get("cmd"), item["ran"]))
    return Estimates(runs)


_estimates: dict[str, Estimates] = {}  # history files' (size, mtime) -> Estimates, so a waiter's repeated looks read the history once


def cached_estimates(directory: Path) -> Estimates:
    """history_estimates(), kept in this process and in estimates.json beside the history, both keyed by the
    history files' size and modification time: a new process, a release or `available` reads the small file
    instead of the history until the history changes. A file another version wrote (its "v" differs) is worked out
    again, so runs that version counted (reaped leases, before version 2) do not linger until the history changes;
    an older client reads past the "v"."""
    path = directory / "history.jsonl"
    stamp: list[list[Any]] = []
    for part in (path.with_name(path.name + ".1"), path):
        with contextlib.suppress(OSError):
            info = part.stat()
            stamp.append([part.name, info.st_size, info.st_mtime_ns])
    key = json.dumps(stamp)
    if key in _estimates:
        return _estimates[key]
    cache = directory / "estimates.json"
    found: Estimates | None = None
    with contextlib.suppress(OSError, ValueError, TypeError, AttributeError, KeyError):
        saved: dict[str, Any] = json.loads(cache.read_text())
        if saved["stamp"] == stamp and saved.get("v") == _config.ESTIMATES_VERSION:
            found = Estimates.load(saved["runs"])
    if found is None:
        found = history_estimates(directory)
        if _state.foreign_owner(directory) is None:
            with contextlib.suppress(OSError):
                _state.write(cache, {"v": _config.ESTIMATES_VERSION, "stamp": stamp, "runs": found.save()})
    _estimates.clear()
    _estimates[key] = found
    return found


def estimator(directory: Path) -> _schedule.Estimate:
    """estimate(entry) from cached_estimates(), read only when plan() first asks (a head waits and a later
    request fits)."""
    def estimate(entry: _schedule.Entry) -> float | None:
        spec = entry.get("session")
        expect = cast(dict[str, Any], spec).get("expect") if isinstance(spec, dict) else None
        if isinstance(expect, (int, float)) and not isinstance(expect, bool) and expect > 0:
            return float(expect)  # a session's own estimate (`session start --expect`) before its kind's history
        if _entry.meta_of(entry).get("phase"):  # a run type (fathers#106): its project, phase and plan
            with contextlib.suppress(Exception):
                from . import runtype as _runtype
                found = _runtype.expected_of(entry, _runtype.peek(directory))  # under the lock: never rebuilt
                if found is not None and found["p50"] is not None and not found["fallback"]:
                    return float(found["p50"])
        return cached_estimates(directory).of(entry.get("label"), entry.get("cmd"))
    return estimate
