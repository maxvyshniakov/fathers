"""Which processes a lane left in its worktree, and stopping them (#661), through a seam: lanefather keeps no
process or lease code of its own.

Sources, first that answers:
- the project's `[hooks] process_scan` module (Straylight's tools/agent-hooks/process_scan.py, which asks
  `slotfather sweep --json --pid` what may stop and stops it with `slotfather sweep --live --pid`):
  sort_for_removal(root) -> (processes to stop, reasons the worktree is held), and stop(processes, root) -> what
  still works in root after the stop; each process has .pid and .describe(). An empty or failed answer holds the
  worktree, and so does anything still in use after the stop (fail closed, #661 review);
- `slotfather procs --json --cwd PATH` by default, when no project hook is configured. It owns process
  inspection and sweep owns stopping; the older hook stays an explicit adapter for live branch compatibility.
Without a source nothing is scanned: done removes the worktree as before #661 and says so in the ledger.
"""
from __future__ import annotations

from dataclasses import dataclass
import functools
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import subprocess
from typing import Any, Protocol, cast

from . import config, slots


class Process(Protocol):
    pid: int

    def describe(self) -> str: ...


class Source(Protocol):
    def sort_for_removal(self, root: str) -> tuple[list[Any], list[str]]: ...

    def stop(self, processes: list[Any], root: str) -> list[Any]: ...


class NoSource:
    """No way to see processes: nothing to stop, nothing holds the worktree."""
    name = "none"

    def sort_for_removal(self, root: str) -> tuple[list[Any], list[str]]:
        del root
        return [], []

    def stop(self, processes: list[Any], root: str) -> list[Any]:
        del root
        return list(processes)


def source_name(source: Any) -> str:
    """How messages name the source: the hook's own NAME, else slotfather sweep, which Straylight's hook asks."""
    return str(getattr(source, "NAME", "slotfather sweep" if not isinstance(source, NoSource) else "nothing"))


def project_root(project: config.Config | None = None) -> Path | None:
    path = (project or config.current()).path
    return path.parent if path else None


@functools.cache
def _load(path: str) -> Any:
    loader = importlib.machinery.SourceFileLoader("lanefather_process_scan", path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@dataclass(frozen=True)
class Item:
    pid: int
    start: str
    command: str

    def describe(self) -> str:
        return f"pid {self.pid} ({self.command[:100]})"


class QueryError(Exception):
    pass


class SlotfatherSource:
    """Process and stop observations through versioned CLI contracts; never inspect the machine ourselves."""

    NAME = "slotfather sweep"

    def __init__(self, command: str):
        self.command = command

    def _ask(self, *args: str) -> dict[str, Any]:
        try:
            run = subprocess.run([self.command, *args], capture_output=True, text=True, timeout=120)
            if run.returncode not in ((0, 1) if "--live" in args else (0,)):
                raise QueryError(f"exit {run.returncode}: {run.stderr.strip()[:200]}")
            data: Any = json.loads(run.stdout)
            if not isinstance(data, dict) or data.get("v") != 1:  # pyright: ignore[reportUnknownMemberType]
                raise QueryError("unknown slotfather JSON schema")
            return cast(dict[str, Any], data)
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            raise QueryError(str(error)) from None

    def _query(self, *args: str) -> dict[str, Any]:
        data = self._ask("procs", "--json", *args)
        if not all(isinstance(data.get(k), list) for k in ("processes", "cwd_unknown", "lease_unknown")):
            raise QueryError("incomplete process query")
        return data

    def sort_for_removal(self, root: str) -> tuple[list[Item], list[str]]:
        try:
            query = self._query("--cwd", root)
            if query["cwd_unknown"] or query["lease_unknown"]:
                raise QueryError("working-directory or lease membership is unknown")
            processes: list[dict[str, Any]] = query["processes"]
            # --cwd intentionally excludes command-line-only references: those still retain a worktree.
            named = self._query()["processes"]
            held = [f"pid {p['pid']} ({str(p.get('cmd', ''))[:100]}) names the worktree"
                    for p in named if root in str(p.get("cmd", "")) and p.get("pid") != os.getpid()
                    and p.get("pid") not in {q.get("pid") for q in processes}]
            if not processes:
                return [], held
            by_pid = {p["pid"]: p for p in processes}
            if any(not isinstance(p.get("pid"), int) or not p.get("start") for p in processes):
                raise QueryError("process identity is unavailable")
            sweep = self._ask("sweep", "--json", "--no-log",
                              *(word for pid in by_pid for word in ("--pid", str(pid))))
            items: list[dict[str, Any]] = sweep["items"]
            offered = [p for p in items if p.get("action") == "would-stop"]
            covered = {pid for p in offered for pid in p.get("tree", [p["pid"]])}
            held += [f"pid {pid}: slotfather sweep does not take it for left behind"
                     for pid in by_pid if pid not in covered]
            return [Item(p["pid"], by_pid[p["pid"]]["start"], str(p.get("cmd", "")))
                    for p in offered if p["pid"] in by_pid], held
        except (QueryError, KeyError, TypeError) as error:
            return [], [f"process source query failed: {error}"]

    def stop(self, processes: list[Item], root: str) -> list[Any]:
        try:
            # A sweep's offered PID is never a timeless permission to signal that PID.
            live: list[Item] = []
            for item in processes:
                identity = self._query("--pid", str(item.pid))
                current = identity["processes"]
                if identity["cwd_unknown"] or identity["lease_unknown"]:
                    raise QueryError(f"pid {item.pid} membership is unknown")
                if not current:
                    continue
                if len(current) != 1 or current[0].get("start") != item.start:
                    raise QueryError(f"pid {item.pid} changed identity")
                live.append(item)
            if live:
                answer = self._ask("sweep", "--json", "--live",
                                   *(word for p in live for word in ("--pid", str(p.pid))))
                if answer.get("live_refused"):
                    raise QueryError("live sweep refused")
                stopped = {p["pid"] for p in answer["items"] if p.get("action") in ("stopped", "gone")}
                if any(p.pid not in stopped for p in live):
                    return list(processes)
            left, held = self.sort_for_removal(root)
            return [*left, *([Item(0, "", "; ".join(held))] if held else [])]
        except (QueryError, KeyError, TypeError):
            return list(processes)


class HookSource:
    """The older project's process observation adapter, including command-line references outside the cwd."""

    NAME = "slotfather sweep"

    def __init__(self, module: Any):
        self.module = module

    def sort_for_removal(self, root: str) -> tuple[list[Any], list[str]]:
        if not hasattr(self.module, "table"):
            return self.module.sort_for_removal(root)
        try:
            table = self.module.table()
            stopping, held = self.module.sort_for_removal(root, table)
            held += [f"pid {p.pid} ({p.command[:100]}) names the worktree"
                     for p in table.values() if p.pid != os.getpid()
                     and root in p.command and not self.module.inside(p.cwd, root)]
            return stopping, held
        except self.module.ScanError as error:
            return [], [f"which processes in {root} may be stopped is unknown: {error}"]

    def stop(self, processes: list[Any], root: str) -> list[Any]:
        return self.module.stop(processes, root)


def source(project: config.Config | None = None) -> Any:
    """The process source of this project."""
    project = project or config.current()
    hook = project.process_scan
    root = project_root(project)
    if hook and root is not None and (root / hook).is_file():
        return HookSource(_load(str(root / hook)))
    command = slots.command()
    return SlotfatherSource(command) if command else NoSource()
