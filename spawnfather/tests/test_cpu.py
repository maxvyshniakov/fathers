"""Whose processes the busy cores belong to (fathers#98): the attribution rules on fake process tables, a fake
`slotfather` on PATH, and the report in sweep.json and `status --json`."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Dict, List
import unittest

from support import Case, proc, table

from spawnfather import cpu, procs, sweep

NOW = 1_900_000_000.0
START = "Thu Nov 12 10:00:00 2026"


def local_start(start: str) -> str:
    """The start time as Slotfather records it: ps lstart in its local time."""
    epoch = procs.started_at(start)
    assert epoch is not None
    return time.strftime(procs.LSTART, time.localtime(epoch))


class ParseTests(unittest.TestCase):
    def test_the_ps_line_carries_cpu(self):
        line = "  4242     1  4242   501  87.5 ??       Thu Nov 12 10:00:00 2026     node server.js --port 3000"
        found = procs.parse(line)[4242]
        self.assertEqual((87.5, "", "node server.js --port 3000"), (found.cpu, found.tty, found.cmd))



class RealCpuTests(Case):
    def test_a_busy_child_shows_its_cpu(self):
        child = self.spawn(sys.executable, "-c", "while True: pass")
        self.wait_for(lambda: (procs.Table.read().get(child.pid) or proc(0)).cpu > 5.0, what="the busy child's cpu",
                      seen=lambda: str(procs.Table.read().get(child.pid)))
        here = procs.Table.read()
        self.assertTrue(all(isinstance(p.cpu, float) and p.cpu >= 0 for p in here.procs.values()))


class AttributionTests(Case):
    def fixture(self) -> procs.Table:
        return table(
            proc(100, cmd="sleep 600 (owned)", cpu=50.0), proc(101, ppid=100, cmd="worker", cpu=25.0),
            proc(200, cmd="slotfather run -n 1 -- make", cpu=1.0), proc(201, ppid=200, cmd="cc1", cpu=300.0),
            proc(300, cmd="/usr/local/bin/claude", cpu=10.0), proc(301, ppid=300, cmd="node mcp.js", cpu=40.0),
            proc(400, cmd="/Applications/Spotify.app/Contents/MacOS/Spotify", cpu=120.0),
            proc(401, ppid=400, cmd="Spotify Helper", cpu=30.0), proc(500, cmd="idle", cpu=0.0),
            moment=NOW)

    def lease(self, here: procs.Table, **fields: Any) -> Dict[str, Any]:
        holder = here.get(200)
        assert holder is not None
        return {"id": "0000000001-200", "label": "jf-1 build", "pid": 200, "start": local_start(holder.start),
                "tree": [200, 201], **fields}

    def report(self, here: procs.Table, leases: Any) -> Dict[str, Any]:
        rules = [cpu.entry_rule({"j000001": ("web", [here.procs[100], here.procs[101]])})]
        if leases is not None:
            rules.append(cpu.lease_rule(leases, here))
        rules.append(cpu.session_rule(here, [{"pid": 300, "sessionId": "sess-1"}]))
        return cpu.report(here, rules, {400: "/Users/me"})

    def test_each_tree_goes_to_its_first_claimer(self):
        here = self.fixture()
        found = self.report(here, [self.lease(here)])
        self.assertEqual({"entry": 0.75, "lease": 3.01, "session": 0.5}, found["attributed"])
        self.assertEqual(1.5, found["unattributed"])
        self.assertEqual(5.76, found["cores"])
        owners = {(o["kind"], o["id"]): o["cores"] for o in found["owners"]}
        self.assertEqual({("entry", "j000001"): 0.75, ("lease", "0000000001-200"): 3.01,
                          ("session", "sess-1"): 0.5}, owners)
        (tree,) = found["top"]
        self.assertEqual({"pid": 400, "start": START, "cmd": "/Applications/Spotify.app/Contents/MacOS/Spotify",
                          "cwd": "/Users/me", "uid": os.getuid(), "cpu": 150.0, "processes": 2}, tree)

    def test_an_entry_wins_over_the_lease_and_the_session_above_it(self):
        here = table(proc(300, cmd="/usr/local/bin/claude", cpu=1.0),
                     proc(200, ppid=300, cmd="slotfather run -- make", cpu=1.0),
                     proc(100, ppid=200, cmd="server", cpu=80.0), moment=NOW)
        lease = {"id": "L", "pid": 200, "start": local_start(START), "tree": [200, 100]}
        rules = [cpu.entry_rule({"j1": (None, [here.procs[100]])}), cpu.lease_rule([lease], here),
                 cpu.session_rule(here, [])]
        found = cpu.report(here, rules, {})
        self.assertEqual({"entry": 0.8, "lease": 0.01, "session": 0.01}, found["attributed"])
        self.assertEqual("pid 300", next(o for o in found["owners"] if o["kind"] == "session")["id"])

    def test_a_lease_whose_holder_pid_was_reused_claims_nothing(self):
        here = self.fixture()
        found = self.report(here, [self.lease(here, start="Mon Jan  5 01:00:00 2026")])
        self.assertEqual(0.0, found["attributed"]["lease"])
        self.assertEqual(3.01 + 1.5, found["unattributed"])

    def test_the_top_trees_are_cut_to_ten_and_their_commands_to_120_characters(self):
        here = table(*[proc(1000 + i, cmd="x" * 300, cpu=float(i + 1)) for i in range(15)], moment=NOW)
        found = cpu.report(here, [], {})
        self.assertEqual(10, len(found["top"]))
        self.assertEqual([15.0, 14.0], [t["cpu"] for t in found["top"][:2]])
        self.assertEqual(120, len(found["top"][0]["cmd"]))


class SlotfatherTests(Case):
    def fake_slotfather(self, entries: List[Any], code: int = 0) -> Path:
        path = self.tmp / "bin" / "slotfather"
        path.parent.mkdir(exist_ok=True)
        path.write_text(f"#!{sys.executable}\nimport sys\nprint({json.dumps(json.dumps({'v': 2, 'entries': entries}))})"
                        f"\nsys.exit({code})\n")
        path.chmod(0o755)
        return path

    def test_leases_come_from_the_cli_on_path(self):
        path = self.fake_slotfather([{"id": "a", "pid": 1, "start": "x"}, "junk"])
        os.environ["SPAWNFATHER_SLOTFATHER"] = str(path)
        self.assertEqual(([{"id": "a", "pid": 1, "start": "x"}], ""), cpu.leases())
        os.environ["SPAWNFATHER_SLOTFATHER"] = "slotfather"
        old = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{path.parent}{os.pathsep}{old}"
        self.addCleanup(os.environ.__setitem__, "PATH", old)
        self.assertEqual("a", (cpu.leases()[0] or [{}])[0]["id"])

    def test_slotfather_absent_or_failing_leaves_the_lease_step_out_and_says_so(self):
        self.assertEqual((None, "slotfather is not installed here"), cpu.leases())
        os.environ["SPAWNFATHER_SLOTFATHER"] = str(self.fake_slotfather([], code=3))
        self.assertEqual((None, "slotfather status --json exited 3"), cpu.leases())
        here = table(proc(200, cmd="make", cpu=100.0), moment=NOW)
        report = sweep.sweep(self.store, table=here, cwds={}, sessions=[], now=NOW)
        self.assertEqual(["Slotfather leases are left out: slotfather status --json exited 3"],
                         report["cpu"]["notes"])
        self.assertNotIn("lease", report["cpu"]["attributed"])
        self.assertEqual(1.0, report["cpu"]["unattributed"])

    def test_the_sweep_attributes_a_lease_and_writes_the_report_for_status(self):
        here = table(proc(200, cmd="slotfather run -- make", cpu=0.0), proc(201, ppid=200, cmd="cc1", cpu=200.0),
                     moment=NOW)
        lease = {"id": "q-200", "label": "build", "pid": 200, "start": local_start(START), "tree": [200, 201]}
        os.environ["SPAWNFATHER_SLOTFATHER"] = str(self.fake_slotfather([lease]))
        report = sweep.sweep(self.store, table=here, cwds={}, sessions=[], now=NOW)
        self.assertEqual({"entry": 0.0, "lease": 2.0, "session": 0.0}, report["cpu"]["attributed"])
        self.assertIn("cpu: 2.0 cores busy; entry 0.0, lease 2.0, session 0.0, unattributed 0.0",
                      sweep.text(report))
        status = json.loads(self.cli("status", "--json").stdout)
        self.assertEqual(2, status["v"])
        self.assertEqual(report["cpu"], status["cpu"])
        self.assertEqual({"v", "at", "cores", "attributed", "unattributed", "owners", "top", "notes"},
                         set(status["cpu"]))
        self.assertEqual({"kind", "id", "label", "cores", "processes"}, set(status["cpu"]["owners"][0]))
        caps = json.loads(self.cli("capabilities", "--json").stdout)
        self.assertEqual([2], caps["dashboard"]["snapshot"]["versions"])

    def test_status_before_any_sweep_has_no_cpu_report(self):
        self.assertIsNone(json.loads(self.cli("status", "--json").stdout)["cpu"])


if __name__ == "__main__":
    unittest.main()
