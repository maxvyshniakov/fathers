"""One lease-membership rule (fathers#38): for the same process table and leases, `status --json --pid`,
`procs --json --pid` and the sweep's protection agree on which lease a process is in, and a process whose environment
cannot be read is unknown in all three; the sweep spares such a process and reports it, never stops it."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from slotfather import process as sf_process
from slotfather import procs as sf_procs
from slotfather import query as sf_query
from slotfather import sweep as sf_sweep
from slotfather import sweep_plan

BORN = "Wed Nov  4 10:00:00 2026"
UID = os.geteuid()
HOLDER, CHILD, CARRIER, PLAIN, HIDDEN = 51, 52, 53, 54, 55
COMMANDS = {HOLDER: "python3 holder.py", CHILD: "sh -c work", CARRIER: "node /w/verify.mjs",
            PLAIN: "node /w2/verify.mjs", HIDDEN: "node /w3/verify.mjs"}
Answer = tuple[list[tuple[str, str]], list[int]]  # the leases a pid is in (id, via), and the pids that are unknown
PARENTS = {HOLDER: 1, CHILD: HOLDER, CARRIER: 1, PLAIN: 1, HIDDEN: 1}


class MembershipTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="slotfather-membership-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.state = root / "state"
        (self.state / "queue").mkdir(parents=True)
        self.lease = self.state / "queue" / f"{1:010d}-{HOLDER}.json"
        self.lease.write_text(json.dumps({"pid": HOLDER, "start": BORN, "count": 1, "state": "held", "label": "gate",
                                          "since": "2026-11-04T10:00:00Z", "cmd": "work"}))
        proc = root / "proc"
        marker = f"SLOTFATHER_LEASE={self.lease}"
        environments = {HOLDER: marker, CHILD: marker, CARRIER: f"HOME=/h\0{marker}\0PATH=/bin",
                        PLAIN: "HOME=/h\0PATH=/bin"}  # HIDDEN's cannot be read
        for pid, text in environments.items():
            (proc / str(pid)).mkdir(parents=True)
            (proc / str(pid) / "environ").write_bytes(text.encode() + b"\0")
        for patcher in (mock.patch.dict(os.environ, {"SLOTFATHER_DIR": str(self.state), "SLOTFATHER_LEGACY_DIRS": "",
                                                     "SLOTFATHER_PROC": str(proc), "SLOTFATHER_CORES": "4",
                                                     "SLOTFATHER_MEMORY_GIB": "64"}),
                        mock.patch("sys.platform", "linux"), mock.patch("subprocess.run", side_effect=self.ps)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def ps(self, argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        """The one process table, in the columns each reader asks for."""
        rows = {**PARENTS, os.getpid(): 1}
        if sf_process.IDENTITY in argv:
            text = "".join(f"{pid} {ppid} S {BORN}\n" for pid, ppid in rows.items())
        elif sf_process.MEMBERS in argv:
            text = "".join(f"{pid} {ppid} {UID} ?? S {BORN} {COMMANDS.get(pid, 'python3 -m unittest')}\n"
                           for pid, ppid in rows.items())
        else:
            raise AssertionError(f"unexpected command {argv}")
        return subprocess.CompletedProcess(argv, 0, text, "")

    def status(self, pid: int) -> Answer:
        found = sf_query.status_json(self.state, pid=pid)
        entries: list[dict[str, Any]] = found["entries"]
        unknown: list[int] = found.get("lease_unknown", [])
        return [(item["id"], item["via"]) for item in entries], unknown

    def procs(self, pid: int) -> Answer:
        found = sf_procs.query(pid=pid, directory=self.state)
        records: list[dict[str, Any]] = found["processes"]
        leases: list[dict[str, Any]] = records[0]["leases"] if records else []
        unknown: list[int] = found["lease_unknown"]
        return [(item["id"], item["via"]) for item in leases], unknown

    def test_status_procs_and_the_sweep_agree_and_a_hidden_environment_is_unknown_in_all_three(self) -> None:
        name = self.lease.stem
        expected: dict[int, Answer] = {CHILD: ([(name, "tree")], []), CARRIER: ([(name, "env")], []), PLAIN: ([], []),
                    HIDDEN: ([], [HIDDEN])}
        for pid, answer in expected.items():
            with self.subTest(pid=pid):
                self.assertEqual(answer, self.status(pid), "status --json --pid")
                self.assertEqual(answer, self.procs(pid), "procs --json --pid")
        protected, carriers = sf_sweep.protection(*sf_sweep.leases(self.state))
        self.assertEqual({HOLDER: "gate", CHILD: "gate"}, protected)
        assert carriers is not None
        self.assertEqual({CARRIER: "gate", HIDDEN: None}, dict(carriers([CARRIER, PLAIN, HIDDEN])))
        self.assertTrue(self.lease.exists(), "membership reaps nothing that runs")

    def test_the_sweep_spares_and_reports_a_process_whose_lease_is_unknown(self) -> None:
        now = 1_800_000_000.0
        procs = {pid: sweep_plan.Proc(pid, PARENTS[pid], UID, 1024, "", BORN, now - 36000, COMMANDS[pid])
                 for pid in (CARRIER, PLAIN, HIDDEN)}
        snap = sweep_plan.Snapshot(procs, moment=now, uid=UID, me=(999,))
        settings: dict[str, Any] = {**sf_sweep.DEFAULTS, "classes": list(sweep_plan.CLASSES)}
        protected, carriers = sf_sweep.protection(*sf_sweep.leases(self.state))
        found = {item["pid"]: item for item in sweep_plan.plan(snap, settings, protected, carriers, live=True)}
        self.assertEqual({CARRIER: "spared", PLAIN: "stop", HIDDEN: "spared"},
                         {pid: item["action"] for pid, item in found.items()})
        self.assertIn("inside the live lease gate", found[CARRIER]["reason"])
        self.assertEqual([HIDDEN], found[HIDDEN]["lease_unknown"])
        self.assertIn(f"lease unknown: the environment of pid {HIDDEN} cannot be read", found[HIDDEN]["reason"])
        self.assertNotIn("lease_unknown", found[PLAIN])


class MacosMarkerTests(unittest.TestCase):
    """macOS: ps -wwE prints nothing for an environment it may not show, so only the kernel's copy proves that a
    process without the marker does not carry it."""

    def markers(self, shown: str, native: dict[int, list[bytes] | None]) -> tuple[dict[int, set[str]], list[int]]:
        answer = subprocess.CompletedProcess(["ps"], 0, shown, "")

        def environment(pid: int) -> list[bytes] | None:
            return native.get(pid)
        with mock.patch("sys.platform", "darwin"), mock.patch.dict(os.environ, {"SLOTFATHER_PROC": ""}), \
                mock.patch("subprocess.run", return_value=answer), \
                mock.patch("slotfather.macos_process.environment", side_effect=environment):
            return sf_process.lease_markers([61, 62, 63], ["/q/a.json", "/q/b.json"])

    def test_a_shown_marker_a_readable_environment_and_a_hidden_one(self) -> None:
        found, unknown = self.markers("61 node a.js SLOTFATHER_LEASE=/q/a.json HOME=/h\n62 node b.js\n63 sleep 9\n",
                                      {62: [b"HOME=/h", b"PATH=/bin"], 63: None})
        self.assertEqual({61: {"/q/a.json"}, 62: set()}, found)
        self.assertEqual([63], unknown, "a hidden environment is unknown, never 'not carrying'")
        self.assertEqual({61}, {pid for pid, marked in found.items() if marked})

    def test_a_ps_that_fails_leaves_every_environment_unknown(self) -> None:
        with mock.patch("sys.platform", "darwin"), mock.patch.dict(os.environ, {"SLOTFATHER_PROC": ""}), \
                mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("ps", 5)):
            self.assertEqual(({}, [61, 62]), sf_process.lease_markers([61, 62], ["/q/a.json"]))


if __name__ == "__main__":
    unittest.main()
