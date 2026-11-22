"""Spawnfather's command line against the CLI convention (docs/cli.md, common/cliconvention.py; fathers#118).

EXPECTED lists the cases that fail, each with the ticket that fixes it: none since fathers#153. Like every test, the
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
TOOL = cliconvention.Tool("spawnfather", (sys.executable, "-m", "spawnfather"), (
    Command("own"), Command("own-resource"), Command("adopt"), Command("list", json=()), Command("status", json=()),
    Command("capabilities", json=()), Command("changelog", json=(), option=("--since", "0.0.1")), Command("reap"),
    Command("hand"), Command("extend"), Command("sweep", json=()), Command("settings", json=()), Command("hook"),
    Command("version"),
))
EXPECTED: Dict[str, str] = {}  # each case passes (fathers#153 emptied the list)


class CLIConvention(cliconvention.conformance(TOOL, expected=EXPECTED)):
    def prepare(self, tool: cliconvention.Tool) -> cliconvention.Tool:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(("CLAUDE", "CODEX", "LANEFATHER_", "SPAWNFATHER_", "JUNKFATHER_"))}
        environment.update(SPAWNFATHER_STATE=str(root / "state"), SPAWNFATHER_CLAUDE_SESSIONS=str(root / "sessions"),
                           SPAWNFATHER_LANE_RECORDS=str(root / "no-lanes"),
                           SPAWNFATHER_SCRATCH_ROOTS=str(root / "scratch"), SPAWNFATHER_REPOS="",
                           SPAWNFATHER_SLOTFATHER=str(root / "no-slotfather"),
                           SPAWNFATHER_AGENT_BROWSER=str(root / "no-agent-browser"),
                           SPAWNFATHER_LANEFATHER_STATE=str(root / "no-lanefather"), PYTHONPATH=str(PACKAGE),
                           PYTHONDONTWRITEBYTECODE="1", NO_COLOR="1")
        return cliconvention.Tool(tool.name, tool.argv, tool.commands, environment, str(root))


if __name__ == "__main__":
    unittest.main()
