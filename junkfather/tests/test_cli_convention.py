"""Junkfather's command line against the CLI convention (docs/cli.md, common/cliconvention.py; fathers#118).

The cases listed in EXPECTED fail today, each with the ticket that fixes it (fathers#153). Like every test, the
commands run with the machine's Slotfather, agent-browser sessions, Lanefather lanes and repositories out of reach:
`sweep --json` would otherwise remove their leftovers and worktrees.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Dict
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "common"))

import cliconvention  # common/cliconvention.py
from cliconvention import Command

PACKAGE = Path(__file__).resolve().parents[1]

# `supervise` is internal (hidden from the help); `hook` reads its event from stdin.
TOOL = cliconvention.Tool("junkfather", (sys.executable, "-m", "junkfather"), (
    Command("own"), Command("own-resource"), Command("adopt"), Command("list", json=()), Command("status", json=()),
    Command("capabilities", json=()), Command("changelog", json=(), option=("--since", "0.0.1")), Command("reap"),
    Command("hand"), Command("extend"), Command("sweep", json=()), Command("settings", json=()), Command("hook"),
    Command("version"),
))
EXPECTED: Dict[str, str] = dict.fromkeys((
    "version", "unknown command", "unknown option", "own unknown option", "own-resource unknown option",
    "adopt unknown option", "list unknown option", "status unknown option", "capabilities unknown option",
    "changelog unknown option", "reap unknown option", "hand unknown option", "extend unknown option",
    "sweep unknown option", "settings unknown option", "hook unknown option", "version unknown option",
), "fathers#153")


class CLIConvention(cliconvention.conformance(TOOL, expected=EXPECTED)):
    def prepare(self, tool: cliconvention.Tool) -> cliconvention.Tool:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(("CLAUDE", "CODEX", "LANEFATHER_", "JUNKFATHER_"))}
        environment.update(JUNKFATHER_STATE=str(root / "state"), JUNKFATHER_CLAUDE_SESSIONS=str(root / "sessions"),
                           JUNKFATHER_LANE_RECORDS=str(root / "no-lanes"),
                           JUNKFATHER_SCRATCH_ROOTS=str(root / "scratch"), JUNKFATHER_REPOS="",
                           JUNKFATHER_SLOTFATHER=str(root / "no-slotfather"),
                           JUNKFATHER_AGENT_BROWSER=str(root / "no-agent-browser"),
                           JUNKFATHER_LANEFATHER_STATE=str(root / "no-lanefather"), PYTHONPATH=str(PACKAGE),
                           PYTHONDONTWRITEBYTECODE="1", NO_COLOR="1")
        return cliconvention.Tool(tool.name, tool.argv, tool.commands, environment, str(root))


if __name__ == "__main__":
    unittest.main()
