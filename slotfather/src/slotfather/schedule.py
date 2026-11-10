"""Slotfather schedule responsibilities."""
from __future__ import annotations

import contextlib
import json
import math
import os
from pathlib import Path
from typing import Any, Callable, Optional, cast
import re
import time

from . import config as _config
from . import state as _state


Entry = dict[str, Any]  # a queue entry, waiting or held, as the queue files hold it
Estimate = Callable[[Entry], Optional[float]]  # an entry's expected run in seconds; None when unknown


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


def overtakable(entry: dict, moment: float | None = None) -> bool:
    """Whether later requests that fit may still start before this waiter: it has waited less than the window."""
    queued = _config.seconds(entry.get("since"))
    return queued is not None and (time.time() if moment is None else moment) - queued < _config.overtake_window()


def order_settings(directory: Path | None = None, environment: bool = True) -> dict[str, Any]:
    """The queue's order and backfill settings: order.json beside the queue (`slotfather order`), else
    ORDER_DEFAULTS, with "saved" saying which. A client's environment only tightens them: SLOTFATHER_QUEUE=window
    falls back to the window order, a higher SLOTFATHER_BACKFILL_SAFETY or _FALLBACK, a lower _OVERRUN and a lower
    (nonzero) _LIMIT count; looser values are ignored, since every client's look grants for the whole queue. A file
    that cannot be read gives the window order."""
    found: dict[str, Any] = dict(_config.ORDER_DEFAULTS, saved=False)
    try:
        data = json.loads(((directory or _config.state_dir()) / "order.json").read_text())
        found.update(checked_order(data), saved=True)
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError, AttributeError):
        found.update(order="window", saved=True)
    if not environment:  # what every client shares; yield requests are decided on it alone
        return found
    if _config.setting("QUEUE") == "window":
        found["order"] = "window"
    for key, (name, least, tighten) in _config.ORDER_KNOBS.items():
        try:
            value = float(_config.setting(name) or "nan")
        except ValueError:
            continue
        if not math.isfinite(value) or value < least:
            continue
        if tighten == "up":
            found[key] = max(found[key], value)
        elif tighten == "down":
            found[key] = min(found[key], value)
        elif tighten == "delay":  # 0 asks nobody, the tightest
            found[key] = 0.0 if not value or not found[key] else max(found[key], value)
        elif value > 0:  # limit: 0 is no limit, the loosest
            found[key] = value if not found[key] else min(found[key], value)
    return found


def checked_order(data: Any) -> dict[str, Any]:
    """Saved order settings, validated: raises ValueError for anything `slotfather order` would refuse."""
    if not isinstance(data, dict) or set(data) - set(_config.ORDER_DEFAULTS) or data.get("order", "backfill") not in \
            _config.QUEUE_ORDERS:
        raise ValueError(f"not order settings: {data!r}")
    data = cast(dict[str, Any], data)
    found: dict[str, Any] = {}
    for key, value in data.items():
        if key != "order":
            least = _config.ORDER_KNOBS[key][1]
            if type(value) not in (int, float) or not math.isfinite(value) or value < least:
                raise ValueError(f"{key} must be a number of at least {least:g}, not {value!r}")
        found[key] = value
    return found



def queue_order() -> str:
    """backfill (the default) or window, the order before #643."""
    return order_settings()["order"]


def family(label: str | None) -> str:
    """A label without its ticket or agent id: lane-636-measure and lane-640-smoke-skip are both lane,
    agent-a17b708a4e705e421 is agent, sid-kit-wt-646 is sid-kit-wt."""
    words = (label or "").split("-")
    for at, word in enumerate(words):
        if at and any(c.isdigit() for c in word):
            return "-".join(words[:at])
    return label or ""


class Estimates:
    """Run times of finished requests, for how long a request will run: the median of the last ESTIMATE_RUNS runs
    of the closest kind that ran before (same label and command kind; same label family and command kind; the same
    family with a check list of about the same size; the command kind under any label; about the same size under
    any label; same label; same family), or None for a request like nothing before."""

    def __init__(self, runs=()):
        self.runs = {}
        for label, command, ran in runs:
            self.add(label, command, ran)

    @staticmethod
    def keys(label: str | None, command: str | None) -> list[tuple]:
        found = []
        if command:
            what = kind(command)
            # A list of checks keeps its size within a factor of two: 140 checks are not 4.
            rough = re.sub(r" \[(\d+)\]$", lambda m: f" [~{int(m[1]).bit_length()}]", what)
            found += [("label", label, what), ("family", family(label), what), ("family", family(label), rough),
                      ("kind", what), ("rough", rough)]
        return found + [("label", label), ("family", family(label))]

    def add(self, label: str | None, command: str | None, ran: float) -> None:
        for key in self.keys(label, command):
            recent = self.runs.setdefault(key, [])
            recent.append(float(ran))
            del recent[:-_config.ESTIMATE_RUNS]

    def save(self) -> dict:
        return {json.dumps(key): runs for key, runs in self.runs.items()}

    @classmethod
    def load(cls, saved: dict) -> "Estimates":
        found = cls()
        found.runs = {tuple(json.loads(key)): [float(v) for v in runs] for key, runs in saved.items()}
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
    runs = []
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




