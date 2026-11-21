"""The command runner (fathers#37): execute() runs a command with its lease's environment, forwarding SIGTERM and
SIGHUP, measuring its process tree (usage.Watch) and reaping it with wait4; run() is `slotfather run`: a label,
a lease (client.acquire()) and the command under it."""
from __future__ import annotations

import contextlib
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Any

from . import client as _client
from . import config as _config
from . import entry as _entry
from . import process as _process
from . import settings as _settings


def lease_cores(count: int, cores: int | None = None, total: int | None = None) -> int:
    """The lease's exported core share (SLOTFATHER_LEASE_CORES, fathers#11):
    `max(1, floor(min(count, total) * cores / total))`."""
    machine = _config.cores() if cores is None else cores
    capacity = _settings.total() if total is None else total
    effective = min(max(1, count), max(1, capacity))
    return max(1, math.floor(effective * max(1, machine) / max(1, capacity)))


def execute(argv: list[str], environment: dict[str, str], background: bool = False, marker: Path | None = None,
            directory: Path | None = None) -> tuple[int, dict[str, Any] | None]:
    """Runs argv with `environment` added and waits for it, forwarding SIGTERM and SIGHUP: (its exit status, 128 +
    the signal when one ended it; what its process tree used, where measured). With `marker`, a held lease's entry,
    its tree is measured with the processes that carry that lease and its idleness recorded beside the entry; with
    only `directory`, the tree alone is measured (a run inside a session, which shares its lease with others)."""
    from . import usage as _usage
    if background:
        prefix = ["/usr/sbin/taskpolicy", "-b", "/usr/bin/nice", "-n", "10"] if os.access(
            "/usr/sbin/taskpolicy", os.X_OK) else ["/usr/bin/nice", "-n", "10"]
        argv = prefix + argv
    try:
        child = subprocess.Popen(argv, env={**os.environ, **environment})
    except OSError as error:
        print(f"slot: {error}", file=sys.stderr)
        return 127, None
    began = time.monotonic()
    watch = None
    if marker is not None:
        text = str(marker)
        def carrying(pids: list[int]) -> set[int]:
            return _process.carrying(pids, text)
        watch = _usage.Watch(_usage.TreeUsage(child.pid, time.time() - 1, carrying), marker.parent.parent, entry=marker)
    elif directory is not None:
        watch = _usage.Watch(_usage.TreeUsage(child.pid, time.time() - 1), directory)
    if watch is not None:
        with contextlib.suppress(RuntimeError):
            watch.start()
    reaped: list[bool] = []

    def forward(signum: int, _frame: Any) -> None:
        # os.kill rather than child.send_signal, which polls and could reap the child before wait4 sees it;
        # until wait4 returns the child (a zombie at worst) keeps its pid.
        if not reaped:
            with contextlib.suppress(ProcessLookupError):
                os.kill(child.pid, signum)
    # The terminal sends SIGINT to the child's process group itself; the lease outlives the child.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, forward)
    try:
        _, raw, rusage = os.wait4(child.pid, 0)  # the rusage holds every descendant reaped inside the tree
        status = os.waitstatus_to_exitcode(raw)
    except ChildProcessError:
        status, rusage = child.wait(), None
    reaped.append(True)
    if watch is not None:
        watch.usage.root_gone = True  # before any later look: the pid may be reused from now on
    child.returncode = status
    usage: dict[str, Any] | None = None
    if watch is not None and watch.is_alive():
        usage = watch.finish(rusage, time.monotonic() - began)
    return (128 - status if status < 0 else status), usage


def run(count: int | str, label: str, argv: list[str], wait: float | None = None, meta: dict[str, str] | None = None,
        yieldable: bool = False, *, priority: str | None = None, ticket: str | int | None = None,
        labelled: bool = True, max_count: int | None = None) -> int:
    """Run argv under a lease of `count` slots; the command's exit status. Without `labelled` (no --label), `label`
    (the directory's name) gives way to one derived from the project, the command and the metadata (labels.py); a
    label that says little gets the derived parts appended, with a hint."""
    meta = _entry.checked_meta(meta)
    naming: dict[str, Any] = {}
    if _client.policy() != "bypass" and not _config.setting("LEASE"):  # a nested run takes no lease and names none
        try:
            cwd = os.getcwd()
        except OSError:
            cwd = ""
        with contextlib.suppress(Exception):  # auto-tagging must never cost the run
            from . import labels as _labels
            meta, tag_warnings = _labels.auto_tags(cwd, meta)
            for warning in tag_warnings:
                print(f"slot: {warning}", file=sys.stderr, flush=True)
        with contextlib.suppress(Exception):  # a name must never cost the run
            from . import labels as _labels
            naming = _labels.choose(label if labelled else None, cwd, argv, meta,
                                    None if ticket is None else str(ticket))
            label = naming["label"] or label
            if naming.get("hint"):
                print(f"slot: {naming['hint']}", file=sys.stderr, flush=True)
    try:
        background = _client.policy() == "background"
        note = _config.BATCH_NOTE if priority == "batch" else _config.ALL_NOTE if count == "all" else ""
        lease = _client.acquire(count, label, " ".join(argv), wait=wait, note=note, meta=meta,
                        yieldable=yieldable, priority=priority, ticket=ticket,
                        naming={k: naming[k] for k in ("given", "derived", "project")
                                if naming.get(k) and (k != "given" or naming[k] != label)} if naming else None,
                        max_count=max_count)
    except _config.SlotTimeout as error:
        print(f"slot: {label}: {error}", file=sys.stderr)
        return _config.TIMED_OUT
    except KeyboardInterrupt:
        return 130
    except SystemExit as stop:
        return int(stop.code or 1)
    if count != "all" and lease.path is not None and not lease.inherited:
        with contextlib.suppress(Exception):  # a hint must never cost the run
            from . import runtype as _runtype
            found = _runtype.hint(lease.path.parent.parent, label, lease.count)
            if found:
                print(f"slot: {found}", file=sys.stderr, flush=True)
    with lease:
        marker = lease.path if lease.path is not None and not lease.inherited else None
        child_env = lease.environment()
        child_env["SLOTFATHER_SLOTS"] = str(lease.count)
        child_env["SLOTFATHER_LEASE_CORES"] = str(lease_cores(lease.count))
        lease.status, lease.usage = execute(argv, child_env, background, marker)
    return lease.status
