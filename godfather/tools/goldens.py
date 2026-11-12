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
(fathers#102). tests/fixtures/news/ is what the tools answer for what's new (`changelog --json`, or only their
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
from typing import Any, Iterator
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
SCENE = ROOT / "tests" / "fixtures" / "scene"
NEWS = ROOT / "tests" / "fixtures" / "news"
GOLDENS = ROOT / "tests" / "goldens"
COMMIT = "9f600e1c0ffee"  # Godfather's own install commit in what's new, so a commit changes no golden
THEMES = {"seti": ("#111213", "#cacecd"), "prolight": ("#ffffff", "#191919")}  # tools/screens.mjs
SCREENS = {  # name: (width, height, overlay); what's new's filter after a colon
    "overview-156x43": (156, 43, None), "overview-100x40": (100, 40, None), "overview-76x40": (76, 40, None),
    "overview-156x20": (156, 20, None), "help-156x43": (156, 43, "help"), "settings-156x43": (156, 43, "settings"),
    "unread-156x43": (156, 43, None), "unread-100x40": (100, 40, None), "unread-76x40": (76, 40, None),
    "whatsnew-156x43": (156, 43, "changelog"), "whatsnew-76x40": (76, 40, "changelog"),
    "whatsnew-godfather-156x43": (156, 43, "changelog:godfather"),
}


def scene_news() -> tuple[Any, dict[str, str], dict[str, str]]:
    """What's new as the fixture tools answer it, with everything read, and with Slotfather's newest unread."""
    from godfather import news

    with mock.patch.dict(os.environ, {"GODFATHER_FIXTURES": str(NEWS)}), \
            mock.patch("godfather.version.running_identity", return_value={"commit": COMMIT, "version": "0.1.0"}):
        found = news.load((SCENE / "CHANGELOG.md").read_text(), stale=False)
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
    _, model = demo.load_scene(advance=False)
    block = stamp({key: value for key, value in model.items() if key != "installed"})
    lanes = {}
    for item in block["holders"] + block["waiters"]:
        lanes[json.dumps(item["key"])] = item.pop("lane", None)
        item.pop("style", None)
    snapshot = {"v": 2, "host": "mbp", "time": block["updated"], "slots": block["slots"], "free": block["free"],
                "held": block["used"], "draining": 0, "dashboard": block}
    state = fake_state({"cores": 12, "memory_gib": 64, "capacity": {"slots": block["slots"]}, "order": {},
                        "writable": True, "environment": ["SLOTFATHER_CORES", "SLOTFATHER_MEMORY_GIB"]})
    block.update(v=1, draining=0, basis=fake.basis(state))
    capabilities = {"v": 1, "tool": "slotfather", "version": "0.2.0", "dashboard": {
        "v": 1, "tag": "slot", "snapshot": {"argv": ["status", "--json", "--full"], "versions": [2], "poll": 1},
        "settings": {"versions": [1]}, "actions": ["reprioritize"]}}
    (SCENE / "slotfather").mkdir(parents=True, exist_ok=True)
    (SCENE / "slotfather" / "replies.json").write_text(json.dumps({
        "capabilities --json": {"code": 0, "json": capabilities},
        "status --json --full": {"code": 0, "json": snapshot},
        "settings --json": {"code": 0, "json": fake.settings(state)}}, indent=1) + "\n")
    (SCENE / "CHANGELOG.md").write_text((ROOT / "CHANGELOG.md").read_text())
    (SCENE / "scene.json").write_text(json.dumps({"moment": block["updated"], "history_window": 60,
                                                  "lanes": lanes}, indent=1) + "\n")


def fake_state(state: dict[str, Any]) -> dict[str, Any]:
    os.environ.pop("SLOTFATHER_QUEUE", None)  # the fake reads it as the environment's queue order
    return dict(state, version="0.2.0")


@contextlib.contextmanager
def environment(theme: str | None) -> Iterator[None]:
    keep = {key: value for key, value in os.environ.items()
            if not key.startswith(("SLOTFATHER_", "GODFATHER_", "COLORFGBG", "NO_COLOR"))}
    keep.update(GODFATHER_FIXTURES=str(SCENE), TZ="Etc/GMT-2", HOME="/nonexistent")
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
    """One screen in one theme (None: plain text), as the live view would draw it at the scene's moment."""
    from godfather import app, state, theme as themes, tui, version
    from godfather.adapters import slotfather as adapter
    from godfather.views import common, machine, overlays, overview

    width, height, overlay = SCREENS[name]
    found, everything, unread = scene_news()
    read = everything if name.startswith(("overview-", "help-", "settings-")) else unread
    scene = json.loads((SCENE / "scene.json").read_text())
    with environment(theme):
        tui.set_theme(themes.from_environment())
        common.use_marks(False)
        screen = app.Screen()
        model, error = screen.load()
        assert model is not None and error is None, error
        common.dashboard_colours(model, {})
        for item in model["holders"] + model["waiters"]:
            lane = scene["lanes"].get(json.dumps(list(item["key"])))
            if lane:
                item["lane"] = lane
        moment = adapter.parse_time(scene["moment"])
        assert moment is not None
        clock = load(ROOT / "tests" / "helpers.py", "golden_helpers").frozen(moment)
        shown: object = None
        with mock.patch.object(tui, "datetime", clock), mock.patch.object(overview, "datetime", clock), \
                mock.patch.object(machine, "HISTORY_WINDOW", scene["history_window"]), \
                mock.patch.object(version, "changelog_text", (SCENE / "CHANGELOG.md").read_text):
            if overlay == "settings":
                settings = screen.settings(1.0, "auto")
                settings.location = lambda: "~/.local/state/slotfather"  # type: ignore[method-assign]
                shown = state.SettingsOverlay(settings)
            elif overlay and overlay.startswith("changelog"):
                tool = overlay.partition(":")[2] or "all"
                shown = state.Changelog(tab=overlays.filters(found).index(tool))
            elif overlay:
                shown = state.Help()
            canvas = tui.Canvas(width, height)
            overview.dashboard_draw(canvas, model, 0, shown, cwd="~/straylight", news=found, read=read)
            return canvas.render(theme is not None)


def goldens() -> dict[str, str]:
    found = {}
    for name in SCREENS:
        found[f"{name}.txt"] = render(name, None) + "\n"
        for theme in THEMES:
            found[f"{name}.{theme}.ansi"] = render(name, theme) + "\n"
    return found


def main(argv: list[str]) -> int:
    if argv == ["--check"]:
        bad = [name for name, text in goldens().items()
               if not (GOLDENS / name).is_file() or (GOLDENS / name).read_text() != text]
        for name in bad:
            print(f"differs: tests/goldens/{name}", file=sys.stderr)
        return 1 if bad else 0
    write_scene()
    GOLDENS.mkdir(parents=True, exist_ok=True)
    for name, text in goldens().items():
        (GOLDENS / name).write_text(text)
    print(f"wrote {SCENE.relative_to(ROOT)} and {len(SCREENS) * 3} goldens in {GOLDENS.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
