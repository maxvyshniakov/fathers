"""Configured project operations for queue policy and idle detection, without importing a project's lane script."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import os
from pathlib import Path
import stat
import tempfile
from typing import Any

from . import config, host, journal, queue, state
from .errors import LaneError


@dataclass(frozen=True)
class ProjectPaths:
    state_dir: Path
    queue_path: Path


@dataclass(frozen=True)
class Readiness:
    state_dir: Path
    queue_path: Path
    text: str
    settings: dict[str, Any]
    entries: tuple[dict[str, Any], ...]
    records: tuple[state.Record, ...]
    states: tuple[tuple[dict[str, Any], str | None], ...]
    ready: dict[str, Any] | None
    cap: int
    cap_source: str
    host: dict[str, Any]
    blocked: str | None
    timestamp: str

    @property
    def active(self) -> tuple[state.Record, ...]:
        return tuple(state.occupying(list(self.records)))

    @property
    def can_start(self) -> bool:
        return self.ready is not None and self.blocked is None


@dataclass(frozen=True)
class QueueUpdate:
    text: str
    message: str = ''


class Project:
    """One explicit configuration; operations never replace the command's ambient configuration."""

    def __init__(self, project: config.Config):
        self.config = project
        self._store = state.Store(project)
        self._queue_path = queue.queue_path(project)
        self.paths = ProjectPaths(self._store.directory, self._queue_path)

    def readiness(self) -> Readiness:
        try:
            text = self._queue_path.read_text()
        except FileNotFoundError:
            raise LaneError(f"no queue at {self._queue_path}: rank the next tickets there "
                            "as [[lane]] entries") from None
        except OSError as error:
            raise LaneError(f'{self._queue_path}: {error}') from None
        settings, entries = queue.parse_queue(text, self._queue_path)
        records = self._store.lanes(strict=True)
        states = queue.queue_states(entries, records)
        cap, source = host.lane_cap(self.config)
        machine = host.host(self.config)
        active = len(state.occupying(records))
        blocked = None
        if active >= cap:
            blocked = f'{active} of {cap} lanes planned or active'
        elif machine['load15'] >= host.LOAD_FACTOR * machine['cores']:
            blocked = f"load15 {machine['load15']:.1f} is at or above {host.LOAD_FACTOR * machine['cores']:.1f}"
        elif machine['disk_free_gb'] < host.MIN_DISK_GB:
            blocked = f"free disk {machine['disk_free_gb']:.0f} GB is under {host.MIN_DISK_GB} GB"
        return Readiness(self._store.directory, self._queue_path, text, settings, tuple(entries), tuple(records),
                         tuple(states), next((entry for entry, reason in states if reason is None), None),
                         cap, source, machine, blocked, journal.now())

    def update_queue(self, policy: Callable[[Readiness], QueueUpdate | None]) -> str | None:
        """Run project policy under the shared queue lock; validate and atomically persist its proposal."""
        with self._store.locked('queue'):
            snapshot = self.readiness()
            change = policy(snapshot)
            if change is None:
                return None
            _, entries = queue.parse_queue(change.text, self._queue_path)
            ids = [entry['id'] for entry in entries]
            if len(ids) != len(set(ids)):
                raise LaneError('queue policy returned duplicate lane ids; queue was not changed')
            if self._queue_path.read_text() != snapshot.text:
                raise LaneError('queue changed outside the shared lock; refusing to overwrite it')
            if change.text == snapshot.text:
                return change.message
            mode = stat.S_IMODE(self._queue_path.stat().st_mode)
            pending: Path | None = None
            try:
                with tempfile.NamedTemporaryFile('w', dir=self._queue_path.parent,
                                                 prefix=f'.{self._queue_path.name}.pending-', delete=False) as handle:
                    pending = Path(handle.name)
                    handle.write(change.text)
                    handle.flush()
                    os.fchmod(handle.fileno(), mode)
                    os.fsync(handle.fileno())
                os.replace(pending, self._queue_path)
            finally:
                if pending is not None:
                    pending.unlink(missing_ok=True)
            with (self._store.directory / 'ledger.log').open('a') as ledger:
                ledger.write(f'{journal.now()} {change.message or "queue updated via project policy"}\n')
            return change.message
