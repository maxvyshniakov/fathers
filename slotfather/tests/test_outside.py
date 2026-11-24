#!/usr/bin/env python3
"""Who uses the machine outside leases (fathers#239): the families a machine sample stores, from fixture process
tables (no real ps), and how machine_facts() takes them without ever failing the sample."""
from __future__ import annotations
import sys as _path_sys
from pathlib import Path as _Path
_path_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "src"))
from slotfather import outside as sf_outside
from slotfather import platform as sf_platform
from slotfather import process as sf_process
from slotfather import usage as sf_usage

import json
import os
import shutil
import tempfile
from typing import Any, Sequence
import unittest
from unittest import mock

from fathers_common.proc import Proc

UID = os.geteuid()
SINCE = 1_800_000_000.0
LEASE = "/state/queue/abc.json"
PW = "/Users/dev/Library/Caches/ms-playwright/webkit-2370"
DRIVER = f"{PW}/Playwright.app/Contents/MacOS/Playwright --inspector-pipe --headless --no-startup-window"
WEBCONTENT = (f"{PW}/Playwright.app/Contents/Frameworks/WebKit.framework/Versions/A/XPCServices/"
              "com.apple.WebKit.WebContent.xpc/Contents/MacOS/com.apple.WebKit.WebContent")
CLAUDE = "/Users/dev/.local/share/claude/versions/2.1.0"


def proc(pid: int, ppid: int, cmd: str, cpu: float = 0.0, pgid: int | None = None) -> Proc:
    return Proc(pid, ppid, pid if pgid is None else pgid, UID, "", "Mon Nov 23 09:37:00 2026", cmd, cpu,
                epoch=int(SINCE) + 5)


def table(*rows: Proc) -> dict[int, Proc]:
    return {row.pid: row for row in rows}


def no_markers(pids: Sequence[int], leases: Any) -> tuple[dict[int, set[str]], list[int]]:
    return {pid: set() for pid in pids}, []


BUSY = table(
    proc(1, 0, "/sbin/launchd", 2.0),
    proc(100, 1, f"{CLAUDE} --resume 1234"),
    proc(200, 100, "node /Users/dev/secret/build.js --token=hunter2", 80.0),
    proc(201, 100, "node /Users/dev/secret/watch.js", 50.0),
    # The lease: the holder, its command, a WebKit driver in its tree and that driver's detached XPC helper (#202).
    proc(300, 100, "python3 -m slotfather run --label e2e -- bun test", 1.0),
    proc(301, 300, "bun test", 300.0),
    proc(302, 301, DRIVER, 40.0),
    proc(400, 1, WEBCONTENT, 200.0),
    # Left the lease's tree by setsid; its environment carries the lease.
    proc(500, 1, "/Users/dev/w/node_modules/esbuild/bin/esbuild --service=0.25", 120.0),
    proc(600, 1, "/System/Library/PrivateFrameworks/SkyLight.framework/Resources/WindowServer -daemon", 60.0),
    proc(700, 1, "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", 10.0),
    proc(701, 700, "/Applications/Google Chrome.app/Contents/Frameworks/Google Chrome Framework.framework/Helpers/"
                   "Google Chrome Helper (Renderer).app/Contents/MacOS/Google Chrome Helper (Renderer) --type=renderer",
         25.0),
)


def marked_500(pids: Sequence[int], leases: Any) -> tuple[dict[int, set[str]], list[int]]:
    return {pid: ({LEASE} if pid == 500 else set()) for pid in pids}, []


class FamilyTests(unittest.TestCase):
    def test_a_family_is_the_program_or_its_outermost_app_never_an_argument(self) -> None:
        cases = {
            "node /Users/dev/secret/build.js --token=hunter2": "node",
            "/usr/local/bin/python3.12 -c 'print(1)'": "python3.12",
            "-zsh": "zsh",
            "postgres: dev app 127.0.0.1(5432) idle": "postgres",
            WEBCONTENT: "Playwright",
            "/Applications/Google Chrome.app/Contents/Frameworks/Google Chrome Framework.framework/Helpers/"
            "Google Chrome Helper (GPU).app/Contents/MacOS/Google Chrome Helper (GPU) --type=gpu-process": "Google Chrome",
            "tool --token=secret /Applications/Foo.app/Contents/MacOS/Foo": "tool",
            "tool --token=secret Foo.app/Contents/MacOS/Foo": "tool",
            "[kworker/u16:2-events]": "[kworker]",
            "[ksoftirqd/0]": "[ksoftirqd]",
            "[rcu_preempt]": "[rcu_preempt]",
            "[]": "?",
            "": "?",
        }
        for cmd, family in cases.items():
            with self.subTest(cmd=cmd):
                self.assertEqual(family, sf_outside.family(cmd))
                self.assertNotIn("--token", sf_outside.family(cmd))
        self.assertLessEqual(len(sf_outside.family("/bin/" + "x" * 200)), sf_outside.FAMILY_MAX)


