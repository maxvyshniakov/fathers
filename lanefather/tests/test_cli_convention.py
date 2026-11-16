"""Lanefather's command line against the CLI convention (docs/cli.md, common/cliconvention.py; fathers#118).

The cases listed in EXPECTED fail today; fathers#130 makes them pass and empties the list.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "common"))

import cliconvention  # common/cliconvention.py
from cliconvention import Command

PACKAGE = Path(__file__).resolve().parents[1]

# Run outside any repository, with the machine files in a temporary directory: only commands that print state
# without a project are asked for --json.
TOOL = cliconvention.Tool("lanefather", (sys.executable, "-m", "lanefather"), (
    Command("capabilities", json=()), Command("changelog", json=(), option=("--since", "0.0.1")),
    Command("project-hook"), Command("start"), Command("next"), Command("adopt"), Command("check"), Command("land"),
    Command("promote"), Command("sync"), Command("perf"), Command("shipped"), Command("review"), Command("findings"),
    Command("done"), Command("gc"), Command("show"), Command("status", json=()), Command("settings"),
    Command("results"), Command("stats"), Command("list"),
))
EXPECTED = dict.fromkeys((
    "unknown command", "unknown option", "capabilities unknown option", "changelog unknown option",
    "project-hook unknown option", "start unknown option", "next unknown option", "adopt unknown option",
    "check unknown option", "land unknown option", "promote unknown option", "sync unknown option",
    "perf unknown option", "shipped unknown option", "review unknown option", "findings unknown option",
    "done unknown option", "gc unknown option", "show unknown option", "status unknown option",
    "settings unknown option", "results unknown option", "stats unknown option", "list unknown option",
), "fathers#130")


class CLIConvention(cliconvention.conformance(TOOL, expected=EXPECTED)):
    def prepare(self, tool: cliconvention.Tool) -> cliconvention.Tool:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(("LANEFATHER_", "STRAYLIGHT_"))}
        environment.update(LANEFATHER_EVENTS=str(root / "events.jsonl"),
                           LANEFATHER_REGISTRY=str(root / "projects.json"),
                           LANEFATHER_SETTINGS=str(root / "settings.json"), HOME=str(root), PYTHONPATH=str(PACKAGE),
                           GIT_CEILING_DIRECTORIES=str(root.parent), NO_COLOR="1")
        return cliconvention.Tool(tool.name, tool.argv, tool.commands, environment, str(root))


if __name__ == "__main__":
    unittest.main()
