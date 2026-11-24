#!/usr/bin/env python3
"""Slotfather's recorded navigation scene in Godfather's real interactive dashboard, for live demos and screen-kit.

    python3 tools/dashboard-demo.py [--once] [--width N] [--height N]    the scene, live or one frame
    screen-kit capture --config godfather/tools/screen-kit.config.mjs --out assets

The scene and its recordings are Slotfather's (tools/fixtures/README.md says where they come from), moved with the
dashboard. With GODFATHER_FIXTURES set (e.g. tests/fixtures/procs), Lanefather and Spawnfather snapshots, events and
pages come from those recorded fixtures too, while the clock and MACHINE chart history stay frozen to the scene.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import os
import sys
import time
import types
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, cast
from unittest import mock

if TYPE_CHECKING:
    from godfather.model import Dashboard

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
FIXTURE = ROOT / "tools/fixtures/navigation.json"
METRICS = ROOT / "tools/fixtures/metrics.json"
CHART_HISTORY = ROOT / "tools/fixtures/chart-history.json"
NEWS = ROOT / "tests/fixtures/news"  # what the tools answer for what's new (tools/goldens.py uses it too)
MIN_FREE_SWAP_MB, MAX_PRESSURE_LEVEL = 512, 1  # Slotfather's defaults, for its judgement of the recorded memory


def judge(memory: dict[str, Any]) -> None:
    """Slotfather's judgement of recorded memory facts, as its snapshot carries it (`low`, `swap_low`)."""
    level = memory.get("level")
    memory["low"] = None if level is None else level > MAX_PRESSURE_LEVEL
    memory["swap_low"] = bool(memory.get("swap_total")) and memory.get("swap_free") is not None \
        and memory["swap_free"] < MIN_FREE_SWAP_MB