class MeasureTests(unittest.TestCase):
    def measure(self, procs: dict[int, Proc], markers: Any = marked_500, spawned: Any = None,
                **extra: Any) -> dict[str, Any] | None:
        leases = [sf_process.Lease(LEASE, 300, since=SINCE)]
        return sf_outside.measure(procs, leases, markers=markers, spawned=spawned or (lambda: []), **extra)

    def test_a_sample_under_unleased_load_stores_the_families_without_leased_and_attributed_processes(self) -> None:
        with mock.patch.object(sf_platform, "macos", return_value=True):
            found = self.measure(BUSY)
        assert found is not None
        rows = {(row["family"], row["owner"]): (row["cpu"], row["processes"]) for row in found["top"]}
        self.assertEqual((1.3, 2), rows[("node", "claude 100")])
        self.assertEqual((0.6, 1), rows[("WindowServer", None)])
        self.assertEqual((0.35, 2), rows[("Google Chrome", None)])
        families = {family for family, _ in rows}
        self.assertNotIn("bun", families, "the lease's own tree is leased")
        self.assertNotIn("Playwright", families, "the #202 helper is the lease's")
        self.assertNotIn("esbuild", families, "a process carrying the lease is the lease's")
        self.assertEqual(2.27, found["cpu"])
        self.assertEqual([row["cpu"] for row in found["top"]], sorted((row["cpu"] for row in found["top"]),
                                                                       reverse=True))
        text = json.dumps(found)
        for secret in ("secret", "hunter2", "--token", "--resume", "1234", "/Users"):
            self.assertNotIn(secret, text, "no arguments or paths are stored")

    def test_a_sample_without_unleased_load_stores_none(self) -> None:
        quiet = table(proc(1, 0, "/sbin/launchd", 2.0), proc(100, 1, CLAUDE),
                      proc(200, 100, "node build.js", 60.0), proc(300, 100, "python3 -m slotfather run", 1.0),
                      proc(301, 300, "bun test", 900.0))
        asked: list[str] = []
        def spawned() -> list[dict[str, Any]]:
            asked.append("spawnfather")
            return []
        self.assertIsNone(self.measure(quiet, spawned=spawned))
        self.assertEqual([], asked, "below one core outside, Spawnfather is not asked either")

    def test_spawnfather_entries_own_their_trees_and_process_groups(self) -> None:
        procs = table(proc(1, 0, "/sbin/launchd"), proc(300, 1, "python3 -m slotfather run", 1.0),
                      proc(800, 1, "vite --port 5173", 90.0, pgid=800),
                      proc(801, 800, "esbuild --service", 30.0, pgid=800),
                      proc(802, 1, "node server.js", 40.0, pgid=800))
        spawned = [{"id": "sf-1", "pid": 800, "pgid": 800, "label": "storybook dev " + "x" * 80}]
        found = self.measure(procs, markers=no_markers, spawned=lambda: spawned)
        assert found is not None
        owners = {row["owner"] for row in found["top"]}
        self.assertEqual(1, len(owners))
        owner = owners.pop()
        assert owner is not None
        self.assertTrue(owner.startswith("spawnfather storybook dev"))
        self.assertLessEqual(len(owner), sf_outside.OWNER_MAX)

    def test_the_record_keeps_the_top_n_and_reads_environments_of_few_processes(self) -> None:
        rows = [proc(1, 0, "/sbin/launchd"), proc(300, 1, "python3 -m slotfather run", 1.0)]
        rows += [proc(1000 + n, 1, f"/opt/tool{n}", 20.0 + n) for n in range(40)]
        rows += [proc(2000 + n, 1, "/usr/bin/true", 0.1) for n in range(200)]
        asked: list[int] = []

        def markers(pids: Sequence[int], leases: Any) -> tuple[dict[int, set[str]], list[int]]:
            asked.extend(pids)
            return no_markers(pids, leases)
        found = self.measure(table(*rows), markers=markers)
        assert found is not None
        self.assertEqual(sf_outside.TOP, len(found["top"]))
        self.assertEqual("tool39", found["top"][0]["family"])
        self.assertLessEqual(len(asked), sf_outside.MARK_MAX)
        self.assertNotIn(2000, asked, "a process below MARK_MIN is not looked into")
        self.assertEqual(241, found["processes"])

    def test_an_unreadable_spawnfather_leaves_the_session_owner(self) -> None:
        def broken() -> list[dict[str, Any]]:
            raise OSError("no spawnfather")
        found = self.measure(BUSY, spawned=broken)
        assert found is not None
        self.assertIn("claude 100", {row["owner"] for row in found["top"]})

    def test_busy_in_tree_processes_do_not_exhaust_the_marker_budget_for_escaped_processes(self) -> None:
        rows = [proc(1, 0, "/sbin/launchd"), proc(300, 1, "python3 -m slotfather run", 1.0)]
        rows += [proc(301 + n, 300, "/usr/bin/clang -c a.c", 200.0 + n) for n in range(sf_outside.MARK_MAX + 8)]
        rows += [
            proc(500, 1, "/Users/dev/w/node_modules/esbuild/bin/esbuild --service=0.25", 120.0),
            proc(501, 1, "/usr/bin/gradle --daemon", 110.0),
            proc(600, 1, "/usr/bin/node unleased.js", 105.0),
        ]
        asked: list[int] = []

        def markers(pids: Sequence[int], leases: Any) -> tuple[dict[int, set[str]], list[int]]:
            asked.extend(pids)
            return {pid: ({LEASE} if pid in (500, 501) else set()) for pid in pids}, []

        found = self.measure(table(*rows), markers=markers)
        assert found is not None
        self.assertIn(500, asked)
        self.assertIn(501, asked)
        families = {row["family"] for row in found["top"]}
        self.assertNotIn("esbuild", families)
        self.assertNotIn("gradle", families)
        self.assertEqual({"node"}, families)
        self.assertEqual(1.05, found["cpu"])

    def test_spawn_owners_ignores_malformed_non_string_fields(self) -> None:
        entries: Any = [
            {"pid": 800, "pgid": 800, "label": True, "id": "sf-1"},
            {"pid": 801, "pgid": 801, "label": ["bad"], "id": 123},
            {"pid": True, "pgid": False, "label": "ignored"},
            "not-a-mapping",
        ]
        by_pid, by_group = sf_outside.spawn_owners(entries)
        self.assertEqual({800: "spawnfather sf-1", 801: "spawnfather"}, by_pid)
        self.assertEqual({800: "spawnfather sf-1", 801: "spawnfather"}, by_group)


