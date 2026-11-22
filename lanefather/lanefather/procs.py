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
import re
import subprocess
import time
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
    path = (project or config.DEFAULT).path
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
    reason: str = ""

    def describe(self) -> str:
        return f"pid {self.pid} ({self.command[:100]})" + (f"; {self.reason}" if self.reason else "")


def order(pid: Any) -> tuple[int, str]:
    """Pids in numeric order (99 before 100); anything else after them, as text."""
    return (pid, "") if isinstance(pid, int) else (2**63, str(pid))


class QueryError(Exception):
    unsupported = False


class SlotfatherSource:
    """Read-only process observations through versioned CLI contracts; identity-bound stopping is unavailable."""

    NAME = "slotfather sweep"
    # fathers#158: how often an answer with unknown pids is asked again, and the pause before each.
    retries = 4
    pause = 0.5

    def __init__(self, command: str):
        self.command = command

    def _ask(self, *args: str) -> dict[str, Any]:
        try:
            run = subprocess.run([self.command, *args], capture_output=True, text=True, timeout=120)
            if run.returncode:
                error = QueryError(f"exit {run.returncode}: {run.stderr.strip()[:200]}")
                message = (run.stderr + run.stdout).lower()
                error.unsupported = bool(args[:1] == ("procs",) and (
                    re.search(r"unknown(?: or incomplete)?(?: command| option)?[: ]+['\"]?procs\b", message)
                    or ("usage:" in message and "procs" not in message)))
                raise error
            data: Any = json.loads(run.stdout)
            if not isinstance(data, dict):
                raise QueryError("unknown slotfather JSON schema")
            payload = cast(dict[str, Any], data)
            if type(payload.get("v")) is not int or payload.get("v") not in (1, 2):
                raise QueryError("unknown slotfather JSON schema")
            return payload
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            raise QueryError(str(error)) from None

    def _query(self, *args: str) -> dict[str, Any]:
        data = self._ask("procs", "--json", *args)
        if not all(isinstance(data.get(k), list) for k in ("processes", "cwd_unknown", "lease_unknown")):
            raise QueryError("incomplete process query")
        return data

    def _settled(self, root: str) -> dict[str, Any]:
        """`procs --cwd root` until one answer has no unknown pid (fathers#158): on a busy machine short-lived
        helpers (lsof, git, a shell) exit between the scan and the read of their working directory. An answer that
        still has unknowns after the retries refuses, naming each pid and its command."""
        query = self._query("--cwd", root)
        for _ in range(self.retries):
            if not query["cwd_unknown"] and not query["lease_unknown"]:
                return query
            time.sleep(self.pause)
            query = self._query("--cwd", root)
        if not query["cwd_unknown"] and not query["lease_unknown"]:
            return query
        raise QueryError(f"working-directory or lease membership is unknown after {self.retries + 1} queries: "
                         + "; ".join(self._unknowns(query)))

    def _unknowns(self, query: dict[str, Any]) -> list[str]:
        """Each unknown pid of an answer with its command and what is unknown. A Slotfather that names them
        (`unknown`, fathers#158) is read as it is; for an older one, `procs --pid` asks for each command."""
        cwd, lease = set(query["cwd_unknown"]), set(query["lease_unknown"])
        given: Any = query.get("unknown")
        commands: dict[Any, str] = {}
        if isinstance(given, list):
            for entry in cast(list[Any], given):
                if isinstance(entry, dict) and "pid" in entry:
                    named = cast(dict[str, Any], entry)
                    commands[named["pid"]] = str(named.get("cmd") or "")
        for pid in sorted(cwd | lease, key=order):
            if pid not in commands:
                try:
                    rows: list[dict[str, Any]] = self._query("--pid", str(pid))["processes"]
                    commands[pid] = str(rows[0].get("cmd", "")) if rows else "exited"
                except (QueryError, KeyError, TypeError, IndexError):
                    commands[pid] = "command unknown"
        return [f"pid {pid} ({commands[pid][:100] or 'command unknown'}) "
                + " and ".join(what for what, held in (("cwd", pid in cwd), ("lease", pid in lease)) if held)
                + " unknown" for pid in sorted(cwd | lease, key=order)]

    def sort_for_removal(self, root: str) -> tuple[list[Item], list[str]]:
        try:
            query = self._settled(root)
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

    def supported(self) -> bool:
        """Negotiate the actual read-only procs contract, without probing a mutating command."""
        try:
            self._query("--pid", str(os.getpid()))
        except QueryError as error:
            # Only an explicit older command surface gets legacy behavior. Failed inspection and unknown schemas
            # remain an unavailable source, which retains a worktree rather than silently skipping inspection.
            return not error.unsupported
        return True

    def stop(self, processes: list[Item], root: str) -> list[Item]:
        """The receiver has no identity-bound stop contract yet. A later PID-only sweep is not permission to
        stop a PID from an earlier observation: it could be reused before sweep builds its own snapshot."""
        del root
        reason = "slotfather has no identity-bound stop contract; retained instead of a PID-only live sweep"
        return [Item(p.pid, p.start, p.command, reason) for p in processes]


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
            selected = ({p.pid for p in self.module.in_worktree(root, table)}
                        if hasattr(self.module, "in_worktree") else {p.pid for p in table.values()})
            held += [f"pid {p.pid} ({p.command[:100]}) uses the worktree"
                     for p in table.values() if p.pid != os.getpid()
                     and ((self.module.inside(p.cwd, root) and p.pid not in selected)
                          or (root in p.command and not self.module.inside(p.cwd, root)))]
            return stopping, held
        except self.module.ScanError as error:
            return [], [f"which processes in {root} may be stopped is unknown: {error}"]

    def stop(self, processes: list[Any], root: str) -> list[Any]:
        return self.module.stop(processes, root)


def source(project: config.Config | None = None) -> Any:
    """The process source of this project."""
    cfg = project or config.DEFAULT
    hook = cfg.process_scan
    root = project_root(cfg)
    if hook and root is not None and (root / hook).is_file():
        return HookSource(_load(str(root / hook)))
    command = slots.command()
    adapter = SlotfatherSource(command) if command else None
    return adapter if adapter is not None and adapter.supported() else NoSource()