def no_estimate(entry: Entry) -> float | None:
    """An estimate for which every run is unknown."""
    return None


def deferring(entry: Entry) -> bool:
    """Whether a waiter defers to normal and interactive work (#5): it is batch, whatever its slot count, and also
    when it is back in the queue after a yield. While a normal or interactive request waits, it is never the head
    and holds that work back nowhere; once none waits it is the head as any other (others_wait())."""
    return _config.priority_of(entry) == "batch"


def whole_machine(entry: Entry, slots: int) -> bool:
    """Whether a waiter asks for every slot: `-n all`, or a count of at least the capacity."""
    return bool(entry.get("all")) or entry["count"] >= slots


def others_wait(waiters: list[Entry], entry: Entry, grants: list[Entry]) -> bool:
    """Whether a normal or interactive waiter other than `entry` waits in `waiters` and is not among `grants`: work
    that a deferring() waiter lets go first."""
    return any(other is not entry and not any(other is g for g in grants) and not deferring(other)
               for other in waiters)


def rank_time(entry: Entry, settings: dict[str, Any]) -> float:
    """When a waiter counts as queued: an interactive one `interactive` seconds before it came, a batch one `batch`
    seconds after. The offset is its ageing: a batch request that has waited longer than the two together is ahead
    of every interactive one that comes after. A lease that yielded counts as queued at its pinned `rank`, just
    behind the waiter it yielded to, whatever its class: it is half done and holds its memory meanwhile."""
    behind = entry.get("rank")  # a lease that yielded queues just behind the waiter it yielded to, and only there
    if isinstance(behind, (int, float)) and not isinstance(behind, bool) and math.isfinite(behind):
        return float(behind)
    queued = _config.seconds(entry.get("since"))
    queued = math.inf if queued is None else queued
    cls = _config.priority_of(entry)
    return queued - settings.get("interactive", 0.0) if cls == "interactive" else \
        queued + settings.get("batch", 0.0) if cls == "batch" else queued


def ranked(waiters: list[Entry], settings: dict[str, Any]) -> list[Entry]:
    """Waiters in the order the queue serves them: by rank_time(), then as they came (a stable sort)."""
    return sorted(waiters, key=lambda entry: rank_time(entry, settings))


class Plan:
    """What grant() does to the waiters: `grants` start now, in queue order; `reason` says why the first that does
    not start waits; `head` is the waiter the others must not delay, expected to start at `start` (epoch seconds)."""

    def __init__(self) -> None:
        self.grants: list[Entry] = []
        self.reason: str | None = None
        self.head: Entry | None = None
        self.start: float | None = None


def held_for(entry: Entry, moment: float) -> float:
    """How long a lease has run: since its first grant, less the time it spent yielded."""
    granted = _config.seconds(entry.get("granted"))
    return 0.0 if granted is None else max(0.0, moment - granted - float(entry.get("yielded") or 0))


def expected_end(entry: Entry, moment: float, run: float | None, fallback: float, overrun: float) -> float:
    """When a lease that holds slots is expected to end: its expected `run` (`fallback` when unknown) after its
    grant; one already past it runs on for `overrun` times what it has run so far, because a run that overran its
    kind tends to be a long one."""
    ran = held_for(entry, moment)
    expected = fallback if run is None else run
    return moment + max(expected - ran, overrun * ran)


def reserve(found: Plan, held: list[Entry], slots: int, used: int, moment: float, estimate: Estimate,
            fallback: float, overrun: float) -> int:
    """Sets the head's expected start: when, by the expected ends of what holds slots (`held` and the grants so
    far), enough slots will be free for it. Returns the slots spare then, those the head will not need even if the
    leases expected to run on past its start are the largest ones (estimates are rough, and which lease ends first
    is too)."""
    assert found.head is not None
    need: int = found.head["count"]
    ends = [(expected_end(entry, moment, estimate(entry), fallback, overrun), int(entry["count"]))
            for entry in held + found.grants]
    have, start = max(0, slots - used), moment
    for end, count in sorted(ends):
        if have >= need:
            break
        have, start = have + count, end
    found.start = start
    if have < need:  # slots held in an earlier queue, whose end is unknown: nothing passes
        found.start = moment
        return 0
    staying = sum(1 for end, _ in ends if end > start)
    largest = sum(sorted((count for _, count in ends), reverse=True)[:staying])
    other = used - sum(count for _, count in ends)  # held in an earlier queue
    return max(0, min(have, slots - other - largest) - need)


