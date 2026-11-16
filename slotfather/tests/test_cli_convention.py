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
EXPECTED: dict[str, str] = dict.fromkeys((
    "version", "unknown command", "unknown option", "run --help", "run unknown option", "yield --help",
    "yield unknown option", "session --help", "session unknown option", "status --help", "status unknown option",
    "capabilities --help", "capabilities unknown option", "capabilities without --json", "settings --help",
    "settings unknown option", "settings without --json", "reprioritize --help", "reprioritize unknown option",
    "ask-yield --help", "ask-yield unknown option", "procs --help", "procs unknown option", "procs without --json",
    "lane-admission --help", "lane-admission unknown option", "version --help", "version unknown option",
    "changelog --help", "changelog unknown option", "changelog without --json", "changelog --opt=value",
    "history --help", "history unknown option", "stats unknown option", "stats --json", "stats --opt=value",
    "slots --help", "slots unknown option", "slots --json", "available --help", "available unknown option",
    "advise --help", "advise unknown option", "advise --json", "order --help", "order unknown option",
    "order --json", "sweep unknown option",
), "fathers#124")


class CLIConvention(cliconvention.conformance(TOOL, expected=EXPECTED)):
    def prepare(self, tool: cliconvention.Tool) -> cliconvention.Tool:
        state = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, state, True)
        environment = {key: value for key, value in os.environ.items() if not key.startswith("SLOTFATHER_")}
        environment.update(SLOTFATHER_DIR=state, SLOTFATHER_LEGACY_DIRS="", NO_COLOR="1")
        return cliconvention.Tool(tool.name, tool.argv, tool.commands, environment, state)


if __name__ == "__main__":
    unittest.main()
