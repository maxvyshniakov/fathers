"""Godfather's command line against the CLI convention (docs/cli.md, common/cliconvention.py; fathers#118).

The cases listed in EXPECTED fail today, each with the ticket that fixes it (none: fathers#154's three are fixed).
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

# `events` runs with no tool on PATH (prepare()), so it reads no log: what is checked is how it takes its options.
TOOL = cliconvention.Tool("godfather", (sys.executable, str(PACKAGE / "godfather")),
                          (Command("events", json=(), option=("--source", "slotfather")),))
# `godfather events --json` prints the logs' lines as JSON Lines, a stream (SPEC.md), not one state object with `v`.
EXPECTED = {"events --json": "fathers#154"}


class CLIConvention(cliconvention.conformance(TOOL, expected=EXPECTED)):
    def prepare(self, tool: cliconvention.Tool) -> cliconvention.Tool:
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        environment = dict(os.environ, XDG_CONFIG_HOME=root, XDG_STATE_HOME=root, NO_COLOR="1", PATH=root)
        return cliconvention.Tool(tool.name, tool.argv, tool.commands, environment, root)


if __name__ == "__main__":
    unittest.main()
