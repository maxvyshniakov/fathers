"""One lease-membership rule (fathers#38): for the same process table and leases, `status --json --pid`,
`procs --json --pid` and the sweep's protection agree on which lease a process is in, and a process whose environment
cannot be read is unknown in all three; the sweep spares such a process and reports it, never stops it."""
from __future__ import annotations

import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from slotfather import macos_process
from slotfather import process as sf_process
from slotfather import procs as sf_procs
from slotfather import query as sf_query
from slotfather import sweep as sf_sweep
from slotfather import sweep_plan

BORN = "Wed Nov  4 10:00:00 2026"
UID = os.geteuid()
HOLDER, CHILD, CARRIER, PLAIN, HIDDEN = 51, 52, 53, 54, 55
OTHER, MARKED_IN_TREE, HIDDEN_IN_TREE = 57, 58, 59  # a second lease's holder; in the first's tree: B's marker, hidden
COMMANDS = {HOLDER: "python3 holder.py", CHILD: "sh -c work", CARRIER: "node /w/verify.mjs",
            PLAIN: "node /w2/verify.mjs", HIDDEN: "node /w3/verify.mjs", OTHER: "python3 other.py",
            MARKED_IN_TREE: "node b.js", HIDDEN_IN_TREE: "/bin/zsh -c make"}
Answer = tuple[list[tuple[str, str]], list[int]]  # the leases a pid is in (id, via), and the pids that are unknown
PARENTS = {HOLDER: 1, CHILD: HOLDER, CARRIER: 1, PLAIN: 1, HIDDEN: 1, OTHER: 1, MARKED_IN_TREE: HOLDER,
           HIDDEN_IN_TREE: HOLDER}


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
        self.other = self.state / "queue" / f"{2:010d}-{OTHER}.json"
        self.other.write_text(json.dumps({"pid": OTHER, "start": BORN, "count": 1, "state": "held", "label": "other",
                                          "since": "2026-11-04T10:00:00Z", "cmd": "work"}))
        proc = root / "proc"
        marker, other = f"SLOTFATHER_LEASE={self.lease}", f"SLOTFATHER_LEASE={self.other}"
        environments = {HOLDER: marker, CHILD: marker, CARRIER: f"HOME=/h\0{marker}\0PATH=/bin",
                        PLAIN: "HOME=/h\0PATH=/bin", OTHER: other, MARKED_IN_TREE: other}  # the HIDDENs' cannot be read
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
        elif sf_process.STARTS in argv:  # procs' re-check of an unknown (fathers#158)
            wanted = {int(pid) for pid in argv[argv.index("-p") + 1].split(",")}
            text = "".join(f"{pid} S {BORN}\n" for pid in rows if pid in wanted)
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
        self.assertEqual({HOLDER: "gate", CHILD: "gate", OTHER: "other", MARKED_IN_TREE: "gate",
                          HIDDEN_IN_TREE: "gate"}, protected)
        assert carriers is not None
        self.assertEqual({CARRIER: "gate", HIDDEN: None}, dict(carriers([CARRIER, PLAIN, HIDDEN])))
        self.assertTrue(self.lease.exists(), "membership reaps nothing that runs")

    def test_inside_one_lease_status_also_asks_the_others_and_procs_does_not(self) -> None:
        """Each keeps the scope it had before the one rule (fathers#38 review): `status --json --pid` looks for a
        process in one lease's tree by another lease's marker too; `procs` and the sweep only for a process in no
        lease's tree, so a hidden environment there is no unknown for them."""
        a, b = self.lease.stem, self.other.stem
        self.assertEqual(([(a, "tree"), (b, "env")], []), self.status(MARKED_IN_TREE))
        self.assertEqual(([(a, "tree")], []), self.procs(MARKED_IN_TREE))
        self.assertEqual(([(a, "tree")], [HIDDEN_IN_TREE]), self.status(HIDDEN_IN_TREE))
        self.assertEqual(([(a, "tree")], []), self.procs(HIDDEN_IN_TREE))

    def test_the_sweep_spares_and_reports_a_process_whose_lease_is_unknown(self) -> None:
        now = 1_800_000_000.0
        procs = {pid: sweep_plan.Proc(pid, PARENTS[pid], UID, 1024, "", BORN, now - 36000, COMMANDS[pid])
                 for pid in (CARRIER, PLAIN, HIDDEN)}
        snap = sweep_plan.Snapshot(procs, moment=now, uid=UID, me=(999,))
        settings: dict[str, Any] = {**sf_sweep.DEFAULTS, "classes": list(sf_sweep.CLASSES)}
        protected, carriers = sf_sweep.protection(*sf_sweep.leases(self.state))
        found = {item["pid"]: item for item in sweep_plan.plan(snap, settings, protected, carriers, live=True)}
        self.assertEqual({CARRIER: "spared", PLAIN: "stop", HIDDEN: "spared"},
                         {pid: item["action"] for pid, item in found.items()})
        self.assertIn("inside the live lease gate", found[CARRIER]["reason"])
        self.assertEqual([HIDDEN], found[HIDDEN]["lease_unknown"])
        self.assertIn(f"lease unknown: the environment of pid {HIDDEN} cannot be read", found[HIDDEN]["reason"])
        self.assertNotIn("lease_unknown", found[PLAIN])


