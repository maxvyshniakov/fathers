#!/usr/bin/env python3
"""The screens every later change is compared against: Slotfather's recorded scene drawn by Godfather.

    python3 tools/goldens.py             write tests/fixtures/scene/ and tests/goldens/ again
    python3 tools/goldens.py --check     exit 1 when a golden differs (tests/test_goldens.py does the same)

tests/fixtures/scene/ is a GODFATHER_FIXTURES directory: the scene's `status --json --full` reply as the Slotfather
adapter reads it, capabilities and a `settings --json` reply, with scene.json holding the frozen moment and the lane
summaries the scene recorded, and CHANGELOG.md the changelog the changelog screen shows, so a new entry in the real
one changes no golden. tests/goldens/ holds, for each screen, the plain text (`.txt`) and the exact escape
sequences in the owner's two themes (`.seti.ansi`, Seti dark; `.prolight.ansi`, Pro Light): the overview wide
(156×43), at 100 and at 76 columns (one column), short (156×20), the help and Settings overlays, and what's new
(fathers#102); and, from tests/fixtures/expected/ (the scene with lease ids, expected runs and an event log of an
outlier and slower trends, fathers#108), the overview at 100 columns with an overrun lease and with a waiter
selected; and the overview with Tab's focus on EVENTS, and at 76 columns on DETAILS (fathers#139).
tests/fixtures/news/ is what the tools answer for what's new (`changelog --json`, or only their
version); the overview screens have everything read (the footer muted), the `unread-` ones and what's new have
Slotfather's newest release unread.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Iterator, cast
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
SCENE = ROOT / "tests" / "fixtures" / "scene"
EXPECTED = ROOT / "tests" / "fixtures" / "expected"  # the scene with expected runs, outliers and trends (fathers#108)
MIXED = ROOT / "tests" / "fixtures" / "mixed"  # the scene with Lanefather's and junkfather's event logs (fathers#84)
NEWS = ROOT / "tests" / "fixtures" / "news"
GOLDENS = ROOT / "tests" / "goldens"
COMMIT = "9f600e1c0ffee"  # Godfather's own install commit in what's new, so a commit changes no golden
THEMES = {"seti": ("#111213", "#cacecd"), "prolight": ("#ffffff", "#191919")}  # tools/raster.py THEMES
SCREENS = {  # name: (width, height, overlay); what's new's filter after a colon
    "overview-156x43": (156, 43, None), "overview-100x40": (100, 40, None), "overview-76x40": (76, 40, None),
    "overview-156x20": (156, 20, None), "help-156x43": (156, 43, "help"), "settings-156x43": (156, 43, "settings"),
    "unread-156x43": (156, 43, None), "unread-100x40": (100, 40, None), "unread-76x40": (76, 40, None),
    "whatsnew-156x43": (156, 43, "changelog"), "whatsnew-76x40": (76, 40, "changelog"),
    "whatsnew-godfather-156x43": (156, 43, "changelog:godfather"),
    # fathers#108: an overrun lease selected, then a waiter with an expected run; EVENTS with an outlier and trends
    "expected-100x40": (100, 40, "expected:0"), "expected-waiting-100x40": (100, 40, "expected:3"),
    # fathers#139: Tab's focus on EVENTS, and at 76 columns (no EVENTS there) on DETAILS
    "focus-events-156x43": (156, 43, "focus:events"), "focus-details-76x40": (76, 40, "focus:details"),
    # fathers#84: EVENTS with every source, wide, at 100 and at 76 columns; filtered to Lanefather while "710" is typed
    "mixed-156x43": (156, 43, "mixed"), "mixed-100x40": (100, 40, "mixed"), "mixed-76x40": (76, 40, "mixed"),
    "mixed-filter-156x43": (156, 43, "mixed:lanefather:710"),
}


def scene_news() -> tuple[Any, dict[str, str], dict[str, str]]:
    """What's new as the fixture tools answer it, with everything read, and with Slotfather's newest unread."""
    from godfather import news

    with mock.patch.dict(os.environ, {"GODFATHER_FIXTURES": str(NEWS)}):
        found = news.load((SCENE / "CHANGELOG.md").read_text(), stale=False, commit=COMMIT)
    read = news.mark_read(found, {})
    return found, read, dict(read, slotfather="0.1.0")


def load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def stamp(value: Any) -> Any:
    if isinstance(value, dt.datetime):
        return value.astimezone(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    if isinstance(value, dict):
        return {str(key): stamp(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [stamp(item) for item in value]
    return value


def write_scene() -> None:
    """The demo scene, frozen, as the fixture replies Slotfather would give (docs/contracts/slotfather.md)."""
    demo = load(ROOT / "tools" / "dashboard-demo.py", "godfather_demo")
    fake = load(ROOT / "tests" / "fake_slotfather.py", "fake_slotfather")
    _, model = demo.load_scene(advance=False, recorded=True)
    block = stamp({key: value for key, value in model.items() if key not in ("installed", "history_window")})
    lanes = {}
    for item in block["holders"] + block["waiters"]:
        lanes[lane_key(item["key"])] = item.pop("lane", None)
        item.pop("style", None)
    snapshot = {"v": 2, "host": "mbp", "time": block["updated"], "slots": block["slots"], "free": block["free"],
                "held": block["used"], "draining": 0, "dashboard": block}
    state = fake_state({"cores": 12, "memory_gib": 64, "capacity": {"slots": block["slots"]}, "order": {},
                        "writable": True, "environment": ["SLOTFATHER_CORES", "SLOTFATHER_MEMORY_GIB"]})
    block.update(v=1, draining=0, basis=fake.basis(state))
    capabilities = {"v": 1, "tool": "slotfather", "version": "0.2.0", "dashboard": {
        "v": 1, "tag": "slot", "snapshot": {"argv": ["status", "--json", "--full"], "versions": [2], "poll": 1},
        "events": {"path": "/fixture/events.jsonl", "versions": [1]},
        "settings": {"versions": [1]}, "actions": ["reprioritize"]}}
    (SCENE / "slotfather").mkdir(parents=True, exist_ok=True)
    # EVENTS reads the event log (fathers#84): the scene's grants and releases as Slotfather logs them
    live = {item["pid"]: (item["since"], item["granted"]) for item in block["holders"]}
    log = replay().lines(block["events"], live)
    (SCENE / "slotfather" / "events.jsonl").write_text("\n".join(log) + "\n")
    (SCENE / "host.json").write_text(json.dumps({"host": "mbp"}) + "\n")  # the host its event log is read for
    (SCENE / "slotfather" / "replies.json").write_text(json.dumps({
        "capabilities --json": {"code": 0, "json": capabilities},
        "status --json --full": {"code": 0, "json": snapshot},
        "settings --json": {"code": 0, "json": fake.settings(state)}}, indent=1) + "\n")
    (SCENE / "CHANGELOG.md").write_text((ROOT / "CHANGELOG.md").read_text())
    (SCENE / "scene.json").write_text(json.dumps({"moment": block["updated"], "history_window": 60,
                                                  "lanes": lanes}, indent=1) + "\n")


def replay() -> Any:
    """tests/replay.py: a recorded scene's grants and releases as Slotfather's event-log lines."""
    return load(ROOT / "tests" / "replay.py", "godfather_replay")


def lane_key(key: Any) -> str:
    """scene.json's key of a row's lane: its key as JSON, a [pid, since] pair as a list."""
    return json.dumps(list(cast(list[Any], key)) if isinstance(key, (list, tuple)) else key)


def write_expected() -> None:
    """tests/fixtures/expected/: the scene as a Slotfather with fathers#101 and fathers#106 answers it: `entries` with
    lease ids and expected runs (one lease past its p90, one waiter with an expected run), and its event log with
    lease ids, an outlier and two slower trends."""
    replies = json.loads((SCENE / "slotfather" / "replies.json").read_text())
    snapshot = replies["status --json --full"]["json"]
    block = snapshot["dashboard"]
    gate = {"key": "straylight · gate", "p50": 1080.0, "p90": 1860.0, "runs": 23, "fallback": False}
    expected = {10000: (gate, True), 10001: (gate, False),
                10003: ({"key": "prof51 · bash", "p50": 2400.0, "p90": 3300.0, "runs": 8, "fallback": True}, None)}
    snapshot["entries"] = []
    for item in block["holders"] + block["waiters"]:
        pid, since = item["key"]
        entry: dict[str, Any] = {"pid": pid, "since": since, "lease_id": f"lease-{pid}", "expected": None}
        if pid in expected:
            entry["expected"], overrun = expected[pid]
            if overrun is not None:
                entry["overrun"] = overrun
        snapshot["entries"].append(entry)
    def line(kind: str, time: str, key: str, details: dict[str, Any], level: str) -> str:
        return json.dumps({"v": 1, "time": time, "host": "mbp", "source": "slotfather", "kind": kind,
                           "subject": {"type": "run_type", "id": key}, "actor": {"type": "tool"},
                           "details": details, "level": level})

    log = [line("run.trend", "2026-11-04T09:52:30.400Z", "fathers · check · plan 3f2a",
                {"verdict": "slower", "p50_recent": 7.0, "p50_before": 5.0, "ratio": 1.4, "recent": 10,
                 "before": 30, "plan_change": {"checks": 2}, "why": "plan grew: +2 checks"}, "warn"),
           line("run.outlier", "2026-11-04T09:59:35.300Z", "straylight · gate",
                {"lease_id": "lease-21201", "ran": 73.0, "p50": 41.0, "p90": 52.0,
                 "ended": "2026-11-04T09:58:33Z", "later": [40.0, 44.0, 39.0]}, "warn"),
           line("run.trend", "2026-11-04T09:59:35.500Z", "straylight · gate",
                {"verdict": "slower", "p50_recent": 341.0, "p50_before": 258.0, "ratio": 1.32, "recent": 10,
                 "before": 30, "plan_change": None, "why": "same plan"}, "warn")]
    live = {item["pid"]: (item["since"], item["granted"]) for item in block["holders"]}
    def lease(pid: int) -> str:
        return f"lease-{pid}"

    queue = replay().lines(block["events"], live, lease=lease)
    log = sorted(queue + log, key=lambda text: json.loads(text)["time"])  # stable: a run event after its release
    EXPECTED.mkdir(parents=True, exist_ok=True)
    (EXPECTED / "slotfather").mkdir(exist_ok=True)
    (EXPECTED / "slotfather" / "replies.json").write_text(json.dumps(replies, indent=1) + "\n")
    (EXPECTED / "slotfather" / "events.jsonl").write_text("\n".join(log) + "\n")
    (EXPECTED / "host.json").write_text(json.dumps({"host": "mbp"}) + "\n")  # the host its event log is read for
    scene = json.loads((SCENE / "scene.json").read_text())
    scene["lanes"] = {lane_key(f"lease-{json.loads(key)[0]}"): lane for key, lane in scene["lanes"].items()}
    (EXPECTED / "scene.json").write_text(json.dumps(scene, indent=1) + "\n")


def write_mixed() -> None:
    """tests/fixtures/mixed/: the scene with Lanefather and junkfather present, each answering capabilities with its
    event log, and logs of a lane's start, check, land, smoke and review, a red batch gate, and owned, reported and
    reaped processes in the scene's last ten minutes (fathers#84)."""
    import shutil

    shutil.rmtree(MIXED, ignore_errors=True)
    shutil.copytree(SCENE, MIXED, ignore=shutil.ignore_patterns("CHANGELOG.md"))

    def line(source: str, kind: str, time: str, subject: dict[str, Any], details: dict[str, Any],
             level: str | None = None) -> str:
        event = {"v": 1, "time": f"2026-11-04T{time}.000Z", "host": "mbp", "source": source, "kind": kind,
                 "subject": subject, "actor": {"type": "agent", "session": "4bceaaaf"}, "details": details}
        if level:
            event["level"] = level
        return json.dumps(event, sort_keys=True)

    lane = {"type": "lane", "id": "710-speech-paste-phase", "ticket": "710", "project": "Straylight"}
    review = {"type": "lane", "id": "644-build-once", "ticket": "644", "project": "Straylight"}
    commit = {"type": "commit", "id": "cedf8b972dd8ed11d8af0725f0a9bcb5b2d0ba35", "project": "Straylight"}
    lanes = [
        line("lanefather", "lane.started", "09:50:12", lane, {"status": "active", "base": "integrate/queue-347",
                                                              "claims": ["desktop/src/**", "docs/**"]}),
        line("lanefather", "lane.checked", "09:53:40", lane, {"status": "green", "suites": ["tooling", "native"],
                                                              "checks": 14, "reused": 3, "jobs": 2}),
        line("lanefather", "lane.landed", "09:56:05", lane, {"onto": "integrate/queue-347", "merge": "cedf8b97"}),
        line("lanefather", "lane.post_merge", "09:57:48", lane, {"status": "green", "scope": "narrowed", "jobs": 2}),
        line("lanefather", "gate.finished", "09:58:10", commit, {"status": "red", "jobs": 1, "seconds": 1177},
             "error"),
        line("lanefather", "review.finished", "09:59:02", review, {"status": "finished", "findings": 2,
                                                                   "seconds": 540})]
    entry = {"type": "entry", "id": "j3f9a1c", "label": "vite dev", "pid": 90412}
    junk = [
        line("junkfather", "entry.owned", "09:51:30", entry, {"argv": ["bun", "run", "dev"], "ttl": 7200,
                                                              "keep": "owner", "mode": "detached"}),
        line("junkfather", "unowned.reported", "09:54:20", {"type": "process", "id": "88123", "pid": 88123},
             {"cmd": "node storybook dev", "why": "orphan", "age": 5400}, "warn"),
        line("junkfather", "entry.reaped", "09:57:00", {"type": "entry", "id": "j1c2d3e", "label": "simulator",
                                                         "pid": 87001}, {"reason": "ttl expired", "result": "stopped"}),
        line("junkfather", "entry.extended", "09:59:20", entry, {"ttl": 14400})]
    for tool, tag, log in (("lanefather", "lane", lanes), ("junkfather", "junk", junk)):
        (MIXED / tool).mkdir()
        capabilities = {"v": 1, "tool": tool, "version": "0.9.0", "dashboard": {
            "v": 1, "tag": tag, "snapshot": {"argv": ["status", "--json"], "versions": [1], "poll": 5},
            "events": {"path": f"/fixture/{tool}/events.jsonl", "versions": [1]}}}
        (MIXED / tool / "replies.json").write_text(json.dumps({"capabilities --json": {"code": 0, "json": capabilities}},
                                                              indent=1) + "\n")
        (MIXED / tool / "events.jsonl").write_text("\n".join(log) + "\n")


