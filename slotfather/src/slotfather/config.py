"""Slotfather config responsibilities."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
import re



MIN_PYTHON = (3, 9)


DEFAULT_DIR = "~/.local/state/slotfather"


DEFAULT_CGROUP = None  # install.py --cgroup writes the queue's cgroup here, so every client of the queue names it


DEFAULT_LEGACY_DIRS = "~/.local/state/straylight-verification/slots"


LEGACY_TOTAL = 4  # the earlier queue's fixed slot count


CORES_PER_SLOT = 3


AUTO_GIB_PER_SLOT = 4.0


CAPACITY_MODES = ("memory", "lower")  # saved in capacity.json beside "slots", which older clients read alone


MIN_FREE_SWAP_MB = 512


MAX_PRESSURE_LEVEL = 1  # kern.memorystatus_vm_pressure_level: 1 normal, 2 warning, 4 critical


CRITICAL_PRESSURE_LEVEL = 4  # at or above: no new lease beside running ones; between: memory_level() warning


MIN_AVAILABLE_MB = 1024


MAX_PSI_SOME = 10.0


MAX_PSI_FULL = 5.0


POLICIES = ("normal", "background", "bypass")


TIMED_OUT = 75  # EX_TEMPFAIL


POLL_MAX = 10  # seconds between a long waiter's full looks at the queue


OVERTAKE_WINDOW = 600  # seconds a waiter that does not fit lets later requests that fit start first


QUEUE_ORDERS = ("backfill", "window")  # `slotfather order`; the first is the default


ORDER_DEFAULTS = {"order": "backfill", "safety": 3.0, "limit": 0.0, "fallback": 3600.0, "overrun": 0.5,
                  "interactive": 1800.0, "batch": 1800.0, "yield_after": 120.0, "idle_cpu": 0.1, "idle_after": 300.0,
                  "aging_batch": 1800.0, "aging_normal": 3600.0, "warn_wait": 1800.0, "warning_admits": 1.0}


ORDER_KNOBS = {"safety": ("BACKFILL_SAFETY", 1.0, "up"), "limit": ("BACKFILL_LIMIT", 0.0, "limit"),
               "fallback": ("BACKFILL_FALLBACK", 0.0, "up"), "overrun": ("BACKFILL_OVERRUN", 0.0, "down"),
               "interactive": ("PRIORITY_INTERACTIVE", 0.0, "down"), "batch": ("PRIORITY_BATCH", 0.0, "down"),
               "yield_after": ("YIELD_AFTER", 0.0, "delay"),
               # Idle leases (#12): below idle_cpu cores for idle_after seconds. A client's environment tightens them
               # by finding fewer leases idle: a lower threshold, a longer duration.
               "idle_cpu": ("IDLE_CPU", 0.0, "down"), "idle_after": ("IDLE_AFTER", 1.0, "up"),
               # Ageing: a waiter moves up a class after waiting this long in its class (batch -> normal ->
               # interactive); 0: never. Like the class offsets, a client's environment can only move requests less:
               # a longer wait, or 0.
               "aging_batch": ("AGING_BATCH", 0.0, "delay"), "aging_normal": ("AGING_NORMAL", 0.0, "delay"),
               # How long a waiter waits before its client says so (stderr, queue.log) and status flags it; 0: never.
               # It decides nothing for other clients, so a client's environment sets it either way.
               "warn_wait": ("WARN_WAIT", 0.0, "own"),
               # Under memory-pressure warning, 1: one 1-slot lease at a time starts beside running ones; 0: none (the
               # rule before it). A client's environment can only turn it off.
               "warning_admits": ("WARNING_ADMITS", 0.0, "down")}


DURATION_KNOBS = ("limit", "fallback", "interactive", "batch", "yield_after", "idle_after", "aging_batch",
                  "aging_normal", "warn_wait")  # `slotfather order` takes these as 90, 90s, 30m or 1.5h


WARN_REPEAT = 900  # seconds between a long waiter's repeated "waiting ..." lines once it has waited warn_wait


# Always saved; the rest only when not the default. `limit` left the base when its default went from 2700 to 0:
# a file without it gives an older client its own default (2700), the bound it has without reservations.
ORDER_BASE = ("order", "safety", "fallback", "overrun")


PRIORITIES = ("interactive", "normal", "batch")


YIELDS_MAX = 2  # times one lease is asked to yield


ASKERS = ("interactive",)  # the classes whose waiters ask (the replay found nothing in letting normal ones ask)


YIELD_BEHIND = 0.001  # seconds behind the waiter it yields to that a yielded lease queues


SHELLS = ("sh", "bash", "zsh", "dash", "fish")


ESTIMATE_RUNS = 20  # recent runs of a kind its estimate takes the median of


ESTIMATE_BYTES = 1 << 20  # of the history's end (and of its rotated part) read for estimates


UNFINISHED = ("reaped", "left")  # history outcomes whose time is no run of their kind: left out of the estimates


ESTIMATES_VERSION = 2  # estimates.json: 2 leaves UNFINISHED out; a file without it (or older) is worked out again


HISTORY_MAX = 4 << 20  # bytes of history.jsonl; past it the file becomes history.jsonl.1, replacing the older one


HISTORY_VERSION = 2


MACHINE_VERSION = 1  # machine.jsonl lines


MACHINE_MAX = 4 << 20  # bytes of machine.jsonl before it becomes machine.jsonl.1


MACHINE_EVERY = 15  # seconds between machine samples while any lease runs


SAMPLE_FIRST = 0.5  # seconds before a holder's first look at its command's process tree


SAMPLE_MAX = 10  # longest interval between those looks; each is half as long again as the one before


PS_TIMEOUT = 5  # seconds a ps or sysctl of the sampler may take before its answer is given up


WATCH_JOIN = 5  # seconds a release waits for the sampler to stop, and for its lock; then it goes without


ALL_NOTE = ("-n all waits until the whole machine is free; once it is the head, later requests start before it only "
            "if they are expected to end before its reserved start; use it only for work that needs the machine to "
            "itself, such as a performance measurement, and otherwise ask for the slots the command uses")


BATCH_NOTE = ("a batch request lets waiting normal and interactive requests go first until it has aged (`slotfather "
              "order` shows after how long): until then it is the head only while none waits, and one for every slot "
              "starts only then; `slotfather reprioritize PID normal` moves it up without losing its place")


LEASE_ENV_PREFIXES = ["SLOTFATHER_"]


META_KEY = re.compile(r"[a-z][a-z0-9_.-]{0,31}")


META_VALUE_MAX = 200


META_KEYS_MAX = 16
SWEEP_CLASSES = ("removed", "orphan", "dev-server", "container", "memory")  # the sweep's classes (sweep_plan)
CACHE_VALUES = ("hit", "miss", "partial")  # --meta cache=: whether the work reused a cache (fathers#97)


class SlotError(Exception):
    pass


class SlotTimeout(SlotError):
    pass


class ProcessTableError(SlotError):
    """ps gave no process table that can be trusted, and every holder would read as dead (fathers#121)."""


def setting(name: str, default: str | None = None) -> str | None:
    """SLOTFATHER_<name>, read when needed; a client that loads this file may replace this function to add its own
    spellings (tools/slot_lease.py)."""
    value = os.environ.get("SLOTFATHER_" + name)
    return default if value is None else value


def state_dir() -> Path:
    return Path(os.path.expanduser(setting("DIR") or DEFAULT_DIR))


def cores() -> int:
    value = setting("CORES") or ""
    return int(value) if value.isdigit() and int(value) > 0 else os.cpu_count() or 1


def overtake_window() -> float:
    """SLOTFATHER_OVERTAKE_WINDOW in seconds; a value that is not a number serves strictly in order."""
    try:
        return max(0.0, float(setting("OVERTAKE_WINDOW") or OVERTAKE_WINDOW))
    except ValueError:
        return 0.0


def legacy_dirs() -> list[Path]:
    value = setting("LEGACY_DIRS", DEFAULT_LEGACY_DIRS)
    own = state_dir().resolve()
    paths = [Path(os.path.expanduser(p)) for p in value.split(":") if p]
    return [p for p in paths if p.resolve() != own]


def now() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def seconds(stamp: Any) -> float | None:
    """A UTC queue timestamp as epoch seconds, including earlier unpadded spellings."""
    if not isinstance(stamp, str):
        return None
    import datetime
    try:
        if len(stamp) == 20 and stamp[4] == stamp[7] == "-" and stamp[10] == "T" and stamp[13] == stamp[16] == ":" and stamp[-1] == "Z":
            return datetime.datetime.fromisoformat(stamp[:-1]).replace(tzinfo=datetime.timezone.utc).timestamp()
        # Old clients/readers accepted unpadded months and days. Keep that fallback.
        return datetime.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc).timestamp()
    except ValueError:
        return None


def duration(value: float) -> str:
    value = int(value)
    if value >= 3600:
        return f"{value // 3600}h{value % 3600 // 60:02d}m"
    return f"{value // 60}m{value % 60:02d}s" if value >= 60 else f"{value}s"


def number_setting(name: str, default: float) -> float:
    try:
        return float(setting(name) or default)
    except ValueError:
        return default



def meta_text(meta: dict[str, str]) -> str:
    """Metadata for one line: key=value pairs joined by commas, a value quoted where it holds a space, comma, quote
    or equals sign."""
    plain = re.compile(r'[^\s,="]+')
    return ",".join(f"{k}={v if plain.fullmatch(v) else json.dumps(v)}" for k, v in meta.items())
