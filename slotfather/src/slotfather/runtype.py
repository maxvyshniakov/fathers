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
import math
import os
from pathlib import Path
import re
from typing import Any, Iterable, cast

from . import config as _config
from . import entry as _entry
from . import settings as _settings
from . import state as _state


# The names the run-time estimates group requests by (here, estimates.py, stats, tools/replay.py)

def kind(command: str | None) -> str:
    """The kind of check a command is: its program's name and first argument; a comma list counts its items, a path
    keeps its last part, an option keeps its name, and a short option value follows it. `env` and VAR=value before
    the program go; a runner's `run` keeps what it runs (`bun run build`, `uv run pytest`), and a shell's `-c`
    script is the kind of the script's command (`sh -c bun run build`)."""
    words = [w for w in (command or "").split()]
    while words and (re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]) or os.path.basename(words[0]) == "env"):
        words.pop(0)  # env and VAR=value before the program
    if not words:
        return "?"
    found = [os.path.basename(words[0]) or words[0]]
    rest = words[1:]
    if rest and found[0] in _config.SHELLS and re.fullmatch(r"-[a-z]*c", rest[0]) and len(rest) > 1:
        return f"{found[0]} {rest[0]} {kind(' '.join(rest[1:]))}"[:60]
    if len(rest) > 1 and rest[0] == "run" and not rest[1].startswith("-"):
        return f"{found[0]} run {os.path.basename(rest[1].rstrip('/')) or rest[1]}"[:60]
    if rest:
        first = rest[0]
        if first.startswith("-"):
            found.append(first.split("=", 1)[0])
            value = rest[1] if len(rest) > 1 and "=" not in first else ""
            if value and not value.startswith("-"):
                if "," in value:
                    found.append(f"[{len([v for v in value.split(',') if v])}]")
                elif len(value) <= 40 and "=" not in value:
                    found.append(os.path.basename(value.rstrip("/")) or value)
        elif "," in first:
            found.append(f"[{len([v for v in first.split(',') if v])}]")
        else:
            found.append(os.path.basename(first.rstrip("/")) or first)
    return " ".join(found)[:60]


def family(label: str | None) -> str:
    """A label without its ticket or agent id: lane-636-measure and lane-640-smoke-skip are both lane,
    agent-a17b708a4e705e421 is agent, sid-kit-wt-646 is sid-kit-wt."""
    label = (label or "").split(" · ")[0]  # a derived label (labels.py): its project, then the old rule
    words = label.split("-")
    for at, word in enumerate(words):
        if at and any(c.isdigit() for c in word):
            return "-".join(words[:at])
    return label or ""

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
    """The git repository the request ran in (`project`, which `run` records); else, for a label the caller gave,
    its family (an older client's or a library caller's request, or one outside any repository); else "" (fathers#106
    review): a label derived outside any repository begins with the command's name ("true · check"), so neither it
    nor its family may name the series, or one request's run type would change with its command."""
    if isinstance(item.get("project"), str) and item["project"]:
        return item["project"]
    derived = item.get("label_derived")
    if isinstance(derived, str) and derived and item.get("label") == derived:
        return ""  # its label is the derived one: no project, and the label names the command
    return family(item.get("label"))