class SpawnfatherTests(unittest.TestCase):
    def test_a_holder_asks_spawnfather_at_most_once_a_minute(self) -> None:
        calls: list[list[str]] = []

        def run(argv: list[str], **options: Any) -> Any:
            calls.append(argv)
            self.assertEqual(sf_outside.SPAWNFATHER_TIMEOUT, options["timeout"])
            return mock.Mock(returncode=0, stdout=json.dumps({"entries": [{"id": "a", "pid": 9, "state": "running"}]}))
        clock = [1000.0]
        with mock.patch.object(sf_outside, "_spawned", None), \
                mock.patch.object(sf_outside.subprocess, "run", run), \
                mock.patch.object(sf_outside.time, "monotonic", lambda: clock[0]):
            first = sf_outside.spawnfather_entries()
            clock[0] += 30
            self.assertEqual(first, sf_outside.spawnfather_entries())
            clock[0] += 31
            sf_outside.spawnfather_entries()
        self.assertEqual(2, len(calls))
        self.assertEqual(["a"], [entry["id"] for entry in first])


class MachineSampleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = _Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory, True)

    def facts(self, load: float, sample: Any) -> dict[str, Any]:
        with mock.patch.object(sf_platform, "loads", return_value=(load, load, load)), \
                mock.patch.object(sf_outside, "sample", sample):
            return sf_usage.machine_facts(self.directory)

    def test_a_machine_sample_carries_the_families_and_the_new_version(self) -> None:
        found = {"cpu": 2.0, "processes": 3, "top": [{"family": "node", "owner": None, "cpu": 2.0, "processes": 3}]}
        def sample(held: Any) -> dict[str, Any]:
            return found
        item = self.facts(6.0, sample)
        self.assertEqual(found, item["outside"])
        self.assertEqual(2, item["v"])

    def test_a_quiet_machine_takes_no_process_table(self) -> None:
        def sample(held: Any) -> None:
            raise AssertionError("ps while the machine is quiet")
        self.assertNotIn("outside", self.facts(0.5, sample))

    def test_a_fault_measuring_outside_use_never_costs_the_sample(self) -> None:
        def sample(held: Any) -> None:
            raise RuntimeError("ps broke")
        item = self.facts(8.0, sample)
        self.assertNotIn("outside", item)
        self.assertIn("ticks", item)

    def test_sample_gives_none_when_ps_fails(self) -> None:
        with mock.patch.object(sf_process, "outside_snapshot", return_value=None):
            self.assertIsNone(sf_outside.sample([]))


if __name__ == "__main__":
    unittest.main()
