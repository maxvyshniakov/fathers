"""Idle leases (#12): one definition of idle for every reader.

A running lease is idle when its command's process tree has used less than `idle_cpu` cores, with no process starting
or ending, for `idle_after` seconds or more (both are queue settings, order.json). The holder measures its own tree
(usage.Watch) and records since when it has been low in `<entry>.idle` beside its queue entry; every reader (the
dashboard's bar colour and marker, LEASE DETAILS, `status`, the yield ask) applies `idle_after` to that record. A
short pause (network, disk, a browser) never lasts the minimum; a record the holder stopped writing says nothing.
Older clients neither write nor read the record: their leases are never idle, and they read past the file."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any, Mapping, cast

from . import config as _config
from . import state as _state

SUFFIX = ".idle"
STALE_LOOKS = 3  # a record older than this many of the holder's longest intervals between looks says nothing
STALE_LEAST = 30.0  # seconds, whatever the interval


# What runs a container or a virtual machine, whose work goes on elsewhere at no CPU of the host's own (fathers#159):
# a tree with one running is never idle. Only a process that runs one counts, not every tool of that family (a
# `kubectl port-forward` or `docker ps` left running is no work) (fathers#159 review).
CONTAINER_CLIENTS = ("docker", "podman", "nerdctl", "container")  # with one of RUNS: a container's own client
RUNS = ("run", "exec", "start", "attach", "up", "build", "buildx")  # what makes a client run one (compose: up/run)
VMS = ("qemu-system", "vfkit", "firecracker", "krunvm", "vboxheadless", "com.apple.virtualization.virtualmachine",
       "lima-guestagent", "hyperkit")  # a virtual machine's own process


def container(command: str) -> bool:
    """Whether a process's command line (ps `command`) runs a container or a virtual machine: a VM's own process
    (VMS: `qemu-system-aarch64 ...`, `vfkit ...`), or a container client (CONTAINER_CLIENTS, `docker-compose`) with
    a command that runs one (RUNS: `docker run ...`, `docker compose up`, `podman exec ...`)."""
    words = command.split()
    if not words:
        return False
    program = os.path.basename(words[0]).lower()
    if any(program == vm or program.startswith(vm) for vm in VMS):
        return True
    client = program in CONTAINER_CLIENTS or program in ("docker-compose", "podman-compose")
    return client and any(word in RUNS for word in words[1:])


def path(entry: Path) -> Path:
    """The idle record beside a queue entry."""
    return entry.with_suffix(SUFFIX)


class Tracker:
    """Since when a process tree has been low: fed one sample after another, (pid, start) -> own CPU seconds."""

    def __init__(self) -> None:
        self.last: tuple[float, dict[Any, float]] | None = None
        self.since: float | None = None
        self.peak = 0.0  # the highest rate of the current low run: a lower threshold than that starts a new run

    def observe(self, moment: float, members: Mapping[Any, float], threshold: float) -> float | None:
        """Takes one sample and returns since when the tree has been low (None: it is not, or not known yet).
        `members` is each process's CPU seconds, its own and its reaped children's (platform.children_cpu()), so a
        worker started and reaped between two samples counts. An interval is low when the tree used less than
        `threshold` cores in it and kept the same processes: a process that starts or ends is activity even if its
        CPU fell between two samples. A run built under a looser threshold than the one now saved starts again. A
        threshold of 0 finds nothing."""
        members = dict(members)
        if self.last is not None:
            before, previous = self.last
            elapsed = moment - before
            used = sum(max(0.0, cpu - previous[key]) for key, cpu in members.items() if key in previous)
            self.rate = used / elapsed if elapsed > 0 else math.inf
            low = elapsed > 0 and set(members) == set(previous) and threshold > 0 and self.rate < threshold
            if low and self.since is not None and self.peak >= threshold:
                self.since = None  # the threshold dropped below what this run allowed
            if not low:
                self.since, self.peak = None, 0.0
            elif self.since is None:
                self.since, self.peak = before, self.rate
            else:
                self.peak = max(self.peak, self.rate)
        self.last = (moment, members)
        return self.since

    rate: float = math.inf  # cores used in the last interval


def record(entry: Path, since: float | None, rate: float, moment: float) -> None:
    """The holder's record: since when its tree has been low (None: busy), the cores it used last, when, and until
    when the record holds (`expires`, by the holder's own sampling interval, so every reader agrees on it)."""
    found: dict[str, Any] = {"since": None if since is None else round(since, 3),
                             "cpu": None if not math.isfinite(rate) else round(rate, 3), "at": round(moment, 3),
                             "expires": round(moment + stale_after(), 3)}
    cast(Any, _state).write(path(entry), found)


def stale_after() -> float:
    """How long a record holds: three of this process's longest intervals between looks, at least 30 s; written by
    the holder into its record (`expires`)."""
    longest = _config.sample_max()
    return max(STALE_LEAST, STALE_LOOKS * longest)


def idle_since(entry: Path, settings: Mapping[str, Any], moment: float) -> float | None:
    """Since when the lease at `entry` has been idle (epoch seconds), or None: not idle, not yet idle for
    `idle_after`, or no fresh record (an older holder, one that stopped sampling, or a broken file)."""
    try:
        found = json.loads(path(entry).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(found, dict):
        return None
    record = cast(dict[str, Any], found)
    at, since, expires = record.get("at"), record.get("since"), record.get("expires")
    numbers = (int, float)
    if not isinstance(at, numbers) or not isinstance(since, numbers) or isinstance(since, bool):
        return None
    if not isinstance(expires, numbers) or isinstance(expires, bool):
        expires = at + stale_after()  # a record without its holder's expiry: this reader's interval
    if moment > expires or moment - since < float(settings.get("idle_after", 0.0)):
        return None
    return float(since)
