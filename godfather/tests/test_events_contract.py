"""`godfather events --follow` against the real tools (fathers#83), each in an isolated state directory: a granted and
released lease from Slotfather and an owned process from junkfather show up within a second, in time order, with
their tags, and Lanefather's capabilities name a log the reader follows. It runs the sibling packages of the fathers
repository at the same commit (../slotfather, ../junkfather, ../lanefather) and skips a tool that is missing there or
too old to name its event log. A Lanefather lane check needs a whole project fixture (lanefather's own tests have it),
so its event is checked by lanefather/tests/test_events.py against the shared validator, not here."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Optional

from helpers import ROOT

REPOSITORY = ROOT.parent


def wrapper(folder: Path, tool: str) -> Optional[Path]:
    """`tool` as the sibling package runs it, on a PATH of its own; None where the package is not there."""
    target = folder / tool
    if tool == "slotfather":
        script = REPOSITORY / "slotfather" / "slotfather"
        if not script.is_file():
            return None
        target.symlink_to(script)
        return target
    package = REPOSITORY / tool
    if not (package / tool / "__main__.py").is_file():
        return None
    target.write_text(f'#!/bin/sh\nPYTHONPATH="{package}" exec "{sys.executable}" -m {tool} "$@"\n')
    target.chmod(0o755)
    return target


class EventsContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="godfather-events-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "bin").mkdir()
        (self.tmp / "home").mkdir()
        self.environ = {key: value for key, value in os.environ.items()
                        if not key.startswith(("SLOTFATHER_", "GODFATHER_", "JUNKFATHER_", "LANEFATHER_"))}
        self.environ.update(PATH=f"{self.tmp / 'bin'}:/usr/bin:/bin", HOME=str(self.tmp / "home"),
                            SLOTFATHER_DIR=str(self.tmp / "slot"), SLOTFATHER_LEGACY_DIRS="", SLOTFATHER_CORES="12",
                            SLOTFATHER_MEMORY_GIB="64", JUNKFATHER_STATE=str(self.tmp / "junk"),
                            LANEFATHER_EVENTS=str(self.tmp / "lane" / "events.jsonl"),
                            XDG_CONFIG_HOME=str(self.tmp / "config"), PYTHONPATH=str(ROOT / "src"))
        self.tools = {tool: wrapper(self.tmp / "bin", tool) for tool in ("slotfather", "lanefather", "junkfather")}
        for tool, path in self.tools.items():
            if path is None or self.log(tool) is None:
                self.skipTest(f"../{tool} with an event log in its capabilities is not in this checkout")

    def log(self, tool: str) -> Optional[str]:
        try:
            result = subprocess.run([tool, "capabilities", "--json"], capture_output=True, text=True, timeout=30,
                                    env=self.environ)
            found: Any = json.loads(result.stdout) if result.returncode == 0 else None
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return None
        events = ((found or {}).get("dashboard") or {}).get("events") or {}
        return events.get("path") if 1 in (events.get("versions") or []) else None

    def run_tool(self, *argv: str) -> None:
        result = subprocess.run(list(argv), capture_output=True, text=True, timeout=60, env=self.environ,
                                cwd=self.tmp)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_the_tools_events_reach_follow_within_a_second_in_time_order(self) -> None:
        self.assertEqual(str(self.tmp / "lane" / "events.jsonl"), self.log("lanefather"))
        follow = subprocess.Popen([sys.executable, "-m", "godfather", "events", "--follow"], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, env=self.environ, cwd=self.tmp)
        assert follow.stdout is not None and follow.stderr is not None
        self.addCleanup(follow.stderr.close)
        self.addCleanup(follow.stdout.close)
        self.addCleanup(follow.wait)
        self.addCleanup(follow.kill)
        time.sleep(1.0)  # its first read is done: what follows is what it follows
        self.run_tool("slotfather", "run", "-n", "1", "--label", "contract", "--", "true")
        done = time.monotonic()
        lines = [follow.stdout.readline(), follow.stdout.readline()]
        self.assertLess(time.monotonic() - done, 1.0 + 0.5, "within a second (and the poll's quarter)")
        self.run_tool("junkfather", "own", "--ttl", "60", "--label", "contract", "--", "true")
        lines.append(follow.stdout.readline())
        self.assertEqual([("slot", "lease.granted"), ("slot", "lease.released"), ("junk", "entry.owned")],
                         [tuple(line.split()[2:4]) for line in lines], lines)
        self.assertTrue(all("contract" in line for line in lines), lines)
        times = [line[:19] for line in lines]
        self.assertEqual(sorted(times), times, "in time order")


if __name__ == "__main__":
    unittest.main()
