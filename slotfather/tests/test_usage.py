#!/usr/bin/env python3
"""What a lease used (#635): the accounting of a process tree from fake process tables, a real tree under
`slotfather run`, the machine samples and the bounded history."""
from __future__ import annotations
import sys as _path_sys
from pathlib import Path as _Path
_path_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "src"))
from slotfather import config as sf_config
from slotfather import platform as sf_platform
from slotfather import process as sf_process
from slotfather import state as sf_state
from slotfather import usage as sf_usage


import importlib
import importlib.machinery
import importlib.util
import json
import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import threading
import time
import types
from typing import Any, Callable, Iterable
import unittest
from unittest import mock

from test_slotfather import TOOL, SlotCase

BURN = "import sys, time\nt = time.process_time()\nwhile time.process_time() - t < float(sys.argv[1]): pass\n"
DETACHED_BURN = (
    "import os, pathlib, sys, time\n"
    "t = time.process_time()\n"
    "while time.process_time() - t < float(sys.argv[1]): pass\n"
    "pathlib.Path(sys.argv[2]).write_text(f'{os.getpid()}\\n')\n"
    "release = pathlib.Path(sys.argv[3])\n"
    "deadline = time.monotonic() + 20\n"
    "while not release.exists() and time.monotonic() < deadline: time.sleep(0.05)\n"
)


def load(path: str, name: str) -> types.ModuleType:
    if pathlib.Path(path).name in ("slotfather-stats", "slotfather-sweep"):
        companion = path.rsplit("slotfather-", 1)[-1]
        return importlib.reload(importlib.import_module("slotfather." + companion))
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module



SINCE = 1_800_000_000.0
HOLDER, UID = 50, 501


def lstart(offset: float) -> str:
    """A ps lstart for SINCE + offset seconds."""
    return time.strftime("%a %b %e %H:%M:%S %Y", time.localtime(SINCE + offset))


def row(ppid: int, cpu: float, rss: int = 0, born: float = 0.0, uid: int = UID) -> tuple[int, str, int, int, float]:
    return (ppid, lstart(born), uid, rss, cpu)