def keys(item: dict[str, Any]) -> list[str]:
    """An entry's or history line's run-type keys, most specific first."""
    meta = _entry.meta_of(item)
    label = str(item.get("label") or "")
    found: list[str] = []
    phase = meta.get("phase")
    if phase:
        project = project_of(item)
        head = (project, phase) if project else (phase,)  # no project: the phase alone names the series
        if meta.get("plan"):
            found.append(SEP.join((*head, "plan " + meta["plan"])))
        found.append(SEP.join(head))
    found += [SEP.join((label, kind(item.get("cmd")))), label]
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
    modification time (as estimates.cached_estimates()): a status look every second reads the small file."""
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
                    _events.record(directory, None, "run.outlier", {"type": "run_type", "id": str(key)},
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
            changed = cast(_events.Trend, {k: found[k] for k in ("verdict", "p50_recent", "p50_before", "ratio", "recent",
                                                                 "before", "plan_change", "why")})
            _events.record(directory, None, "run.trend", {"type": "run_type", "id": str(key)}, changed,
                           _events.actor(kind="tool"), level="warn" if found["verdict"] == "slower" else None)


def week(stamp: Any) -> str | None:
    moment = _config.seconds(stamp)
    if moment is None:
        return None
    year, number, _ = datetime.datetime.fromtimestamp(moment, datetime.UTC).isocalendar()
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


# Slots asked against cores used, per label family (fathers#97; was askuse.py, folded here by fathers#37 review so
# that `run` loads no more modules): `stats` reports it, `run` hints when a request asks for far more than its
# family's recent runs used and warns when it asks for far less (fathers#148), and `run --cores` / `advise` turn
# cores into the slots that hold them.

ASK_RECENT = 20  # a family's last runs that count for the hint
ASK_LEAST = 5  # fewer measured runs than this give no hint
ASK_SHARE = 0.5  # a family "asks far above use" when its median use is at most this share of the cores it asks
ASK_TAIL = 4000  # history lines read for the hint


def ask_median(values: list[float]) -> float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def per_slot(item: dict[str, Any], fallback: float) -> float:
    """The cores one slot held when the request ran: its line's cores over its capacity, else `fallback`."""
    cores, capacity = item.get("cores"), item.get("capacity")
    if isinstance(cores, int) and isinstance(capacity, int) and cores > 0 and capacity > 0:
        return cores / capacity
    return fallback


def slots_for(cores: float, width: float, total: int) -> int:
    """The slots that hold `cores` cores at `width` cores per slot: at least one, at most the capacity `total`."""
    if not width > 0 or not math.isfinite(cores):
        return 1
    limit = max(1, total)
    if cores >= width * limit:
        return limit
    return max(1, min(limit, math.ceil(round(cores / width, 6))))


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def used(item: dict[str, Any]) -> float | None:
    """Mean cores a released, measured request used while it held its slots: CPU over wall less yielded time. None
    for an all-slot request or a session, which ask for the machine to themselves rather than for cores."""
    cpu = _finite_number(item.get("cpu"))
    if item.get("outcome") != "released" or cpu is None:
        return None
    capacity, count = item.get("capacity"), item.get("count")
    if item.get("session") or (isinstance(capacity, int) and not isinstance(capacity, bool)
                               and isinstance(count, int) and not isinstance(count, bool)
                               and capacity > 1 and count >= capacity):
        return None
    wall = _finite_number(item.get("wall"))
    if wall is None:
        wall = _finite_number(item.get("ran"))
    if wall is None:
        return None
    away = _finite_number(item.get("yielded")) or 0.0
    held = wall - away
    return cpu / held if held > 0 else None


def family_row(name: str, items: list[dict[str, Any]], fallback: float) -> dict[str, Any]:
    """One family's measured runs: median slots asked, median and p90 cores used, median cores asked (slots times
    the cores per slot then), the slots that median use needs, whether that use is at most ASK_SHARE of the ask
    (`over_ask`), and whether it is at least CORES_OVER times the ask in more slots (`under_ask`, fathers#148)."""
    measured = [(item, use) for item in items for use in [used(item)]
                if use is not None and isinstance(item.get("count"), int) and item["count"] >= 1]
    slots = ask_median([float(item["count"]) for item, _ in measured])
    cores = ask_median([use for _, use in measured])
    high = percentile([use for _, use in measured], 90)
    asked = ask_median([item["count"] * per_slot(item, fallback) for item, _ in measured])
    width = ask_median([per_slot(item, fallback) for item, _ in measured])
    needed = None if cores is None or not width else max(1, math.ceil(round(cores / width, 6)))
    over = under = False
    if measured and asked and cores is not None and needed is not None and slots is not None:
        over = needed < slots and cores <= ASK_SHARE * asked
        under = needed > slots and cores >= _config.CORES_OVER * asked
    return {"family": name, "measured": len(measured), "slots_p50": slots,
            "cores_p50": None if cores is None else round(cores, 2),
            "cores_p90": None if high is None else round(high, 2),
            "cores_asked_p50": None if asked is None else round(asked, 2), "slots_needed": needed,
            "over_ask": over, "under_ask": under}