def soonest_start(count: int, holding: list[Entry], slots: int, used: int, moment: float, estimate: Estimate,
                  overrun: float) -> float | None:
    """When `count` slots are expected to be free if nothing more starts: by the expected ends of the leases in
    `holding`, a lease past its expected run running on for the overrun as reserve() counts it (#5). None when that
    is unknown: a lease whose run the history does not know (it may end any moment), or slots held in an earlier
    queue. It says whether a waiter can start at all before its client gives up."""
    ends: list[tuple[float, int]] = []
    for entry in holding:
        run = estimate(entry)
        if run is None:
            return None
        ends.append((expected_end(entry, moment, run, run, overrun), int(entry["count"])))
    have, start = max(0, slots - used), moment
    for end, size in sorted(ends):
        if have >= count:
            break
        have, start = have + size, end
    return start if have >= count else None


def fit(waiters: list[Entry], slots: int) -> list[Entry]:
    """Makes each waiter ask for at most the capacity `slots`, and an `-n all` one (its entry's `all`) for exactly
    it: a capacity that shrank under a waiter (a memory budget when a VM's balloon takes memory) would otherwise
    leave it waiting for slots that no longer exist. The change is the waiter's own entry's: a grant writes it and
    the holder takes its count from there. Only for the queue's whole capacity, not a share of it."""
    for entry in waiters:
        if entry.get("all") or entry["count"] > slots:
            entry["count"] = slots
    return waiters