class TreeUsageTests(unittest.TestCase):
    """Each table is a snapshot of ps: pid -> (ppid, start, uid, resident KB, own CPU seconds). The command is pid
    100, its holder 50; launchd is 1."""

    def usage(self, marked: Callable[[list[int]], Iterable[int]] | None = None) -> tuple[sf_usage.TreeUsage, list[list[int]]]:
        asked: list[list[int]] = []

        def mark(pids: list[int]) -> set[int]:
            asked.append(sorted(pids))
            return set(marked(pids)) if marked else set()
        return sf_usage.TreeUsage(100, SINCE, mark, uid=UID), asked

    def test_cpu_of_children_the_tree_reaps_comes_from_rusage_only(self):
        usage, _ = self.usage()
        usage.observe(SINCE + 1, {HOLDER: row(1, 0.1), 100: row(HOLDER, 1.0), 101: row(100, 2.0)})
        usage.observe(SINCE + 2, {HOLDER: row(1, 0.1), 100: row(HOLDER, 3.0)})  # 101 exited, reaped by 100
        usage.observe(SINCE + 3, {HOLDER: row(1, 0.1)})  # the command exited; this look follows wait4
        found = usage.result(reaped_cpu=10.0, wall=3.0)
        self.assertEqual((10.0, 0.0, 2, 3), (found["cpu"], found["cpu_detached"], found["processes"],
                                             found["samples"]), "a reaped child is not counted twice")

    def test_a_child_that_outlives_its_parent_counts_until_it_exits(self):
        usage, _ = self.usage()
        usage.observe(SINCE + 1, {100: row(HOLDER, 1.0), 101: row(100, 1.0), 102: row(101, 2.0)})
        # 101 exited without waiting for 102, which launchd adopted.
        usage.observe(SINCE + 2, {100: row(HOLDER, 1.5), 102: row(1, 5.0)})
        usage.observe(SINCE + 3, {100: row(HOLDER, 1.5), 102: row(1, 8.0)})
        usage.observe(SINCE + 4, {100: row(HOLDER, 2.0)})  # 102 exited; launchd reaped it, rusage never sees it
        usage.observe(SINCE + 5, {})
        found = usage.result(reaped_cpu=4.0)
        self.assertEqual((12.0, 8.0), (found["cpu"], found["cpu_detached"]))

    def test_processes_still_running_when_the_command_exits_count_up_to_the_last_look(self):
        usage, _ = self.usage()
        usage.observe(SINCE + 1, {100: row(HOLDER, 1.0), 101: row(100, 3.0), 102: row(101, 1.0)})
        usage.observe(SINCE + 2, {101: row(1, 6.0), 102: row(101, 2.0)})  # the command exited, 101 runs on
        found = usage.result(reaped_cpu=1.0)
        self.assertEqual((9.0, 8.0), (found["cpu"], found["cpu_detached"]))

    def test_launchd_children_join_the_lease_only_when_they_carry_it_and_started_during_it(self):
        usage, asked = self.usage(marked=lambda pids: {p for p in pids if p == 200})
        table = {100: row(HOLDER, 1.0),
                 200: row(1, 2.0, born=5), 201: row(200, 1.0, born=5),  # carries the lease: joins with its child
                 300: row(1, 4.0, born=5),  # started during the lease without it: someone else's
                 400: row(1, 9.0, born=-3600),  # started before the lease: never asked about
                 500: row(1, 9.0, born=5, uid=0)}  # another user's: never asked about
        usage.observe(SINCE + 10, table)
        usage.observe(SINCE + 11, {**table, 200: row(1, 3.0, born=5), 201: row(200, 1.5, born=5)})
        usage.observe(SINCE + 12, {})
        found = usage.result(reaped_cpu=0.0)
        self.assertEqual([[200, 300]], asked, "each launchd child is considered once")
        self.assertEqual((4.5, 4.5, 3), (found["cpu"], found["cpu_detached"], found["processes"]))

    def test_systemd_user_children_join_the_lease_on_linux_with_their_subtrees(self) -> None:
        usage, asked = self.usage(marked=lambda pids: {p for p in pids if p == 200})
        systemd_checks: list[int] = []

        def fake_systemd_user(pid: int) -> bool:
            systemd_checks.append(pid)
            return pid == 1182

        table = {
            1182: row(1, 0.0, born=-3600),
            100: row(HOLDER, 1.0),
            200: row(1182, 0.5, born=5),  # detached daemon adopted by systemd --user carrying lease
            201: row(200, 4.0, born=5),   # its browser child (descendant joins automatically)
            202: row(1182, 9.0, born=-3600),  # started before the lease: never asked
            203: row(1182, 3.0, born=5),  # started during lease without marker: not adopted
        }
        with mock.patch.object(sf_platform, "linux", return_value=True), \
                mock.patch.object(sf_platform, "is_systemd_user", side_effect=fake_systemd_user):
            usage.observe(SINCE + 10, table)
            usage.observe(SINCE + 11, {**table, 200: row(1182, 1.0, born=5), 201: row(200, 6.0, born=5)})
            usage.observe(SINCE + 12, {})
        found = usage.result(reaped_cpu=0.0)
        self.assertEqual([[200, 203]], asked, "systemd --user children started during lease are checked once")
        self.assertEqual([1182], systemd_checks, "systemd --user check is cached per (pid, start)")
        self.assertEqual((7.0, 7.0, 3), (found["cpu"], found["cpu_detached"], found["processes"]))

    def test_systemd_user_transient_read_failure_is_not_cached(self) -> None:
        usage, asked = self.usage(marked=lambda pids: {p for p in pids if p == 200})
        answers: list[bool | None] = [None, True]
        table = {1182: row(1, 0.0, born=-3600), 100: row(HOLDER, 1.0), 200: row(1182, 2.0, born=5)}
        with mock.patch.object(sf_platform, "linux", return_value=True), \
                mock.patch.object(sf_platform, "is_systemd_user", side_effect=lambda _pid: answers.pop(0)):
            usage.observe(SINCE + 10, table)
            usage.observe(SINCE + 11, {**table, 200: row(1182, 3.0, born=5)})
            usage.observe(SINCE + 12, {})
        self.assertEqual([[200]], asked)
        self.assertEqual(3.0, usage.result(reaped_cpu=0.0)["cpu_detached"])

    def test_is_systemd_user_reads_proc_comm_and_handles_invalid_utf8_and_non_linux(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            for pid, content in ((10, b"systemd\n"), (11, b"bash\n"), (12, b"\xff\xfe\n")):
                (root / str(pid)).mkdir()
                (root / str(pid) / "comm").write_bytes(content)
            with mock.patch.dict(os.environ, {"SLOTFATHER_PROC": str(root)}), \
                    mock.patch.object(sf_platform, "linux", return_value=True):
                self.assertIs(True, sf_platform.is_systemd_user(10))
                self.assertIs(False, sf_platform.is_systemd_user(11))
                self.assertIs(False, sf_platform.is_systemd_user(12))
                self.assertIsNone(sf_platform.is_systemd_user(999))
            with mock.patch.dict(os.environ, {"SLOTFATHER_PROC": str(root)}), \
                    mock.patch.object(sf_platform, "linux", return_value=False):
                self.assertIs(False, sf_platform.is_systemd_user(10))

    def test_a_reused_pid_is_another_process(self):
        usage, _ = self.usage()
        usage.observe(SINCE + 1, {100: row(HOLDER, 1.0), 101: row(100, 1.0), 102: row(101, 2.0)})
        usage.observe(SINCE + 2, {100: row(HOLDER, 1.0), 102: row(1, 3.0)})
        # 102 exited and its pid went to an unrelated process.
        usage.observe(SINCE + 3, {100: row(HOLDER, 1.0), 102: row(1, 50.0, born=-7200)})
        usage.observe(SINCE + 4, {})
        self.assertEqual((3.0, 3.0), (usage.result(0.0)["cpu"], usage.result(0.0)["cpu_detached"]))

    def test_the_holder_and_unrelated_processes_are_not_the_lease(self):
        usage, _ = self.usage()
        usage.observe(SINCE + 1, {HOLDER: row(1, 7.0, 9000), 100: row(HOLDER, 1.0, 100), 60: row(HOLDER, 5.0, 50),
                                  70: row(1, 5.0, 5000), 71: row(70, 1.0, 5000)})
        self.assertEqual(100, usage.rss_peak)
        self.assertEqual(1, usage.processes)

    def test_resident_memory_is_the_trees_sum_with_its_peak_and_time_weighted_mean(self):
        usage, _ = self.usage()
        usage.observe(SINCE + 0, {100: row(HOLDER, 0, 1024)})
        usage.observe(SINCE + 10, {100: row(HOLDER, 0, 1024), 101: row(100, 0, 2048)})
        usage.observe(SINCE + 20, {})
        found = usage.result(reaped_cpu=0.0, reaped_peak_kb=2048)
        self.assertEqual((3.0, 2.0), (found["rss_peak_mb"], found["rss_mean_mb"]))
        self.assertEqual(5.0, usage.result(0.0, reaped_peak_kb=5120)["rss_peak_mb"],
                         "the largest single process from rusage bounds the peak when sampling missed it")

    def test_a_command_whose_pid_was_reused_before_the_first_look_is_not_followed(self):
        usage, _ = self.usage()
        usage.root_gone = True  # wait4 returned: pid 100 is no longer the command
        usage.observe(SINCE + 1, {100: row(HOLDER, 7.0, 4096), 101: row(100, 5.0, 4096)})
        self.assertEqual((0, 0, 0.0), (usage.processes, usage.rss_peak, usage.result(1.0)["cpu_detached"]))

    def test_ps_cpu_times_parse(self) -> None:
        for text, value in (("0:00.05", 0.05), ("593:13.58", 35593.58), ("1:02:03.50", 3723.5),
                            ("2-01:00:00", 176400.0)):
            parsed = sf_platform.cpu_seconds(text)
            self.assertIsNotNone(parsed)
            assert parsed is not None
            self.assertAlmostEqual(value, parsed)
        self.assertIsNone(sf_platform.cpu_seconds("n/a"))


class WatchTests(unittest.TestCase):
    """The sampler thread against stand-ins for ps: it must never hold up the lease's release."""

    def watch(self, marked: Callable[[list[int]], set[int]] | None = None) -> sf_usage.Watch:
        usage = sf_usage.TreeUsage(100, SINCE, marked, uid=UID)
        watch = sf_usage.Watch(usage, pathlib.Path("/nonexistent"))
        watch.most = 0.01
        return watch

    def patch(self, name: str, value: Any) -> None:
        for module in [sf_usage] if name == "sample_machine" else [sf_platform, sf_process]:
            if name in vars(module):  # ps runs in process, sysctl in platform (fathers#38)
                patcher: Any = mock.patch.object(module, name, value)
                patcher.start()
                self.addCleanup(patcher.stop)

    def test_the_environment_query_runs_outside_the_lock(self) -> None:
        held: list[bool] = []
        table = {100: row(HOLDER, 1.0), 200: row(1, 1.0, born=5)}
        self.patch("usage_table", lambda: table)
        self.patch("sample_machine", lambda directory: None)
        watch: sf_usage.Watch

        def check_marked(pids: list[int]) -> set[int]:
            held.append(watch.lock.locked())
            return set()

        watch = self.watch(check_marked)
        watch.look()
        self.assertEqual([False], held)

    def test_a_stalled_ps_does_not_hold_up_finish(self) -> None:
        started = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)
        calls = 0

        def stalled_table() -> dict[int, tuple[Any, ...]]:
            nonlocal calls
            calls += 1
            started.set()
            release.wait(60)
            return {}

        self.patch("usage_table", stalled_table)
        self.patch("sample_machine", lambda directory: None)
        watch = self.watch()
        with mock.patch.object(sf_config, "WATCH_JOIN", 0.05):
            watch.start()
            self.assertTrue(started.wait(10))  # the thread is now inside the stalled ps
            rusage = mock.Mock(ru_utime=1.0, ru_stime=0.5, ru_maxrss=0)
            found = watch.finish(rusage, 2.0)
        self.assertTrue(watch.is_alive(), "finish returned without waiting for the stalled ps to exit")
        self.assertEqual(1, calls, "finish skipped the final observe() while the sampler was stuck")
        assert found is not None
        self.assertEqual(1.5, found["cpu"], "the usage so far, without a last look")

    def test_ps_calls_give_up_after_their_timeout(self) -> None:
        def stalled(*args: Any, **kwargs: Any) -> Any:
            self.assertEqual(sf_config.PS_TIMEOUT, kwargs.get("timeout"))
            raise subprocess.TimeoutExpired(args[0], float(kwargs["timeout"]))
        self.patch("subprocess", mock.Mock(run=stalled, TimeoutExpired=subprocess.TimeoutExpired))
        self.assertEqual(set(), sf_process.carrying([1, 2], "/lease"))
        with self.assertRaises(sf_config.SlotError):
            sf_process.usage_table()
        self.assertIsNone(sf_platform.sysctl("kern.memorystatus_level"))

    def test_the_sampler_thread_leaves_signals_to_the_holder(self) -> None:
        masks: list[set[int]] = []
        looked = threading.Event()

        def table() -> dict[int, tuple[Any, ...]]:
            if threading.current_thread() is watch:
                masks.append(signal.pthread_sigmask(signal.SIG_BLOCK, []))
                looked.set()
            return {}
        self.patch("usage_table", table)
        self.patch("sample_machine", lambda directory: None)
        watch = self.watch()
        watch.start()
        self.assertTrue(looked.wait(10))
        watch.finish(None, 0.0)
        self.assertTrue(masks)
        self.assertLessEqual({signal.SIGTERM, signal.SIGHUP, signal.SIGINT}, masks[0])