def by_family(items: Iterable[dict[str, Any]], fallback: float) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        groups.setdefault(family(item.get("label")), []).append(item)
    rows = [family_row(name, members, fallback) for name, members in groups.items()]
    rows.sort(key=lambda r: (not (r["over_ask"] or r["under_ask"]), -r["measured"], r["family"]))
    return rows


def machine_width() -> tuple[float, int] | None:
    """(cores per slot, slots) of this machine now, or None when the capacity cannot be read."""
    try:
        total = max(1, _settings.total())
        return _config.cores() / total, total
    except (_config.SlotError, OSError, ValueError):
        return None


def learned(directory: Path, label: str | None, width: float) -> dict[str, Any] | None:
    """family_row() of the last ASK_RECENT measured runs of `label`'s family (`width`: cores per slot where a line
    lacks its own), or None with fewer than ASK_LEAST."""
    name = family(label)
    try:
        lines = _state.tail_objects(directory / "history.jsonl", ASK_TAIL)
    except OSError:
        return None
    recent = [item for item in lines
              if family(item.get("label")) == name and used(item) is not None
              and isinstance(item.get("count"), int) and not isinstance(item.get("count"), bool)
              and item["count"] >= 1]
    recent = recent[-ASK_RECENT:]
    if len(recent) < ASK_LEAST:
        return None
    return family_row(name, recent, width)


def hint(directory: Path, label: str, count: int) -> str | None:
    """One line for a request of `count` slots under `label` from its family's last ASK_RECENT measured runs (at
    least ASK_LEAST): a hint when their median use was at most ASK_SHARE of the cores `count` slots hold, in fewer
    slots; a warning when it was at least CORES_OVER times those cores, in more slots that the capacity has
    (fathers#148); else None."""
    found = machine_width()
    if found is None:
        return None
    width, total = found
    row = learned(directory, label, width)
    if row is None:
        return None
    name, needed, cores, high = row["family"], row["slots_needed"], row["cores_p50"], row["cores_p90"]
    asked = count * width
    if needed is None or cores is None:
        return None
    if count > 1 and needed < count and cores <= ASK_SHARE * asked:
        return (f"hint: {name} requests used a median {cores:.1f} cores over their last {row['measured']} runs; "
                f"{count} slots hold {asked:g} cores, {needed} would hold that use (-n {needed})")
    weighted = slots_for(cores, width, total)
    if count < total and weighted > count and cores >= _config.CORES_OVER * asked:
        return (f"warning: {name} requests used a median {cores:.1f} cores (p90 {high:.1f}) over their last "
                f"{row['measured']} runs; {count} slot{'s' if count > 1 else ''} hold{'' if count > 1 else 's'} "
                f"{asked:g} cores, {weighted} would hold that use (-n {weighted}, or --cores auto)")
    return None


def weight(directory: Path, label: str | None, declared: float | None = None) -> dict[str, Any]:
    """The slots a request under `label` should ask for by the cores it uses (fathers#148): for `declared` cores
    when given (`source` "declared"), else for its family's median measured use ("history"), else one ("default").
    With the family's measured runs, as family_row() gives them (`measured` 0 and nulls without)."""
    found = machine_width()
    width, total = found if found is not None else (float(_config.cores()), 1)
    row = learned(directory, label, width)
    if declared is not None:
        cores, source = float(declared), "declared"
    elif row is not None and row["cores_p50"] is not None:
        cores, source = float(row["cores_p50"]), "history"
    else:
        cores, source = None, "default"
    return {"slots": 1 if cores is None else slots_for(cores, width, total),
            "cores": None if cores is None else round(cores, 2), "source": source,
            "cores_per_slot": round(width, 2), "family": family(label),
            "measured": row["measured"] if row else 0,
            **{key: row[key] if row else None for key in ("slots_p50", "cores_p50", "cores_p90", "slots_needed")},
            "over_ask": bool(row and row["over_ask"]), "under_ask": bool(row and row["under_ask"])}