def plan(waiters: list[Entry], held: list[Entry], slots: int, moment: float, order: str | None = None,
         estimate: Estimate | None = None, other: int = 0, reserve_head: bool = False,
         settings: dict[str, Any] | None = None) -> Plan:
    """Which of `waiters` (queue order) start now beside `held` (entries; `other` slots more held without one) on
    `slots` slots at epoch `moment`. `estimate(entry)` is its expected run in seconds or None (unknown).

    window: in arrival order; a waiter that does not fit lets later ones that fit start first until it has waited
    SLOTFATHER_OVERTAKE_WINDOW, then holds back everyone behind it.

    backfill: waiters in their class order (ranked(): an interactive one `interactive` seconds earlier than it came,
    a batch one `batch` seconds later, a yielded lease just behind the waiter it yielded to); the same within the
    window; the first waiter past it that does not fit is the head. Its start is
    reserved: the moment enough slots will be free, from the expected ends of the held leases. Until the head has
    waited `limit` seconds, a later waiter that fits starts before it when `safety` times its expected run ends by
    the head's start and by that limit, or when it takes only slots the head will not need then (reserve()); past
    the limit nothing passes. An unknown run counts `fallback` seconds; a held lease past its expected run is
    expected to run on for `overrun` times what it has run so far. A waiter whose client gives up (its entry's
    `until`, from `run --wait`) before the held leases are expected to free its slots, counting the overrun of
    those past their estimate (soonest_start(); a lease of unknown run keeps it the head), is no head, whether or
    not it has waited `limit`: it would leave without running, so it holds nobody back. Nor is a batch waiter
    (deferring()) while a normal or interactive one waits (others_wait()), and a batch one for the whole machine
    does not start then; once none waits it is the head as any other. `settings` are order_settings() unless
    given; `order` overrides theirs. The head's start is worked out only when a request behind it fits, or with
    `reserve_head`."""
    settings = settings or order_settings()
    order = order or settings["order"]
    estimate = estimate or no_estimate
    window = _config.overtake_window()
    if order == "backfill":
        waiters = ranked(waiters, settings)
    fallback, safety, overrun, limit = (float(settings[key]) for key in ("fallback", "safety", "overrun", "limit"))
    found = Plan()
    used = sum(e["count"] for e in held) + other
    extra = None  # slots free at the head's start that it does not need; reserved once a request behind it fits

    def quiet(free: int, entry: Entry) -> str:
        return f"{free} of {slots} slots free; {entry['label']} is batch and lets the normal and interactive " \
               "requests that wait go first"

    for entry in waiters:
        free = max(0, slots - used)
        if found.head is not None:
            if entry["count"] > free:
                continue
            if extra is None:
                extra = reserve(found, held, slots, used, moment, estimate, fallback, overrun)
            # A yielded head counts its limit from when it yielded: by its arrival the limit is long past.
            began = _state.epoch(found.head.get("yielded_at")) or _config.seconds(found.head.get("since")) or moment
            deadline = began + limit if limit else math.inf
            if moment >= deadline:
                continue  # the head has waited the limit: it holds back everyone behind it
            run = estimate(entry)
            run = fallback if run is None else run
            start = moment if found.start is None else found.start
            if moment + run * safety <= min(start, deadline):
                pass  # ends before the head can start, and before it has waited the limit
            elif entry["count"] <= extra:
                extra -= entry["count"]
            else:
                continue
            found.grants.append(entry)
            used += entry["count"]
            continue
        # A batch waiter defers while normal or interactive work waits (#5): it is no head then, and one for the
        # whole machine does not take it either, since that would shut the waiting work out for its whole run.
        defers = order == "backfill" and deferring(entry) and others_wait(waiters, entry, found.grants)
        if defers and entry["count"] <= free and whole_machine(entry, slots):
            found.reason = found.reason or quiet(free, entry)
            continue
        if entry["count"] <= free:
            found.grants.append(entry)
            used += entry["count"]
            continue
        queued = _config.seconds(entry.get("since"))
        passable = queued is not None and moment - queued < window
        if order != "backfill":
            if not passable:
                found.reason = found.reason or f"{free} of {slots} slots free"
                return found
        elif defers:
            found.reason = found.reason or quiet(free, entry)
            continue
        elif not passable:
            found.head = entry
            gives_up = None if entry.get("granted") else _config.seconds(entry.get("until"))
            if gives_up is not None:
                soonest = soonest_start(entry["count"], held + found.grants, slots, used, moment, estimate, overrun)
                if soonest is not None and soonest > gives_up:
                    # Its client gives up before the held leases are expected to free its slots: holding the
                    # others back would only leave slots idle until it leaves without running. Past its limit too
                    # (#5): nothing passes then, but the leases it waits for still run on.
                    found.head = None
                    clock = time.strftime("%H:%M:%S", time.localtime(gives_up))
                    found.reason = found.reason or (f"{free} of {slots} slots free; {entry['label']} gives up at "
                                                    f"{clock}, before they are expected, so requests that fit pass it")
                    continue
            found.reason = (f"{free} of {slots} slots free; {entry['label']} is next, and a later request that fits "
                            f"starts first only if it ends before {entry['label']} can start")
            if reserve_head:
                extra = reserve(found, held, slots, used, moment, estimate, fallback, overrun)
            continue
        until = time.strftime("%H:%M:%S", time.localtime(queued + window))
        found.reason = found.reason or (f"{free} of {slots} slots free; requests that fit may pass "
                                        f"{entry['label']} until {until}")
    return found


