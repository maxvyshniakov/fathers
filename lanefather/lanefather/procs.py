"""Which processes a lane left in its worktree, and stopping them (#661), through a seam: lanefather keeps no
process or lease code of its own.

Sources, first that answers:
- the project's `[hooks] process_scan` module (Straylight's tools/agent-hooks/process_scan.py, which asks
  `slotfather sweep --json --pid` what may stop and stops it with `slotfather sweep --live --pid`):
  sort_for_removal(root) -> (processes to stop, reasons the worktree is held), and stop(processes, root) -> what
  still works in root after the stop; each process has .pid and .describe(). An empty or failed answer holds the
  worktree, and so does anything still in use after the stop (fail closed, #661 review);
- `slotfather procs --json --cwd PATH` once slotfather has it (maxvyshniakov/slotfather#1): its source goes here,
  ahead of the hook, and the hooks then retire.
Without a source nothing is scanned: done removes the worktree as before #661 and says so in the ledger.
"""
from __future__ import annotations

import functools
import importlib.machinery
import importlib.util
from pathlib import Path
from typing import Any, Protocol

from . import config


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


def project_root() -> Path | None:
    path = config.current().path
    return path.parent if path else None


@functools.cache
def _load(path: str) -> Any:
    loader = importlib.machinery.SourceFileLoader("lanefather_process_scan", path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def source() -> Any:
    """The process source of this project."""
    hook = config.current().process_scan
    root = project_root()
    if hook and root is not None and (root / hook).is_file():
        return _load(str(root / hook))
    return NoSource()
