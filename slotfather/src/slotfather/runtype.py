"""Expected durations per run type, outliers and trends, for agents (fathers#106).

A run type is the project, `phase` and `plan` meta where a request carries a phase (Lanefather passes phase, plan
and checks, fathers#107); else its label and command kind, as the scheduler's estimates key them. A request's
expected run is the p50 and p90 of the last RECENT released runs of the most specific of its keys with at least
LEAST runs: project, phase and plan; project and phase; label and command kind; label.

At its release a run's history line records `run_key` and `verdict`: `normal` (at most the p90 of the runs before
it), `pending` (above it), or none with too few runs. A pending run's verdict is settled once SETTLE later runs of
its key have landed: `outlier` when most of them are normal again, `slower` when they are not. Settled verdicts go to
verdicts.jsonl beside the history (lease_id, verdict) and EVENTS (`run.outlier`); history lines are never rewritten.
A trend is the median of the last TREND_RECENT runs of a key against the TREND_BEFORE before them: `slower` or
`faster` beyond TREND_FACTOR, else `steady`; EVENTS get `run.trend` when a key's trend changes (trends.json), with
the plan change in that window where there is one.
"""
from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
from pathlib import Path
from typing import Any, Iterable, cast

from . import config as _config
from . import entry as _entry
from . import schedule as _schedule
from . import state as _state

RECENT = 20  # runs of a key that make its expected run
LEAST = 3  # fewer runs than this: the next, broader key
SETTLE = 3  # later runs that settle a pending verdict
TREND_RECENT, TREND_BEFORE, TREND_FACTOR = 10, 30, 1.25
TAIL = 4000  # history lines read
KEPT = TREND_RECENT + TREND_BEFORE  # runs kept per key in the cache
SEP = " · "
MARGIN, MARGIN_S = 0.1, 2.0
VERSION = 1


def percentile(values: list[float], q: float) -> float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    rank = (len(ordered) - 1) * q / 100
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def above(ran: float, p90: float | None) -> bool:
    """Whether a run is past its series' p90 by more than noise (MARGIN of it, and at least MARGIN_S seconds: runs
    are recorded in whole seconds)."""
    return p90 is not None and ran > max(p90 * (1 + MARGIN), p90 + MARGIN_S)


def project_of(item: dict[str, Any]) -> str:
    """The git repository the request ran in (`project`, which `run` records), else the first part of its derived
    label, else its label's family (an older client's or a library caller's request)."""
    if isinstance(item.get("project"), str) and item["project"]:
        return item["project"]
    derived = item.get("label_derived")
    if isinstance(derived, str) and derived:
        return derived.split(SEP)[0]
    return _schedule.family(item.get("label"))


def keys(item: dict[str, Any]) -> list[str]:
    """An entry's or history line's run-type keys, most specific first."""
    meta = _entry.meta_of(item)
    label = str(item.get("label") or "")
    found: list[str] = []
    phase = meta.get("phase")
    if phase:
        project = project_of(item)
        if meta.get("plan"):
            found.append(SEP.join((project, phase, "plan " + meta["plan"])))
        found.append(SEP.join((project, phase)))
    found += [SEP.join((label, _schedule.kind(item.get("cmd")))), label]
    return found


def trend_key(item: dict[str, Any]) -> str:
    """The key trends and `stats --trend` follow: project and phase where there is a phase, so that a plan change is
    seen within one series; else the most specific key."""
    found = keys(item)
    return found[1] if len(found) > 3 else found[0]


def finished(item: dict[str, Any]) -> bool:
    return item.get("outcome") == "released" and isinstance(item.get("ran"), (int, float)) \
        and not item.get("session") and bool(item.get("granted"))


