#!/usr/bin/env python3
"""Slotfather's recorded navigation scene in Godfather's real interactive dashboard, for the README media.

    python3 tools/dashboard-demo.py [--once] [--width N] [--height N]    the scene, live or one frame
    python3 tools/dashboard-demo.py --screenshot | --animation           README media through tools/raster.py

The scene and its recordings are Slotfather's (tools/fixtures/README.md says where they come from), moved with the
dashboard; tools/raster.py draws the frames this file makes with the real reducer and renderer. No tool is run: the model
goes straight to the overview, as the Slotfather adapter would decode it from `status --json --full`.
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
from typing import Any, Callable, Union
from unittest import mock

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
    from godfather import app, sources, theme
    from godfather.views import common, details, machine

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

    clock = types.SimpleNamespace(datetime=Clock, date=Date, timedelta=dt.timedelta, timezone=dt.timezone)
    app.datetime = clock  # type: ignore[assignment]  # the live loop's clock, which its Tick carries to the state
    model["history"] = machine.held_history(model["events"], model["used"], origin, 2 * model["history_window"])
    if not recorded:  # EVENTS reads the log (fathers#84)
        model["events"], model["sources"] = scene_events(model), [("slotfather", "slot")]
    common.dashboard_colours(model, {}, theme.from_environment().holders())  # GODFATHER_TERMINAL_BG/_FG

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
            for item, usage in zip(fresh["holders"], observation["lanes"]):
                for key in ("cpu", "rss", "procs"):
                    item[key] = usage[key]
        fresh["history"] = [(history_anchor + dt.timedelta(seconds=step["start"]),
                             history_anchor + dt.timedelta(seconds=step["end"]), step["held"]) for step in chart]
        fresh["history"].append((history_anchor, fresh["updated"], fresh["used"]))
        fresh["advice"]["unleased_load"] = max(0, min(fresh["averages"][:2]) - fresh["used"] * 2)
        return fresh

    class Scene(app.Screen):
        def load(self, _: Any = None) -> tuple[dict[str, Any] | None, str | None]:
            return sample(), None

        def notice(self) -> list[str] | None:
            return None

    def frozen_details(_item: dict[str, Any]) -> None:
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
    found = []
    for order, line in enumerate(replay.lines(model["events"], live), 1):
        data = json.loads(line)
        found.append(events.Event("slotfather", order, events.parse_time(data["time"]), data, line))
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


# The README animation (fathers#114: drawn by tools/raster.py, no terminal): (seconds, keys or a wheel scroll down over
# DETAILS as (column, row, notches)), at four frames a second for 11.5 seconds, as the screen-kit recipe it replaces drove it.
Step = tuple[float, Union[str, tuple[int, int, int]]]
NAVIGATION: list[Step] = [
    (.4, "j"), (3.7, "j"), (4, "j"), (4.3, "j"), (6.8, (149, 11, 4)), (8.3, "g"), (9, "j"), (9.4, (149, 11, 6)),
    (10.4, "g")]
MEDIA = (176, 50)  # columns and rows of the README media
FPS, DURATION, STILL = 4, 11.5, 1.0  # the animation's rate and length; the still is the screen one second in


def frames(steps: list[Step], times: list[float], width: int, height: int, palette: Any) -> list[str]:
    """The live dashboard's screens at `times` (seconds) with `steps` pressed, in colour: the real reducer and
    renderer over the scene, its clock moved by hand and every load done at once, so each run gives the same
    frames."""
    from godfather import state as _state
    from godfather.tui import Canvas
    from godfather.views import overview

    moment = [0.0]
    app, _ = load_scene(advance=True, elapsed=lambda: moment[0])
    screen = app.Scene()
    current = _state.AppState(interval=1)
    fetched = -1.0

    def model_loaded() -> Any:
        model, problem = screen.load()
        return _state.ModelLoaded(model, palette.holders(), problem)

    def send(event: Any) -> None:
        nonlocal current
        current, effects = _state.reduce(current, event, moment[0])
        for effect in effects:
            if effect.kind == "load-details":
                lane, cwd = app.load_details(effect.item)
                send(_state.DetailsLoaded(effect.key, lane, cwd, None))
            elif effect.kind == "load-news":
                send(_state.NewsLoaded(app.load_news()))
            elif effect.kind == "load-model":
                send(model_loaded())

    def draw() -> str:
        canvas = Canvas(width, height, palette)
        overview.render(canvas, current, moment[0], screen.notice())
        send(_state.Drawn(_state.Frame(canvas.width, canvas.height, canvas.tables, canvas.version_at,
                                       tuple(canvas.hint_hits), canvas.order, tuple(canvas.filter_hits))))
        return canvas.render(True)

    send(_state.NewsLoaded(app.load_news()))
    pending = sorted(steps, key=lambda step: step[0])
    found: list[str] = []
    for at in times:
        moment[0] = at
        if at - fetched >= current.interval:
            send(model_loaded())
            fetched = at
        draw()  # the frame the keys land on, as on screen
        while pending and pending[0][0] <= at:
            _, keys = pending.pop(0)
            if isinstance(keys, tuple):
                column, row, notches = keys
                for _ in range(notches):
                    send(_state.Mouse(65, column, row, True))
            else:
                send(_state.Key(keys))
        send(_state.Tick(app.scene_clock()))
        found.append(draw())
    return found


def write_media(animation: bool) -> None:
    """README media in both of the owner's themes, through tools/raster.py: the still (assets/dashboard.png) or the
    navigation animation (assets/dashboard.webp)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("godfather_raster", ROOT / "tools/raster.py")
    assert spec and spec.loader
    raster = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(raster)
    from godfather import theme

    os.environ["TZ"] = "Etc/GMT-2"
    time.tzset()
    for variant, name in (("", "seti"), ("-light", "prolight")):
        palette = raster.THEMES[name]
        os.environ.update(GODFATHER_TERMINAL_BG="#%02x%02x%02x" % palette.background,
                          GODFATHER_TERMINAL_FG="#%02x%02x%02x" % palette.foreground, GODFATHER_THEME="auto")
        drawn = theme.from_environment()  # the theme frames() draws in
        draw = raster.Raster(*MEDIA, palette)
        if animation:
            times = [step / FPS for step in range(round(DURATION * FPS))]
            target = ROOT / f"assets/dashboard{variant}.webp"
            raster.save_webp([draw.frame(text) for text in frames(NAVIGATION, times, *MEDIA, drawn)], target, FPS)
        else:
            target = ROOT / f"assets/dashboard{variant}.png"
            raster.save_png(draw.frame(frames([], [STILL], *MEDIA, drawn)[0]), target)
        print(target)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    export = parser.add_mutually_exclusive_group()
    export.add_argument("--screenshot", action="store_true")
    export.add_argument("--animation", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--width", type=int, default=156)
    parser.add_argument("--height", type=int, default=43)
    args = parser.parse_args()
    if args.screenshot or args.animation:
        write_media(args.animation)
        return
    os.environ["TZ"] = "Etc/GMT-2"
    time.tzset()
    app, model = load_scene(advance=not args.once)
    from godfather import theme, tui
    from godfather.views import overview

    if args.once:
        canvas = tui.Canvas(args.width, args.height, theme.from_environment())  # GODFATHER_THEME, _TERMINAL_BG/_FG
        found, read = app.scene_news
        overview.dashboard_draw(canvas, model, cwd="~/straylight", news=found, read=read)
        print(canvas.render(True))
    else:
        app.dashboard_live(1, screen=app.Scene())


if __name__ == "__main__":
    main()