def fake_state(state: dict[str, Any]) -> dict[str, Any]:
    os.environ.pop("SLOTFATHER_QUEUE", None)  # the fake reads it as the environment's queue order
    return dict(state, version="0.2.0")


@contextlib.contextmanager
def environment(theme: str | None, scene: Path = SCENE) -> Iterator[None]:
    keep = {key: value for key, value in os.environ.items()
            if not key.startswith(("SLOTFATHER_", "GODFATHER_", "COLORFGBG", "NO_COLOR"))}
    keep.update(GODFATHER_FIXTURES=str(scene), TZ="Etc/GMT-2", HOME="/nonexistent")
    if theme:
        keep.update(GODFATHER_TERMINAL_BG=THEMES[theme][0], GODFATHER_TERMINAL_FG=THEMES[theme][1])
    with mock.patch.dict(os.environ, keep, clear=True):
        time.tzset()
        try:
            yield
        finally:
            pass
    time.tzset()


def render(name: str, theme: str | None) -> str:
    """One screen in one theme (None: plain text), as the live view would draw it at the scene's moment: the moment,
    the lanes and the chart's window go in as data, the theme and marks with the canvas, the commit what's new shows as
    an argument. No module is patched (fathers#138); the only thing set from outside is the process environment
    (environment(): the fixture folder, the time zone and the terminal's colours), which is what the tools read."""
    from godfather import app, state, theme as themes, tui
    from godfather.adapters import slotfather as adapter
    from godfather.views import common, overlays, overview

    width, height, overlay = SCREENS[name]
    selected, focus = 0, "requests"
    if overlay and overlay.startswith("focus:"):
        focus, overlay = overlay.partition(":")[2], None
    folder, found_filter = SCENE, None
    if overlay and overlay.startswith("expected"):
        folder, selected, overlay = EXPECTED, int(overlay.partition(":")[2]), None
    if overlay and overlay.startswith("mixed"):
        from godfather.views import events as events_view

        _, source, text = (overlay.split(":") + ["", ""])[:3]
        found_filter = events_view.Filter(source or None, text, editing=bool(text))
        folder, overlay = MIXED, None
    found, everything, unread = scene_news()
    read = everything if name.startswith(("overview-", "help-", "settings-", "expected", "focus-")) else unread
    scene = json.loads((folder / "scene.json").read_text())
    with environment(theme, folder):
        palette = themes.from_environment()
        screen = app.Screen()
        model, error = screen.load()
        assert model is not None and error is None, error
        common.dashboard_colours(model, {}, palette.holders())
        for item in model["holders"] + model["waiters"]:
            lane = scene["lanes"].get(lane_key(item["key"]))
            if lane:
                item["lane"] = lane
        model["history_window"] = scene["history_window"]
        moment = adapter.parse_time(scene["moment"])
        assert moment is not None
        shown: object = None
        if overlay == "settings":
            settings = screen.settings(1.0, "auto")
            settings.location = lambda: "~/.local/state/slotfather"  # type: ignore[method-assign]
            shown = state.SettingsOverlay(settings)
        elif overlay and overlay.startswith("changelog"):
            tool = overlay.partition(":")[2] or "all"
            shown = state.Changelog(tab=overlays.filters(found).index(tool))
        elif overlay:
            shown = state.Help()
        canvas = tui.Canvas(width, height, palette, common.marks_for(False))
        canvas.focus = focus
        overview.dashboard_draw(canvas, model, selected, shown, cwd="~/straylight", news=found, read=read,
                                moment=moment, events_filter=found_filter)
        return canvas.render(theme is not None)