def series(items: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Per key, its finished runs oldest first: {ran, ended, plan, checks, lease_id}."""
    found: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        if not finished(item):
            continue
        meta = _entry.meta_of(item)
        run = {"ran": float(item["ran"]), "ended": item.get("ended"), "plan": meta.get("plan"),
               "checks": meta.get("checks"), "lease_id": item.get("lease_id"), "verdict": item.get("verdict"),
               "run_key": item.get("run_key")}
        for key in keys(item):
            found.setdefault(key, []).append(run)
    return found


_cache: dict[str, dict[str, list[dict[str, Any]]]] = {}


def cached(directory: Path) -> dict[str, list[dict[str, Any]]]:
    """series() of the history's end, kept in this process and in runtypes.json, keyed by the history files' size and
    modification time (as schedule.cached_estimates()): a status look every second reads the small file."""
    path = directory / "history.jsonl"
    stamp: list[Any] = []
    for part in (path.with_name(path.name + ".1"), path):
        with contextlib.suppress(OSError):
            info = part.stat()
            stamp.append([part.name, info.st_size, info.st_mtime_ns])
    key = json.dumps(stamp)
    if key in _cache:
        return _cache[key]
    target = directory / "runtypes.json"
    found: dict[str, list[dict[str, Any]]] | None = None
    with contextlib.suppress(OSError, ValueError, TypeError, KeyError):
        saved = json.loads(target.read_text())
        if saved["stamp"] == stamp and saved.get("v") == VERSION:
            found = saved["series"]
    if found is None:
        found = {k: v[-KEPT:] for k, v in series(history(directory)).items()}
        if _state.foreign_owner(directory) is None:
            with contextlib.suppress(OSError):
                _state.write(target, {"v": VERSION, "stamp": stamp, "series": found})
    _cache.clear()
    _cache[key] = found
    return found


def history(directory: Path) -> list[dict[str, Any]]:
    """The last TAIL lines of the history, across its rotation."""
    path = directory / "history.jsonl"
    found = _state.tail_objects(path, TAIL)
    if len(found) < TAIL:
        found = _state.tail_objects(path.with_name(path.name + ".1"), TAIL - len(found)) + found
    return found


def peek(directory: Path) -> dict[str, list[dict[str, Any]]]:
    """The series as last worked out, never rebuilt: for looks under the queue lock (the scheduler's estimates),
    where reading the history would hold every client up. Slightly stale is fine there."""
    if _cache:
        return next(iter(_cache.values()))
    with contextlib.suppress(OSError, ValueError, TypeError, KeyError):
        saved = json.loads((directory / "runtypes.json").read_text())
        if saved.get("v") == VERSION and isinstance(saved.get("series"), dict):
            return saved["series"]
    return {}


def expected_of(item: dict[str, Any], known: dict[str, list[dict[str, Any]]]) -> dict[str, Any] | None:
    """{"key", "p50", "p90", "runs", "fallback"} by the most specific key with LEAST runs, else None."""
    chain = keys(item)
    for at, key in enumerate(chain):
        runs = [r["ran"] for r in known.get(key, [])][-RECENT:]
        if len(runs) >= LEAST:
            p50, p90 = percentile(runs, 50), percentile(runs, 90)
            return {"key": key, "p50": None if p50 is None else round(p50, 1),
                    "p90": None if p90 is None else round(p90, 1), "runs": len(runs), "fallback": at > 0}
    return None


def verdict_at_release(item: dict[str, Any], directory: Path) -> dict[str, Any]:
    """`run_key` and `verdict` for a history line about to be written (the runs before it are in the history)."""
    with contextlib.suppress(Exception):
        found = expected_of(item, cached(directory))
        if found is None:
            return {"run_key": keys(item)[0]}
        ran = item.get("ran")
        verdict = None if not isinstance(ran, (int, float)) else "pending" if above(float(ran), found["p90"]) \
            else "normal"
        return {"run_key": found["key"], "verdict": verdict} if verdict else {"run_key": found["key"]}
    return {}


def settled(directory: Path) -> dict[str, str]:
    """lease_id -> settled verdict, from verdicts.jsonl."""
    return {str(v.get("lease_id")): str(v.get("verdict")) for v in _state.journal(directory / "verdicts.jsonl")
            if v.get("lease_id")}


def plan_change(runs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The last change of plan among `runs` (oldest first): when, from and to, and the change in checks."""
    for at in range(len(runs) - 1, 0, -1):
        before, after = runs[at - 1], runs[at]
        if after.get("plan") != before.get("plan"):
            change: dict[str, Any] = {"at": after.get("ended"), "from": before.get("plan"), "to": after.get("plan")}
            with contextlib.suppress(TypeError, ValueError):
                change.update(checks_from=int(before["checks"]), checks_to=int(after["checks"]),
                              checks=int(after["checks"]) - int(before["checks"]))
            return change
    return None


def trend(runs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The trend of a key's runs (oldest first), or None with too few: the median of the last TREND_RECENT against
    the TREND_BEFORE before them, its verdict, and the plan change in that window with words for it."""
    if len(runs) < TREND_RECENT * 2:
        return None
    recent, before = runs[-TREND_RECENT:], runs[-(TREND_RECENT + TREND_BEFORE):-TREND_RECENT]
    now, then = percentile([r["ran"] for r in recent], 50), percentile([r["ran"] for r in before], 50)
    if now is None or then is None or then <= 0:
        return None
    ratio = now / then
    verdict = "slower" if ratio >= TREND_FACTOR else "faster" if ratio <= 1 / TREND_FACTOR else "steady"
    change = plan_change(runs[-(TREND_RECENT + TREND_BEFORE):])
    if change is None:
        why = "same plan" if recent[-1].get("plan") else "no plan recorded"
    elif isinstance(change.get("checks"), int) and change["checks"]:
        why = f"plan {'grew' if change['checks'] > 0 else 'shrank'}: {change['checks']:+d} checks"
    else:
        why = "plan changed"
    return {"verdict": verdict, "p50_recent": round(now, 1), "p50_before": round(then, 1), "ratio": round(ratio, 2),
            "recent": len(recent), "before": len(before), "plan_change": change, "why": why}


def after_release(directory: Path, item: dict[str, Any]) -> None:
    """Settles pending verdicts of the released run's key and reports a changed trend; outside the queue lock, under
    a lock of its own (runtypes.lock). Never raises."""
    from . import events as _events
    with contextlib.suppress(Exception):
        key = item.get("run_key")
        if not key or not finished(item):
            return
        with open(directory / "runtypes.lock", "a") as lock:
            try:  # one client at a time; a busy lock is skipped: the next release settles what is left
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            known = series(history(directory))
            runs = known.get(str(key), [])
            done = settled(directory)
            for at, run in enumerate(runs):
                if run.get("verdict") != "pending" or not run.get("lease_id") or run["lease_id"] in done \
                        or run.get("run_key") != key:  # judged against another series: settled there
                    continue
                later = runs[at + 1:at + 1 + SETTLE]
                if len(later) < SETTLE:
                    continue
                base = [r["ran"] for r in runs[max(0, at - RECENT):at]]
                p50, p90 = percentile(base, 50), percentile(base, 90)
                if p90 is None:
                    continue
                normal = sum(1 for r in later if not above(r["ran"], p90))
                verdict = "outlier" if normal * 2 > len(later) else "slower"
                _state.append_bounded(directory / "verdicts.jsonl", json.dumps(
                    {"lease_id": run["lease_id"], "verdict": verdict, "key": key, "ran": run["ran"],
                     "at": _config.now()}) + "\n", _config.HISTORY_MAX)
                if verdict == "outlier":
                    _events.emit(directory, "run.outlier", {"type": "run_type", "id": str(key)},
                                 {"lease_id": run["lease_id"], "ran": run["ran"], "p50": round(p50 or 0, 1),
                                  "p90": round(p90, 1), "ended": run.get("ended"),
                                  "later": [round(r["ran"], 1) for r in later]},
                                 _events.actor(kind="tool"), level="warn")
            key = trend_key(item)
            found = trend(known.get(key, []))
            if found is None:
                return
            path = directory / "trends.json"
            trends: dict[str, Any] = {}
            with contextlib.suppress(OSError, ValueError):
                saved: object = json.loads(path.read_text())
                if isinstance(saved, dict):
                    trends = cast(dict[str, Any], saved)
            if trends.get(key) == found["verdict"]:
                return
            first = key not in trends
            trends[key] = found["verdict"]
            _state.write(path, trends)
            if first and found["verdict"] == "steady":
                return  # nothing to say about a key's first steady trend
            _events.emit(directory, "run.trend", {"type": "run_type", "id": str(key)},
                         {k: found[k] for k in ("verdict", "p50_recent", "p50_before", "ratio", "recent", "before",
                                                "plan_change", "why")}, _events.actor(kind="tool"),
                         level="warn" if found["verdict"] == "slower" else None)


def week(stamp: Any) -> str | None:
    moment = _config.seconds(stamp)
    if moment is None:
        return None
    year, number, _ = datetime.datetime.fromtimestamp(moment, datetime.timezone.utc).isocalendar()
    return f"{year}-W{number:02d}"


def report(directory: Path | None = None, since: float | None = None, project: str | None = None,
           phase: str | None = None) -> dict[str, Any]:
    """`stats --trend --json`: per key, weekly p50/p90/count, plan changes with their dates, and the trend."""
    directory = directory or _config.state_dir()
    items = [i for i in _state.history_items(directory)
             if since is None or (_config.seconds(i.get("ended")) or 0) >= since]
    done = settled(directory)
    keyed: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        if not finished(item):
            continue
        meta = _entry.meta_of(item)
        if project is not None and project_of(item) != project:
            continue
        if phase is not None and meta.get("phase") != phase:
            continue
        keyed.setdefault(trend_key(item), []).append(item)
    rows: list[dict[str, Any]] = []
    for key, members in sorted(keyed.items()):
        weeks: dict[str, list[float]] = {}
        for item in members:
            weeks.setdefault(week(item.get("ended")) or "?", []).append(float(item["ran"]))
        runs = series(members).get(key, [])
        changes: list[dict[str, Any]] = []
        for at in range(1, len(runs)):
            if runs[at].get("plan") != runs[at - 1].get("plan"):
                change = plan_change(runs[at - 1:at + 1])
                if change:
                    changes.append(change)
        verdicts: dict[str, int] = {}
        for item in members:
            verdict = done.get(str(item.get("lease_id"))) or item.get("verdict")
            if verdict:
                verdicts[str(verdict)] = verdicts.get(str(verdict), 0) + 1
        meta = _entry.meta_of(members[-1])
        rows.append({"key": key, "project": project_of(members[-1]), "phase": meta.get("phase"),
                     "plan": meta.get("plan"), "checks": meta.get("checks"), "runs": len(members),
                     "weeks": [{"week": w, "count": len(v), "p50": round(percentile(v, 50) or 0, 1),
                                "p90": round(percentile(v, 90) or 0, 1)} for w, v in sorted(weeks.items())],
                     "plan_changes": changes, "trend": trend(runs), "verdicts": verdicts})
    return {"v": 1, "since": since, "project": project, "phase": phase, "keys": rows}


def report_text(found: dict[str, Any]) -> str:
    lines: list[str] = []
    for row in found["keys"]:
        t = row["trend"]
        last = row["weeks"][-1] if row["weeks"] else None
        lines.append(f"{row['key']}: {row['runs']} runs" + (f", last week p50 {last['p50']:g} s p90 {last['p90']:g} s"
                                                            if last else "")
                     + (f"; trend {t['verdict']} ({t['p50_before']:g} -> {t['p50_recent']:g} s, {t['why']})"
                        if t else "; too few runs for a trend"))
    return "\n".join(lines) or "no finished runs in range"


def entry_view(entry: dict[str, Any], known: dict[str, list[dict[str, Any]]], moment: float) -> dict[str, Any]:
    """`expected` (and `overrun`) for a status entry."""
    found = expected_of(entry, known)
    view: dict[str, Any] = {"expected": found}
    if found is not None and _entry.is_held(entry) and found["p90"] is not None:
        view["overrun"] = above(_entry.held_for(entry, moment), found["p90"])
    return view
