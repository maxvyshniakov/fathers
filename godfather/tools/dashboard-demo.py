#!/usr/bin/env python3
"""Slotfather's recorded navigation scene in Godfather's real interactive dashboard, for the README media.

    python3 tools/dashboard-demo.py [--once] [--width N] [--height N]    the scene, live or one frame
    python3 tools/dashboard-demo.py --screenshot | --animation           README media through screen-kit

The scene and its recordings are Slotfather's (tools/fixtures/README.md says where they come from), moved with the
dashboard; capture and export belong to screen-kit, this file only supplies the data. No tool is run: the model
goes straight to the overview, as the Slotfather adapter would decode it from `status --json --full`.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import time
import types
from pathlib import Path
from typing import Any
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


def load_scene(advance: bool = False) -> tuple[Any, dict[str, Any]]:
    """(the app module with the scene patched in, its first model): the demo's clock frozen at the scene's moment,
    or advancing from it, recorded machine samples replayed over the fixed queue."""
    from godfather import app, sources, tui
    from godfather.views import common, details, machine, overview

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
    machine.HISTORY_WINDOW = 60  # short demo zoom; the legend reports seconds per column
    origin = model["updated"]
    started = time.monotonic()
    history_anchor = origin + dt.timedelta(seconds=10 if advance else 0)

    def current() -> dt.datetime:
        return origin + dt.timedelta(seconds=10 + time.monotonic() - started) if advance else origin

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
    overview.datetime = clock  # type: ignore[assignment]
    tui.datetime = clock  # type: ignore[assignment]
    model["history"] = machine.held_history(model["events"], model["used"], origin)
    common.dashboard_colours(model, {})

    def sample() -> dict[str, Any]:
        fresh = copy.deepcopy(model)
        fresh["updated"] = current()
        elapsed = max(0, time.monotonic() - started) if advance else 0
        for observation in profiles:
            if observation["offset"] > elapsed + 2:  # skip process-startup warmup
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
    scene_news(app)
    return app, sample()


def scene_news(app: Any) -> tuple[Any, dict[str, str]]:
    """What's new from the recorded tool replies, never the real tools or read state: everything read, or with
    GODFATHER_DEMO_UNREAD=1 Slotfather's newest entries unread (the footer's what's new? not muted)."""
    from godfather import news

    with mock.patch.dict(os.environ, {"GODFATHER_FIXTURES": str(NEWS)}), \
            mock.patch("godfather.version.running_identity",
                       return_value={"commit": "9f600e1c0ffee", "version": "0.1.0"}):
        found = news.load(stale=False)
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
    export = parser.add_mutually_exclusive_group()
    export.add_argument("--screenshot", action="store_true")
    export.add_argument("--animation", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--width", type=int, default=156)
    parser.add_argument("--height", type=int, default=43)
    args = parser.parse_args()
    if args.screenshot or args.animation:
        tool = shutil.which("screen-kit")
        if not tool:
            parser.error("Install the screen-kit CLI first (bun link in its checkout).")
        base = "dashboard-navigation" if args.animation else "dashboard"
        extension = "webp" if args.animation else "png"
        for variant in ("", "-light"):  # the same scene in a dark and a light terminal theme (#14)
            name = base + variant
            destination = ROOT / "assets/screens" / name
            result = subprocess.run([tool, "capture", "--config", str(ROOT / "tools/screens.mjs"), "--only", name,
                                     "--out", str(destination)], cwd=ROOT, text=True, capture_output=True)
            if result.returncode:
                raise SystemExit(result.stderr)
            shutil.copyfile(destination / f"{name}.{extension}", ROOT / f"assets/dashboard{variant}.{extension}")
            print(ROOT / f"assets/dashboard{variant}.{extension}")
        return
    os.environ["TZ"] = "Etc/GMT-2"
    time.tzset()
    app, model = load_scene(advance=not args.once)
    from godfather import theme, tui
    from godfather.views import overview

    tui.set_theme(theme.from_environment())  # GODFATHER_THEME, GODFATHER_TERMINAL_BG/_FG
    if args.once:
        canvas = tui.Canvas(args.width, args.height)
        found, read = app.scene_news
        overview.dashboard_draw(canvas, model, cwd="~/straylight", news=found, read=read)
        print(canvas.render(True))
    else:
        app.dashboard_live(1, screen=app.Scene())


if __name__ == "__main__":
    main()
