"""Slotfather's command line against the CLI convention (docs/cli.md, common/cliconvention.py; fathers#118).

The cases listed in EXPECTED fail today; fathers#124 makes them pass and empties the list.
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

# `dashboard` is left out: it hands its arguments to the godfather on PATH. `sweep` and `lane-admission` print state
# only after inspecting the machine or reading stdin, so only their help and refusals are checked here.
TOOL = cliconvention.Tool("slotfather", (sys.executable, str(PACKAGE / "slotfather")), (
    Command("run"), Command("yield"), Command("session"), Command("status", json=()),
    Command("capabilities", json=()), Command("settings", json=()), Command("reprioritize"), Command("ask-yield"),
    Command("procs", json=()), Command("lane-admission"), Command("version", json=()),
    Command("changelog", json=(), option=("--since", "0.0.1")), Command("history"),
    Command("stats", json=(), option=("--label", "x")), Command("slots", json=()), Command("available"),
    Command("advise", json=()), Command("order", json=()), Command("sweep"),
))
EXPECTED: dict[str, str] = {}


class CLIConvention(cliconvention.conformance(TOOL, expected=EXPECTED)):
    def prepare(self, tool: cliconvention.Tool) -> cliconvention.Tool:
        state = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, state, True)
        environment = {key: value for key, value in os.environ.items() if not key.startswith("SLOTFATHER_")}
        environment.update(SLOTFATHER_DIR=state, SLOTFATHER_LEGACY_DIRS="", NO_COLOR="1")
        return cliconvention.Tool(tool.name, tool.argv, tool.commands, environment, state)


if __name__ == "__main__":
    unittest.main()