class RuleTests(unittest.TestCase):
    def test_an_earlier_queues_lease_counts_by_its_tree_alone_for_status(self) -> None:
        def markers(pids: Any, paths: Any) -> tuple[dict[int, set[str]], list[int]]:
            return {pid: {"/old/l.json", "/new/l.json"} for pid in pids}, []
        parents = {10: 1, 11: 10, 12: 1}
        leases = [sf_process.Lease("/new/l.json", 20), sf_process.Lease("/old/l.json", 10, by_environment=False)]
        found = sf_process.lease_membership(parents, leases, [11, 12], markers)
        self.assertEqual({11: [(0, "env"), (1, "tree")], 12: [(0, "env")]}, found.leases)

    def test_env_membership_propagates_to_descendants_with_cleared_or_hidden_environ(self) -> None:
        def markers(pids: Any, paths: Any) -> tuple[dict[int, set[str]], list[int]]:
            found_map: dict[int, set[str]] = {}
            hidden: list[int] = []
            for pid in pids:
                if pid == 200:
                    found_map[pid] = {"/q/a.json"}
                elif pid in (202, 203):
                    hidden.append(pid)
                else:
                    found_map[pid] = set()
            return found_map, hidden
        # 1182 = systemd --user; 200 = detached daemon carrying lease; 201 = chrome (empty env);
        # 202 = chrome renderer (hidden env); 203 = unrelated hidden process under systemd --user.
        parents = {10: 1, 1182: 1, 200: 1182, 201: 200, 202: 201, 203: 1182}
        leases = [sf_process.Lease("/q/a.json", 10)]
        found = sf_process.lease_membership(parents, leases, [201, 202, 203], markers, detached_only=True)
        self.assertEqual({201: [(0, "env")], 202: [(0, "env")], 203: []}, found.leases)
        self.assertEqual([203], found.unknown, "descendant of leased daemon is resolved; unrelated hidden stays unknown")


def procargs(executable: bytes, arguments: list[bytes], environment: list[bytes], pad: int = 0) -> bytes:
    """A KERN_PROCARGS2 buffer as the kernel lays it out: argc, the executable's path padded to 8 bytes from its
    start (and `pad` more NULs, as a kernel that aligned further would leave), the arguments, the environment, an
    empty string and the kernel's own strings."""
    path = executable + b"\0"
    path += b"\0" * (-len(path) % 8 + pad)
    return struct.pack("=i", len(arguments)) + path + b"".join(a + b"\0" for a in arguments) + \
        b"".join(e + b"\0" for e in environment) + b"\0" + b"executable_path=/x\0\0\0"


class ProcargsTests(unittest.TestCase):
    """macos_process.procargs_environment() on byte layouts: an empty argument is an argument, never padding."""

    def test_layouts(self) -> None:
        marker = b"SLOTFATHER_LEASE=/q/a.json"
        cases: dict[str, tuple[bytes, list[bytes] | None]] = {
            "plain": (procargs(b"/usr/bin/node", [b"node", b"a.js"], [marker, b"HOME=/h"]), [marker, b"HOME=/h"]),
            "empty argv[0]": (procargs(b"/usr/bin/node", [b"", b"a.js"], [marker, b"HOME=/h"]), [marker, b"HOME=/h"]),
            "empty arguments": (procargs(b"/a", [b"", b"", b""], [marker]), [marker]),
            "path of 7 bytes": (procargs(b"/bin/ab", [b"x"], [marker]), [marker]),
            "path of 8 bytes": (procargs(b"/bin/abc", [b"", b"x"], [marker]), [marker]),
            "hidden": (procargs(b"/bin/sleep", [b"sleep", b"9"], []), None),
            "no argument": (procargs(b"/bin/x", [], [marker]), [marker]),
            "cut": (struct.pack("=i", 5) + b"/bin/x\0\0a\0", None),
            "too short": (b"\0\0", None),
        }
        for name, (raw, wanted) in cases.items():
            with self.subTest(name):
                self.assertEqual(wanted, macos_process.procargs_environment(raw))

    def test_padding_past_the_boundary_keeps_the_marker(self) -> None:
        """Further padding cannot be told from an empty argv[0]: both readings count, so the marker is not lost."""
        found = macos_process.procargs_environment(procargs(b"/usr/bin/node", [b"node"], [b"SLOTFATHER_LEASE=/q"], 8))
        self.assertIsNotNone(found)
        self.assertIn(b"SLOTFATHER_LEASE=/q", found or [])


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
