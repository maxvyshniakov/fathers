"""Writes the parity test's frozen fixture, tests/fixtures/parity/, from Slotfather 37c4df7 (see the README there).

    mkdir DIR && git -C ~/slotfather archive 37c4df7 | tar -x -C DIR
    SLOTFATHER_SRC=DIR/src python3 tests/parity_reference.py OUT

Loads Slotfather's own demo scene (tools/dashboard-demo.py, frozen at its moment), renders it with Slotfather's
in-tree dashboard at every size the test compares, and writes, from that same model:

    OUT/frames.json            {"WxH colour ascii selected": text}
    OUT/cells.json             {"WxH ascii selected": [[character, style, selected]...]}, every cell
    OUT/help.json              Slotfather's help sections
    OUT/scene.json             the moment, the chart window and the lanes the scene froze
    OUT/slotfather/replies.json  capabilities and the `status --json --full` reply (tools/slotfather_reference.py)

Godfather never imports Slotfather; only this script does.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SIZES = ((156, 43), (176, 50), (120, 40), (100, 40), (82, 30), (60, 24), (40, 12))


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(out: Path) -> int:
    for key in [key for key in os.environ if key.startswith(("SLOTFATHER_", "GODFATHER_", "COLORFGBG", "NO_COLOR"))
                and key != "SLOTFATHER_SRC"]:
        del os.environ[key]  # the theme and settings come from the scene, never this shell
    out = out.resolve()
    os.chdir(tempfile.mkdtemp())
    os.environ["TZ"] = "Etc/GMT-2"
    time.tzset()
    reference = load(ROOT / "tools" / "slotfather_reference.py", "slotfather_reference")
    reference.checkout(reference.SOURCE)
    source = Path(reference.SOURCE).resolve()
    sys.path.insert(0, str(source))
    demo = load(source.parent / "tools" / "dashboard-demo.py", "slotfather_demo")
    dashboard, model = demo.load_scene(advance=False)
    dashboard.set_theme(dashboard._theme.from_environment(environ=dict(os.environ)))
    frames: dict[str, str] = {}
    cells: dict[str, list[list[list[object]]]] = {}
    for ascii_only in (False, True):
        dashboard.use_marks(ascii_only)
        for width, height in SIZES:
            for colour in (False, True):
                for selected in (0, 3):
                    canvas = dashboard.Canvas(width, height)
                    dashboard.dashboard_draw(canvas, model, selected, cwd="~/straylight")
                    frames[f"{width}x{height} {int(colour)} {int(ascii_only)} {selected}"] = \
                        canvas.render(colour, ascii_only)
                    cells[f"{width}x{height} {int(ascii_only)} {selected}"] = canvas.cells
    dashboard.use_marks(False)
    for width, height in ((156, 43), (60, 30)):
        canvas = dashboard.Canvas(width, height)
        dashboard.dashboard_draw(canvas, model, 0, True, cwd="~/straylight")
        frames[f"{width}x{height} 0 0 0 help"] = canvas.render(False)
    lanes = {json.dumps(list(item["key"])): item.get("lane") for item in model["holders"] + model["waiters"]}
    snapshot = reference.snapshot(dashboard, model)
    out.mkdir(parents=True, exist_ok=True)
    (out / "frames.json").write_text(json.dumps(frames))
    (out / "cells.json").write_text(json.dumps(cells))
    (out / "help.json").write_text(json.dumps(dashboard.HELP))
    (out / "scene.json").write_text(json.dumps({
        "moment": reference.stamp(model["updated"]), "history_window": dashboard.HISTORY_WINDOW, "lanes": lanes,
        "styles": {json.dumps(list(item["key"])): item["style"] for item in model["holders"]}}))
    (out / "slotfather").mkdir(exist_ok=True)
    (out / "slotfather" / "replies.json").write_text(json.dumps({
        "capabilities --json": {"code": 0, "json": reference.capabilities(dashboard)},
        "status --json --full": {"code": 0, "json": snapshot}}))
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