def goldens() -> dict[str, str]:
    found = {}
    for name in SCREENS:
        found[f"{name}.txt"] = render(name, None) + "\n"
        for theme in THEMES:
            found[f"{name}.{theme}.ansi"] = render(name, theme) + "\n"
    return found


# fathers#114: box drawing and blocks as tools/raster.py draws them, no text, so the pixels are the same everywhere
RASTER = ["╭─┬──╮ ▀▂▄", "│ │  ┃ ▇█🮅", "├─┼──┤ ▌▐░", "│ │  ╹ ▖▟▒", "╰─┴──╯ ╱╲┄"]


def raster_goldens() -> dict[str, Any]:
    """tests/goldens/raster/: the geometry frame in each theme, its lines in the theme's border grey."""
    tool = load(ROOT / "tools" / "raster.py", "godfather_raster")
    found: dict[str, Any] = {}
    for name in THEMES:
        palette = tool.THEMES[name]
        text = "\n".join("\x1b[0;38;2;90;92;93m" + line + "\x1b[0m" for line in RASTER)
        found[f"geometry.{name}.png"] = tool.Raster(10, len(RASTER), palette).frame(text)
    return found


def main(argv: list[str]) -> int:
    if argv == ["--check"]:
        bad = [name for name, text in goldens().items()
               if not (GOLDENS / name).is_file() or (GOLDENS / name).read_text() != text]
        for name in bad:
            print(f"differs: tests/goldens/{name}", file=sys.stderr)
        return 1 if bad else 0
    write_scene()
    write_expected()
    write_mixed()
    GOLDENS.mkdir(parents=True, exist_ok=True)
    for name, text in goldens().items():
        (GOLDENS / name).write_text(text)
    try:
        pictures = raster_goldens()
    except ImportError:
        print("Pillow is missing: tests/goldens/raster/ left as it is (requirements-dev.txt)", file=sys.stderr)
        pictures = {}
    for name, image in pictures.items():
        (GOLDENS / "raster").mkdir(exist_ok=True)
        image.save(GOLDENS / "raster" / name, format="PNG")
    print(f"wrote {SCENE.relative_to(ROOT)} and {len(SCREENS) * 3 + len(pictures)} goldens in "
          f"{GOLDENS.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
