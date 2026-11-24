"""Lanefather's command line against the CLI convention (docs/cli.md, common/cliconvention.py; fathers#118).

A case Lanefather fails would be listed in EXPECTED with its ticket; none is.
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
    Command("results"), Command("stamp"), Command("stats"), Command("list"), Command("wait"), Command("drop"), Command("park"),
    Command("unpark"), Command("abandon"), Command("recheck"), Command("smoke"), Command("keep"), Command("scope"),
))
# fathers#175 gave the usage errors the convention's two lines; nothing is expected to fail.
EXPECTED: dict[str, str] = {}


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