def yield_asks(waiters: list[Entry], held: list[Entry], slots: int, moment: float, estimate: Estimate | None = None,
               other: int = 0, settings: dict[str, Any] | None = None,
               asked: list[Entry] | tuple[Entry, ...] = ()) -> list[tuple[Entry, Entry]]:
    """Polite preemption (#643): (holder, waiter) pairs, the holders of `held` (entries, after this look's grants)
    that a waiter asks to yield now. Only an interactive waiter asks, once it has waited `yield_after` seconds, does
    not fit, and would not get its slots within `yield_after` by the held leases' expected ends; it asks only
    holders that opted in (`yieldable`), are of a lower class, have held their slots `yield_after` seconds since
    their last grant and were asked fewer than YIELDS_MAX times, and only as many as it needs (holders in `asked`,
    their ids, first; then the lowest class; then the one expected to run longest), and only when grant() would
    then start it, with the asked holders back among the waiters at their own rank. Nobody is ever stopped: a
    holder yields when its client gets round to it, or not at all."""
    settings = settings or order_settings()
    after = settings.get("yield_after") or 0
    if not after or settings["order"] != "backfill":
        return []
    estimate = estimate or no_estimate
    level = {name: n for n, name in enumerate(_config.PRIORITIES)}
    asked_ids = {id(entry) for entry in asked}
    holding: list[Entry] = list(held)
    found: list[tuple[Entry, Entry]] = []
    granted: list[Entry] = []
    fallback, overrun = float(settings["fallback"]), float(settings["overrun"])

    def ends(entry: Entry) -> float:
        return expected_end(entry, moment, estimate(entry), fallback, overrun)

    for entry in ranked(waiters, settings):
        if _config.priority_of(entry) not in _config.ASKERS or any(entry is g for g in granted):
            continue
        own = entry.get("yield_after")  # its client's environment asks later, or never (0)
        if isinstance(own, (int, float)) and not isinstance(own, bool):
            wait_for = max(after, float(own)) if own > 0 else None
        else:
            wait_for = after
        queued = _config.seconds(entry.get("since"))
        if wait_for is None or queued is None or moment - queued < wait_for:
            continue
        free = slots - other - sum(h["count"] for h in holding)
        if entry["count"] <= free:
            continue  # it fits: what holds it back is not slots
        have, when = free, moment
        for end, count in sorted((ends(h), h["count"]) for h in holding):
            if have >= entry["count"]:
                break
            have, when = have + count, end
        if have >= entry["count"] and when - moment <= after:
            continue  # its slots are expected soon anyway
        candidates = [h for h in holding if h.get("yieldable") and level[_config.priority_of(h)] > level[_config.priority_of(entry)]
                      and int(h.get("yields") or 0) < _config.YIELDS_MAX
                      and moment - (_config.seconds(h.get("resumed") or h.get("granted")) or moment) >= after]
        candidates.sort(key=lambda h: (id(h) not in asked_ids, -level[_config.priority_of(h)], -ends(h)))
        chosen: list[Entry] = []
        need = entry["count"] - free
        for h in candidates:
            if need <= 0:
                break
            chosen.append(h)
            need -= h["count"]
        if need > 0:
            continue
        behind = rank_time(entry, settings) + _config.YIELD_BEHIND
        back = [dict(h, state="waiting", rank=behind) for h in chosen]
        rest = [w for w in waiters if not any(w is g for g in granted)] + back
        trial = plan(rest, [h for h in holding if not any(h is c for c in chosen)], slots, moment, estimate=estimate,
                     other=other, settings=settings)
        if not any(g is entry for g in trial.grants):
            continue
        found += [(h, entry) for h in chosen]
        holding = [h for h in holding if not any(h is c for c in chosen)] + [entry]
        granted.append(entry)
    return found


def room(waiters: list[Entry], free: int, held: list[Entry] | None = None, estimate: Estimate | None = None,
         other: int = 0, settings: dict[str, Any] | None = None, label: str = "", command: str = "") -> int:
    """The slots a new request gets at once behind `waiters` with `free` slots unheld, as grant() admits it: one
    for `command` under `label`, whose run the estimates know, or else one of unknown run (the fallback). Without
    `held` (the entries holding slots; `other` slots more held without one), the window order's answer."""
    slots = free + sum(e["count"] for e in held or ()) + other
    if held is not None:  # the whole capacity: what grant() fits the waiters to
        fit(waiters, slots)
    moment = time.time()
    settings = settings or order_settings()
    order = settings["order"] if held is not None else "window"
    for count in range(free, 0, -1):
        probe: Entry = {"count": count, "label": label, "cmd": command, "since": _config.now()}
        if any(entry is probe for entry in plan(waiters + [probe], held or [], slots, moment, order, estimate,
                                                         other, settings=settings).grants):
            return count
    return 0


_estimates = {}  # history files' (size, mtime) -> Estimates, so a waiter's repeated looks read the history once


def cached_estimates(directory: Path) -> Estimates:
    """history_estimates(), kept in this process and in estimates.json beside the history, both keyed by the
    history files' size and modification time: a new process, a release or `available` reads the small file
    instead of the history until the history changes. A file another version wrote (its "v" differs) is worked out
    again, so runs that version counted (reaped leases, before version 2) do not linger until the history changes;
    an older client reads past the "v"."""
    path = directory / "history.jsonl"
    stamp = []
    for part in (path.with_name(path.name + ".1"), path):
        with contextlib.suppress(OSError):
            info = part.stat()
            stamp.append([part.name, info.st_size, info.st_mtime_ns])
    key = json.dumps(stamp)
    if key in _estimates:
        return _estimates[key]
    cache = directory / "estimates.json"
    found = None
    with contextlib.suppress(OSError, ValueError, TypeError, AttributeError, KeyError):
        saved = json.loads(cache.read_text())
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


def estimator(directory: Path) -> Estimate:
    """estimate(entry) from cached_estimates(), read only when plan() first asks (a head waits and a later
    request fits)."""
    def estimate(entry: Entry) -> float | None:
        return cached_estimates(directory).of(entry.get("label"), entry.get("cmd"))
    return estimate
