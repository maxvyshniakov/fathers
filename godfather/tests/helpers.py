"""Shared test seams: the package on sys.path, a frozen clock for the renderers, a fake Slotfather on PATH."""
from __future__ import annotations

import contextlib
import datetime as dt
import importlib
import json
import os
import sys
import tempfile
import time
import types
from pathlib import Path
from typing import Any, Iterator
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
FAKE = ROOT / "tests" / "fake_slotfather.py"


def frozen(moment: dt.datetime) -> Any:
    """A datetime module whose now() and today() are `moment`, as Slotfather's demo freezes its own."""

    class Clock(dt.datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Any:  # type: ignore[override]
            return moment.astimezone(tz) if tz else moment.astimezone().replace(tzinfo=None)

    class Date(dt.date):
        @classmethod
        def today(cls) -> Any:  # type: ignore[override]
            return moment.astimezone().date()

    return types.SimpleNamespace(datetime=Clock, date=Date, timedelta=dt.timedelta, timezone=dt.timezone)


@contextlib.contextmanager
def frozen_at(moment: dt.datetime) -> Iterator[None]:
    from godfather import tui
    from godfather.views import overview

    clock = frozen(moment)
    with mock.patch.object(tui, "datetime", clock), mock.patch.object(overview, "datetime", clock):
        yield


@contextlib.contextmanager
def timezone(name: str) -> Iterator[None]:
    before = os.environ.get("TZ")
    os.environ["TZ"] = name
    time.tzset()
    try:
        yield
    finally:
        if before is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = before
        time.tzset()


class FakeSlotfather:
    """tests/fake_slotfather.py as `slotfather` on PATH, with its state (queue, settings) in a temporary
    directory: the CLI contracts of SF-1 and SF-2, nothing else."""

    def __init__(self, state: dict[str, Any] | None = None) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.state = self.root / "state.json"
        self.calls = self.root / "calls.jsonl"
        self.write(dict({"cores": 12, "memory_gib": 64, "capacity": None, "order": {},
                         "environment": ["SLOTFATHER_CORES", "SLOTFATHER_MEMORY_GIB"], "writable": True},
                        **(state or {})))
        command = self.bin / "slotfather"
        command.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE} \"$@\"\n")
        command.chmod(0o755)

    def write(self, state: dict[str, Any]) -> None:
        self.state.write_text(json.dumps(state))

    def read(self) -> dict[str, Any]:
        return json.loads(self.state.read_text())

    def update(self, **changes: Any) -> None:
        state = self.read()
        state.update(changes)
        self.write(state)

    def argv(self) -> list[list[str]]:
        if not self.calls.exists():
            return []
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def environ(self) -> dict[str, str]:
        return {"PATH": f"{self.bin}:/usr/bin:/bin", "FAKE_SLOTFATHER_STATE": str(self.state),
                "FAKE_SLOTFATHER_CALLS": str(self.calls), "HOME": str(self.root),
                "XDG_CONFIG_HOME": str(self.root / "config")}

    @contextlib.contextmanager
    def active(self) -> Iterator["FakeSlotfather"]:
        with mock.patch.dict(os.environ, self.environ()):
            os.environ.pop("GODFATHER_FIXTURES", None)
            yield self

    def close(self) -> None:
        self.tmp.cleanup()


def reload(name: str) -> Any:
    return importlib.reload(importlib.import_module(name))


# The reducer as the drawing tests drive it: a state over the frame a canvas drew, and an event through reduce()

def frame_of(canvas: Any) -> Any:
    """What a drawn canvas placed, as the live loop hands it to the reducer (state.Drawn)."""
    state = importlib.import_module("godfather.state")
    return state.Frame(canvas.width, canvas.height, dict(canvas.tables), canvas.version_at, tuple(canvas.hint_hits),
                       canvas.order)


def drawn_state(canvas: Any, model: dict[str, Any] | None = None, selected: int = 0, **fields: Any) -> Any:
    """An app state with `selected` selected in `model`, over the frame `canvas` drew."""
    import dataclasses

    state = importlib.import_module("godfather.state")
    found = state.move(state.AppState(model=model), selected)
    return dataclasses.replace(found, frame=frame_of(canvas), **fields)


def mouse(canvas: Any, event: tuple[str, int, int, int, bool], scroll: dict[str, int], selected: int = 0,
          model: dict[str, Any] | None = None) -> int:
    """One mouse event through the reducer on the frame `canvas` drew: `scroll` takes the panels' offsets after it;
    the selected index after it."""
    state = importlib.import_module("godfather.state")
    found, _ = state.reduce(drawn_state(canvas, model, selected, scroll=dict(scroll)),
                            state.Mouse(event[1], event[2], event[3], event[4]), 0.0)
    scroll.clear()
    scroll.update(found.scroll)
    return found.selection.index