def load_scene(advance: bool = False, elapsed: Callable[[], float] | None = None,
               recorded: bool = False) -> tuple[Any, dict[str, Any]]:
    """(the app module with the scene patched in, its first model): the demo's clock frozen at the scene's moment,
    or advancing from it, recorded machine samples replayed over the fixed queue. `elapsed` (seconds since the
    start) stands in for the wall clock, so frames drawn at chosen moments are the same every time. EVENTS' rows
    are read from the scene's event log (scene_events()); `recorded` keeps the recording's snapshot-style events
    instead, which tools/goldens.py writes that log from."""
    from godfather import app, entries, sources, theme
    from godfather.views import details, machine

    data = json.loads(FIXTURE.read_text())
    profiles = json.loads(METRICS.read_text())["samples"]
    chart = json.loads(CHART_HISTORY.read_text())["steps"]
    model = data["model"]
    for item in model["holders"] + model["waiters"]:
        for key in ("since", "granted"):
            item[key] = dt.datetime.fromisoformat(item[key]) if item[key] else None
        item["key"] = (item["pid"], item["since"].isoformat())
        item["style"] = "text" if item["kind"] == "waiting" else "accent"
    for event in model["events"]:
        event["when"] = dt.datetime.fromisoformat(event["when"])
    model["updated"] = dt.datetime.fromisoformat(model["updated"])
    model["overtake_window"] = 600
    judge(model["memory"])
    model["history_window"] = 60  # short demo zoom; the legend reports seconds per column
    origin = model["updated"]
    started = time.monotonic()
    since = elapsed or (lambda: time.monotonic() - started)
    history_anchor = origin + dt.timedelta(seconds=10 if advance else 0)

    def current() -> dt.datetime:
        return origin + dt.timedelta(seconds=10 + since()) if advance else origin

    class Clock(dt.datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Any:  # type: ignore[override]
            moment = current()
            return moment.astimezone(tz) if tz else moment.astimezone().replace(tzinfo=None)

    class Date(dt.date):
        @classmethod
        def today(cls) -> Any:  # type: ignore[override]
            return current().astimezone().date()

    clock = types.SimpleNamespace(datetime=Clock, date=Date, timedelta=dt.timedelta, timezone=dt.timezone, UTC=dt.UTC)
    app.datetime = clock  # type: ignore[assignment]  # the live loop's clock, which its Tick carries to the state
    model["history"] = machine.held_history(model["events"], model["used"], origin, 2 * model["history_window"])
    if not recorded:  # EVENTS reads the log (fathers#84)
        model["events"], model["sources"] = scene_events(model), [("slotfather", "slot")]
    entries.dashboard_colours(model, {}, theme.from_environment().holders())  # GODFATHER_TERMINAL_BG/_FG

    def sample() -> dict[str, Any]:
        fresh = copy.deepcopy(model)
        fresh["updated"] = current()
        moment = max(0, since()) if advance else 0
        for observation in profiles:
            if observation["offset"] > moment + 2:  # skip process-startup warmup
                break
            fresh["averages"] = observation["averages"]
            fresh["memory"].update(observation["memory"])
            judge(fresh["memory"])
            for item, usage in zip(fresh["holders"], observation["lanes"], strict=False):
                for key in ("cpu", "rss", "procs"):
                    item[key] = usage[key]
        fresh["history"] = [(history_anchor + dt.timedelta(seconds=step["start"]),
                             history_anchor + dt.timedelta(seconds=step["end"]), step["held"]) for step in chart]
        fresh["history"].append((history_anchor, fresh["updated"], fresh["used"]))
        fresh["advice"]["unleased_load"] = max(0, min(fresh["averages"][:2]) - fresh["used"] * 2)
        return fresh

    class Scene(app.Screen):
        def load(self, _: Any = None) -> tuple[Dashboard | None, str | None]:
            fresh = sample()
            if os.environ.get("GODFATHER_FIXTURES"):
                live_model, _ = super().load()
                if live_model:
                    fresh["events"] = live_model["events"]
                    if "sources" in live_model:
                        fresh["sources"] = live_model["sources"]
            return cast("Dashboard", fresh), None

        def notice(self) -> list[str] | None:
            return None

        def load_news(self, _: Any = None) -> Any:
            return app.load_news()

    def frozen_details(_item: dict[str, Any], *_: Any, **__: Any) -> None:
        pass  # frozen lane summaries; never query a live lanefather

    details.enrich_details = frozen_details  # type: ignore[assignment]
    holders = [holder["pid"] for holder in model["holders"]]
    sources.process_cwd = lambda pid: "~/straylight" if pid in holders else None  # type: ignore[assignment]
    app.Scene = Scene  # type: ignore[attr-defined]
    app.scene_clock = current  # type: ignore[attr-defined]
    scene_news(app)
    return app, sample()


def scene_events(model: dict[str, Any]) -> list[dict[str, Any]]:
    """EVENTS' rows as the dashboard reads them from Slotfather's event log: the scene's recorded grants and releases
    written as that log (tests/replay.py, as tools/goldens.py writes the scene's), then read by godfather.events."""
    import importlib.util

    from godfather import events

    spec = importlib.util.spec_from_file_location("godfather_replay", ROOT / "tests" / "replay.py")
    assert spec and spec.loader
    replay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(replay)
    live = {item["pid"]: (item["since"], item["granted"]) for item in model["holders"]}
    found: list[Any] = []
    for order, line in enumerate(replay.lines(model["events"], live), 1):
        data = json.loads(line)
        when = events.parse_time(data["time"])
        assert when is not None
        found.append(events.Event("slotfather", order, when, data, line))
    return events.rows(found, {"slotfather": "slot"})


def scene_news(app: Any) -> tuple[Any, dict[str, str]]:
    """What's new from the recorded tool replies, never the real tools or read state: everything read, or with
    GODFATHER_DEMO_UNREAD=1 Slotfather's newest entries unread (the footer's what's new? not muted)."""
    from godfather import news

    with mock.patch.dict(os.environ, {"GODFATHER_FIXTURES": str(NEWS)}):
        found = news.load(stale=False, commit="9f600e1c0ffee")
    read = news.mark_read(found, {})
    if os.environ.get("GODFATHER_DEMO_UNREAD"):
        read["slotfather"] = "0.1.0"
    def load_news(_: Any = None) -> Any:
        return found

    def save_read(_: dict[str, str]) -> None:
        pass  # the demo never writes the real read state

    app.load_news = load_news
    news.load_read = lambda: dict(read)  # type: ignore[assignment]
    news.save_read = save_read  # type: ignore[assignment]
    app.scene_news = (found, read)
    return found, read


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--width", type=int, default=156)
    parser.add_argument("--height", type=int, default=43)
    args = parser.parse_args()
    os.environ["TZ"] = "Etc/GMT-2"
    time.tzset()
    app, model = load_scene(advance=not args.once)
    from godfather import theme, tui
    from godfather.views import overview

    if args.once:
        canvas = tui.Canvas(args.width, args.height, theme.from_environment())  # GODFATHER_THEME, _TERMINAL_BG/_FG
        found, read = app.scene_news
        overview.dashboard_draw(canvas, cast("Dashboard", model), cwd="~/straylight", news=found, read=read)
        print(canvas.render(True))
    else:
        app.dashboard_live(1, screen=app.Scene())


if __name__ == "__main__":
    main()