class RunUsageTests(SlotCase):
    def setUp(self):
        super().setUp()
        self.env.update(SLOTFATHER_SAMPLE_MAX="0.1", SLOTFATHER_MACHINE_EVERY="0", PY=sys.executable, BURN=BURN)

    def history(self):
        return [json.loads(line) for line in (self.state / "history.jsonl").read_text().splitlines()]

    def test_a_run_records_the_cpu_of_its_whole_tree_and_of_a_child_that_outlives_its_parent(self):
        script = ('"$PY" -c "$BURN" 0.4 & "$PY" -c "$BURN" 0.4; wait; '
                  '( "$PY" -c "$BURN" 1.0 & sleep 0.3 ); sleep 2')
        result = self.tool("run", "--label", "tree", "--", "sh", "-c", script)
        self.assertEqual(0, result.returncode, result.stderr)
        item = self.history()[-1]
        self.assertEqual((2, "tree", "released", 0), (item["v"], item["label"], item["outcome"], item["status"]))
        self.assertGreaterEqual(item["cpu"] - item["cpu_detached"], 0.75, item)
        self.assertGreaterEqual(item["cpu_detached"], 0.3, f"the orphan's CPU was not seen: {item}")
        self.assertGreaterEqual(item["processes"], 4, item)
        self.assertGreater(item["rss_peak_mb"], 1, item)
        self.assertGreater(item["wall"], 2.0, item)
        self.assertEqual((12, 4), (item["cores"], item["capacity"]))
        self.assertRegex(self.tool("history", "1").stdout,
                         r"released: 1 slot\(s\) tree waited \d+s ran \d+s exit 0 used \d+\.\d cores, \d+ MB cmd=sh")

    def test_a_launchd_child_that_carries_the_lease_is_counted(self):
        probe = subprocess.Popen([sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(5)"],
                                 env={**os.environ, "SLOTFATHER_LEASE": "/probe"},
                                 stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: probe.stdout and probe.stdout.close())
        self.addCleanup(probe.wait)
        self.addCleanup(probe.kill)
        assert probe.stdout is not None
        self.assertEqual("ready", probe.stdout.readline().strip())
        if probe.pid not in sf_process.carrying([probe.pid], "/probe"):
            self.skipTest(f"ps shows no environment for {sys.executable} on this host")
        done = self.tmp / "detached-done"
        release = self.tmp / "detached-release"
        self.addCleanup(release.touch)
        script = (
            '( "$PY" -c "$DETACHED_BURN" 0.5 "$DONE" "$RELEASE_CHILD" </dev/null >/dev/null 2>&1 & ); '
            'n=400; while [ ! -e "$DONE" ] && [ "$n" -gt 0 ]; do sleep 0.05; n=$((n - 1)); done'
        )
        result = self.tool("run", "--label", "daemon", "--", "sh", "-c", script,
                           DETACHED_BURN=DETACHED_BURN, DONE=str(done), RELEASE_CHILD=str(release))
        release.touch()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(done.exists(), "the detached child did not finish its CPU burn before the holder exited")
        self.assertGreaterEqual(self.history()[-1]["cpu_detached"], 0.25, self.history()[-1])

    def test_holders_sample_the_machine_while_leases_run(self):
        self.assertEqual(0, self.tool("run", "-n", "2", "--", "sleep", "0.5").returncode)
        samples = [json.loads(line) for line in (self.state / "machine.jsonl").read_text().splitlines()]
        self.assertTrue(samples)
        first = samples[0]
        self.assertEqual((2, 12, 4, 2, 1), (first["v"], first["cores"], first["capacity"], first["held"],
                                           first["leases"]))
        self.assertEqual(3, len(first["load"]))
        self.assertNotIn("outside", first, "SLOTFATHER_LOADAVG=0: no ps of the machine for use outside leases")
        if sys.platform == "darwin":
            self.assertEqual(4, len(first["ticks"]))

    def test_a_nested_run_records_nothing_of_its_own(self):
        result = self.tool("run", "--label", "outer", "--", TOOL, "run", "--label", "inner", "--", "true")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(["outer"], [item["label"] for item in self.history()])


def four() -> int:
    return 4


def never(ran: float, p90: object) -> bool:
    return False


class BoundedHistoryTests(SlotCase):
    def test_history_rotates_into_one_older_file_and_reads_across_both(self):
        self.state.mkdir(parents=True)
        original = sf_config.HISTORY_MAX
        sf_config.HISTORY_MAX = 2000
        self.addCleanup(setattr, sf_config, "HISTORY_MAX", original)
        for n in range(40):
            sf_state.record(self.state, {"label": f"r{n}", "count": 1, "since": "2026-11-03T00:00:00Z"}, "left",
                            capacity=four, above=never)
        current, older = self.state / "history.jsonl", self.state / "history.jsonl.1"
        self.assertTrue(older.exists())
        self.assertLessEqual(current.stat().st_size if current.exists() else 0, 2000)
        self.assertLessEqual(older.stat().st_size, 2000 + 400, "a file rotates at the first line past the bound")
        labels = [item["label"] for item in sf_state.history_items(self.state)]
        self.assertEqual(labels, sorted(labels, key=lambda name: int(name[1:])), "oldest first across both files")
        self.assertEqual("r39", labels[-1])
        self.assertLess(len(labels), 40, "the rotated file replaced an older one")

    def test_a_torn_line_does_not_swallow_the_next_one(self):
        self.state.mkdir(parents=True)
        (self.state / "history.jsonl").write_text('{"label": "torn", "cou')
        sf_state.record(self.state, {"label": "whole", "count": 1}, "left", capacity=four, above=never)
        self.assertEqual(["whole"], [item["label"] for item in sf_state.history_items(self.state)])

    def test_a_short_listing_reads_only_the_current_file(self):
        self.state.mkdir(parents=True)
        older = self.state / "history.jsonl.1"
        older.write_text(json.dumps({"label": "older", "count": 1}) + "\n")
        for n in range(3):
            sf_state.record(self.state, {"label": f"r{n}", "count": 1}, "left", capacity=four, above=never)
        older.chmod(0)
        self.addCleanup(older.chmod, 0o644)
        self.assertEqual(2, len(sf_state.history_lines(2, self.state)))
        older.chmod(0o644)
        self.assertIn("1 slot(s) older ", sf_state.history_lines(4, self.state)[0])
        self.assertEqual(4, len(sf_state.history_lines(0, self.state)))

    def test_lines_written_before_version_2_still_read(self):
        self.state.mkdir(parents=True)
        old = {"label": "old", "cmd": "make", "cwd": "/w", "count": 1, "pid": 1, "queued": "2026-11-02T10:00:00Z",
               "granted": "2026-11-02T10:01:00Z", "ended": "2026-11-02T10:11:00Z", "outcome": "released",
               "waited": 60, "ran": 600, "status": 0}
        (self.state / "history.jsonl").write_text(json.dumps(old) + "\n")
        self.assertEqual(0, self.tool("run", "--label", "new", "--", "true").returncode)
        listing = self.tool("history").stdout.splitlines()
        self.assertRegex(listing[0], r"released: 1 slot\(s\) old waited 1m00s ran 10m00s exit 0 cmd=make$")
        self.assertRegex(listing[1], r"released: 1 slot\(s\) new .* used .* cmd=true$")


if __name__ == "__main__":
    unittest.main()
