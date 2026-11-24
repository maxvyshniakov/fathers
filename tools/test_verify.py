"""tools/verify.py: the scheduler, the lease, the memory budget, the runner on real processes, the verified-tree cache
and Lanefather's check contract, read back by Lanefather's own report reader (fathers#235)."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from typing import ClassVar
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import verify  # tools/verify.py, beside this file

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lanefather"))

from lanefather import verify as lanefather_verify  # noqa: E402  the reader of the report lane() writes

PY = sys.executable


def step(name: str, code: str = "", memory: float = 0.0, timeout: float = 1, cwd: Path | None = None) -> verify.Step:
    return verify.Step(name, [PY, "-c", code or "pass"], memory, timeout, cwd)


def until(condition, limit: float = 20.0) -> bool:
    """Polls `condition` until it holds or `limit` seconds pass."""
    end = time.monotonic() + limit
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.05)
    return bool(condition())


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie the runner has not reaped still answers kill 0; ps tells it apart.
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(state) and not state.startswith("Z")


def repository(test: unittest.TestCase) -> Path:
    root = Path(tempfile.mkdtemp())
    test.addCleanup(subprocess.run, ["rm", "-rf", str(root)], check=False)
    run_git(root, "init", "-q", "-b", "main")
    (root / "README.md").write_text("fixture\n")
    run_git(root, "add", ".")
    run_git(root, "commit", "-qm", "base")
    return root


def run_git(root: Path, *arguments: str) -> str:
    return subprocess.run(["git", "-C", str(root), "-c", "user.name=Fixture", "-c",
                           "user.email=fixture@example.invalid", "-c", "core.hooksPath=/dev/null", *arguments],
                          check=True, capture_output=True, text=True).stdout.strip()


class SchedulerTests(unittest.TestCase):
    def test_steps_start_in_order_as_their_memory_fits(self) -> None:
        steps = [step("a", memory=3), step("b", memory=2), step("c", memory=1), step("d", memory=5)]
        self.assertEqual(["a", "b"], [s.name for s in verify.startable(steps, [], {}, 5.5)])
        self.assertEqual(["c"], [s.name for s in verify.startable(steps, [], {"a": 3, "b": 2}, 6)])
        self.assertEqual([], [s.name for s in verify.startable(steps, ["c"], {"a": 3, "b": 2}, 6)])

    def test_a_step_larger_than_the_budget_runs_alone(self) -> None:
        steps = [step("big", memory=9), step("small", memory=1)]
        self.assertEqual(["big"], [s.name for s in verify.startable(steps, [], {}, 4)])
        self.assertEqual(["small"], [s.name for s in verify.startable(steps, ["big"], {}, 4)])

    def test_an_unbounded_budget_starts_every_step(self) -> None:
        steps = [step(name, memory=0.7) for name in "abcdefghijkl"]
        self.assertEqual(12, len(verify.startable(steps, [], {}, float("inf"))))


class LeaseTests(unittest.TestCase):
    def argv(self, slotfather: bool = True, purpose: str = "verify", **environment: str) -> list[str]:
        return verify.leased_argv(2, ["cmd", "x"], environment=environment, purpose=purpose, project="fathers",
                                  slotfather=slotfather)

    def test_a_held_lease_or_no_slotfather_runs_the_command_itself(self) -> None:
        self.assertEqual(["cmd", "x"], self.argv(SLOTFATHER_LEASE="abc"))
        self.assertEqual(["cmd", "x"], self.argv(slotfather=False))

    def test_the_label_names_the_project_and_the_purpose(self) -> None:
        self.assertEqual(["slotfather", "run", "-n", "2", "--label", "fathers verify", "--", "cmd", "x"], self.argv())

    def test_a_lane_label_gets_the_purpose_unless_it_has_one(self) -> None:
        self.assertIn("lane-7 check", self.argv(purpose="check", FATHERS_SLOT_LABEL="lane-7"))
        self.assertIn("lane-7 smoke run", self.argv(purpose="check", FATHERS_SLOT_LABEL="lane-7 smoke run"))

    def test_priority_ticket_meta_yield_and_wait_pass_through(self) -> None:
        argv = self.argv(FATHERS_SLOT_PRIORITY="batch", FATHERS_SLOT_TICKET="235", FATHERS_SLOT_YIELDABLE="1",
                         FATHERS_SLOT_META=json.dumps({"phase": "check", "lane": "l1"}), FATHERS_SLOT_WAIT="60")
        self.assertEqual(["slotfather", "run", "-n", "2", "--label", "fathers verify", "--priority", "batch",
                          "--ticket", "235", "--meta", "phase=check", "--meta", "lane=l1", "--yieldable",
                          "--wait", "60", "--", "cmd", "x"], argv)

    def test_empty_values_are_unset(self) -> None:
        self.assertEqual(["slotfather", "run", "-n", "2", "--label", "fathers verify", "--", "cmd", "x"],
                         self.argv(FATHERS_SLOT_PRIORITY="", FATHERS_SLOT_TICKET="", FATHERS_SLOT_YIELDABLE="",
                                   FATHERS_SLOT_WAIT="0", FATHERS_SLOT_META=""))

    def test_bad_priority_and_metadata_are_refused(self) -> None:
        for environment in ({"FATHERS_SLOT_PRIORITY": "urgent"}, {"FATHERS_SLOT_META": "{"},
                            {"FATHERS_SLOT_META": "[]"}, {"FATHERS_SLOT_META": '{"priority": "batch"}'},
                            {"FATHERS_SLOT_META": '{"n": 1}'}):
            with self.subTest(environment=environment), self.assertRaises(ValueError):
                self.argv(**environment)

    def test_the_lane_phase_comes_from_the_metadata(self) -> None:
        self.assertEqual("smoke", verify.lane_phase({"FATHERS_SLOT_META": '{"phase": "smoke"}'}))
        self.assertEqual("check", verify.lane_phase({"FATHERS_SLOT_META": "not json"}))
        self.assertEqual("check", verify.lane_phase({}))


class MemoryTests(unittest.TestCase):
    GIB = verify.GIB

    def test_the_override_wins(self) -> None:
        self.assertEqual(3.5, verify.memory_budget({"FATHERS_VERIFY_MEMORY_GIB": "3.5"}, linux=True, total=1))

    def test_off_linux_without_a_lease_all_memory(self) -> None:
        self.assertEqual(16, verify.memory_budget({}, linux=False, total=16 * self.GIB))

    def test_on_linux_available_memory_and_the_cgroup_limit(self) -> None:
        self.assertEqual(6, verify.memory_budget({}, linux=True, total=16 * self.GIB, available=6 * self.GIB))
        self.assertEqual(4, verify.memory_budget({}, linux=True, total=16 * self.GIB, available=6 * self.GIB,
                                                 constrained=4 * self.GIB))

    def test_a_lease_holds_its_slots_memory_where_slotfather_gives_one(self) -> None:
        leased = {"SLOTFATHER_LEASE": "x", "SLOTFATHER_COUNT": "2"}
        self.assertEqual(10, verify.memory_budget(leased, linux=False, total=64 * self.GIB,
                                                  capacity=lambda: {"gib_per_slot": 5, "total": 8}))
        self.assertEqual(float("inf"), verify.memory_budget(leased, linux=False, total=64 * self.GIB,
                                                            capacity=lambda: {"gib_per_slot": None}))


class RunnerTests(unittest.TestCase):
    def run_steps(self, steps: list[verify.Step], **options) -> tuple[verify.Outcome, str]:
        out = io.StringIO()
        outcome = verify.run(steps, out=out, reaper=False, **options)
        return outcome, out.getvalue()

    def test_passing_steps_stream_prefixed_lines_and_pass(self) -> None:
        outcome, text = self.run_steps([step("a", "print('one'); print('two')"), step("b", "print('three')")])
        self.assertEqual(0, outcome.exit)
        self.assertEqual({"a": "ok", "b": "ok"}, {r.name: r.status for r in outcome.results})
        self.assertIn("[a] one\n[a] two\n", text)
        self.assertIn("[b] three", text)
        self.assertRegex(text, r"(?m)^  ok   a \(\d+\.\ds\)$")
        self.assertRegex(text, r"(?m)^Passed in \d+\.\ds$")

    def test_the_first_failure_stops_the_others_and_shows_its_tail(self) -> None:
        slow = step("slow", "import time\nwhile True: time.sleep(0.05)", timeout=0)
        failing = step("bad", "import sys; print('\\n'.join(map(str, range(100)))); sys.exit(3)")
        later = step("later", memory=100)
        outcome, text = self.run_steps([slow, failing, later], memory=1)
        self.assertEqual(1, outcome.exit)
        self.assertEqual({"slow": "stopped", "bad": "fail"}, {r.name: r.status for r in outcome.results})
        self.assertIn("=== bad failed; its last 80 lines:\n20\n21\n", text)
        self.assertNotIn("\n19\n=== ", text)
        self.assertRegex(text, r"(?m)^  FAIL bad ")
        self.assertRegex(text, r"(?m)^  stop slow ")
        self.assertRegex(text, r"(?m)^  skip later$")
        self.assertRegex(text, r"(?m)^Failed in ")

    def test_keep_going_lets_the_others_finish(self) -> None:
        outcome, _ = self.run_steps([step("bad", "raise SystemExit(1)"), step("good", "print('done')")],
                                    keep_going=True, memory=0)
        self.assertEqual(1, outcome.exit)
        self.assertEqual({"bad": "fail", "good": "ok"}, {r.name: r.status for r in outcome.results})

    def test_a_command_that_cannot_start_fails(self) -> None:
        outcome, text = self.run_steps([verify.Step("missing", ["/nonexistent/command"])])
        self.assertEqual(1, outcome.exit)
        self.assertEqual("fail", outcome.results[0].status)
        self.assertIn("[missing] ", text)

    def test_a_step_past_its_timeout_is_killed_with_its_group(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            child, pid_file = Path(folder) / "child.py", Path(folder) / "pid"
            child.write_text("import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                             "while True: time.sleep(0.05)\n")
            parent = Path(folder) / "parent.py"
            parent.write_text("import signal, subprocess, sys, time\n"
                              "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                              f"p = subprocess.Popen([sys.executable, {str(child)!r}])\n"
                              f"open({str(pid_file)!r}, 'w').write(str(p.pid))\n"
                              "print('started', flush=True)\n"
                              "while True: time.sleep(0.05)\n")
            hang = verify.Step("hang", [PY, str(parent)], 0, 0.01)
            with mock.patch.object(verify, "KILL_GRACE", 0.3), mock.patch.object(verify, "CLOSE_GRACE", 0.3):
                outcome, text = self.run_steps([hang])
            self.assertEqual(1, outcome.exit)
            self.assertEqual(("fail", True), (outcome.results[0].status, outcome.results[0].timed_out))
            self.assertIn("[hang] started", text)
            self.assertIn("(timeout: 0.01 min); stopping its processes", text)
            self.assertRegex(text, r"(?m)^  FAIL hang \(\d+\.\ds, timed out\)$")
            self.assertIn("=== hang timed out after ", text)
            pid = int(pid_file.read_text())
            self.assertTrue(until(lambda: not alive(pid)), "the step's child outlived its SIGKILL")

    def runner(self, folder: Path, *, reaper: bool) -> tuple[subprocess.Popen[str], Path]:
        """A runner in its own process whose one step writes its pid to a file and waits until it is stopped."""
        pid_file = folder / "pid"
        waiting = folder / "wait.py"
        waiting.write_text("import os, pathlib, time\n"
                           f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
                           "while True: time.sleep(0.05)\n")
        script = folder / "runner.py"
        script.write_text(f"import sys\nsys.path.insert(0, {str(ROOT / 'tools')!r})\nimport verify\n"
                          f"steps = [verify.Step('wait', [sys.executable, {str(waiting)!r}], 0, 0)]\n"
                          f"sys.exit(verify.run(steps, reaper={reaper}).exit)\n")
        runner = subprocess.Popen([PY, str(script)], stdout=subprocess.PIPE, text=True)
        self.addCleanup(runner.kill)
        self.assertTrue(until(lambda: pid_file.exists() and pid_file.read_text() != ""), "the step never started")
        return runner, pid_file

    def test_a_signal_stops_every_step_and_exits_128_plus_its_number(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            runner, pid_file = self.runner(Path(folder), reaper=False)
            runner.send_signal(signal.SIGTERM)
            out, _ = runner.communicate(timeout=30)
            self.assertFalse(alive(int(pid_file.read_text())))
        self.assertEqual(128 + signal.SIGTERM, runner.returncode)
        self.assertIn("verify: SIGTERM, stopping the running steps", out)
        self.assertRegex(out, r"(?m)^  stop wait ")

    def test_the_reaper_stops_the_steps_of_a_runner_killed_outright(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            runner, pid_file = self.runner(Path(folder), reaper=True)
            runner.kill()
            runner.communicate(timeout=30)
            pid = int(pid_file.read_text())
            self.assertTrue(until(lambda: not alive(pid)), "the reaper left the step running")


class CacheTests(unittest.TestCase):
    def test_a_check_passed_on_a_tree_with_this_toolchain_is_known(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            cache = verify.Verified(Path(folder), ("py3.13", "darwin-arm64"))
            cache.record("tree1", ["tools", "test hygiene"])
            self.assertEqual({"tools", "test hygiene"}, cache.passed("tree1"))
            self.assertEqual(set(), cache.passed("tree2"))
            self.assertEqual(set(), verify.Verified(Path(folder), ("py3.11", "darwin-arm64")).passed("tree1"))
            self.assertEqual(set(), verify.Verified(Path(folder), ("py3.13", "linux-x86_64")).passed("tree1"))
            with (Path(folder) / verify.VERIFIED_RECORD).open("a") as record:
                record.write("garbage\nv2\ttree1\tpy3.13\tdarwin-arm64\tx\n")
            self.assertEqual({"tools", "test hygiene"}, cache.passed("tree1"))

    def test_a_commit_the_pre_push_gate_passed_is_known(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            cache = verify.Verified(Path(folder), ("py3.13", "darwin-arm64"))
            self.assertFalse(cache.commit_passed("abc"))
            cache.record_commit("abc")
            self.assertTrue(cache.commit_passed("abc"))
            self.assertFalse(cache.commit_passed("abd"))
            self.assertFalse(verify.Verified(Path(folder), ("py3.12", "darwin-arm64")).commit_passed("abc"))
            self.assertEqual(set(), cache.passed("abc"))

    def gate(self, root: Path, steps: list[verify.Step], **options) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch.object(verify, "Reaper", mock.MagicMock()):
            code = verify.gate(steps, root=root, out=out, env={**os.environ, "FATHERS_VERIFY_MEMORY_GIB": "8"},
                               **options)
        return code, out.getvalue()

    def test_a_clean_tree_skips_what_passed_on_it_even_on_another_commit(self) -> None:
        root = repository(self)
        counter = root.parent / f"{root.name}-count"
        self.addCleanup(lambda: counter.unlink(missing_ok=True))
        steps = [step("count", f"open({str(counter)!r}, 'a').write('x')", cwd=root), step("bad", "raise SystemExit(1)")]
        code, _ = self.gate(root, steps, keep_going=True)
        self.assertEqual(1, code)
        code, text = self.gate(root, steps, keep_going=True)
        self.assertEqual(1, code)
        self.assertIn("verify: count already passed on tree", text)
        self.assertEqual("x", counter.read_text())
        run_git(root, "commit", "-q", "--amend", "-m", "same tree, another commit")
        code, text = self.gate(root, steps[:1])
        self.assertEqual(0, code)
        self.assertIn("every selected check already passed", text)
        code, _ = self.gate(root, steps[:1], use_cache=False)
        self.assertEqual("xx", counter.read_text())

    def test_a_dirty_checkout_neither_reads_nor_records(self) -> None:
        root = repository(self)
        (root / "README.md").write_text("changed\n")
        self.gate(root, [step("one")])
        common = Path(run_git(root, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        self.assertFalse((common / verify.VERIFIED_RECORD).exists())

    def test_a_run_that_changes_the_checkout_records_nothing(self) -> None:
        root = repository(self)
        code, text = self.gate(root, [step("edit", "open('README.md', 'a').write('more')", cwd=root)])
        self.assertEqual(0, code)
        self.assertIn("the checkout changed during the run", text)
        common = Path(run_git(root, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        self.assertFalse((common / verify.VERIFIED_RECORD).exists())


class LaneTests(unittest.TestCase):
    CATALOG: ClassVar = [("verify:a", step("a", "print('a ran')")), ("verify:b%20c", step("b c", "print('b ran')"))]

    def selection(self, *argv: str):
        return verify.lane_selection(verify.lane_arguments(list(argv)), self.CATALOG)

    def test_the_selection_by_ids_or_suite(self) -> None:
        self.assertEqual((["verify:b%20c"], None), ([i for i, _ in self.selection("--checks", "verify:b%20c")[0]],
                                                    None))
        scoped_checks, scoped_jobs = self.selection("--checks", "verify:a", "--base", "main")
        self.assertEqual((["verify:a"], None), ([i for i, _ in scoped_checks], scoped_jobs))
        self.assertEqual(2, len(self.selection("--suites", "verify")[0]))
        self.assertEqual(2, len(self.selection()[0]))
        self.assertEqual(3, self.selection("--jobs=3", "--keep-going")[1])

    def test_refusals(self) -> None:
        for argv, message in ((["--checks", "verify:nope"], "unknown check selection"),
                              (["--checks", ""], "unknown check selection"),
                              (["--suites", "browser"], "unknown suite selection"),
                              (["--checks", "verify:a", "--suites", "verify"], "choose --checks or --suites"),
                              (["--base", "main"], "plan flags require --plan"),
                              (["--changed"], "plan flags require --plan"),
                              (["--json"], "plan flags require --plan"),
                              (["--plan"], "a plan takes --changed --base REF --plan --json"),
                              (["--jobs", "0"], "--jobs must be a positive integer"),
                              (["--jobs", "two"], "--jobs must be a positive integer")):
            with self.subTest(argv=argv), self.assertRaisesRegex(verify.LaneError, message):
                self.selection(*argv)
        for argv in (["--jobs"], ["--unknown"], ["positional"], ["--keep-going=yes"]):
            with self.subTest(argv=argv), self.assertRaises(verify.LaneError):
                verify.lane_arguments(argv)

    def lane(self, root: Path, *argv: str, catalog=None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        env = {**os.environ, "SLOTFATHER_LEASE": "test", "FATHERS_VERIFY_MEMORY_GIB": "8"}
        with mock.patch.object(verify, "Reaper", mock.MagicMock()), mock.patch("sys.stderr", err):
            code = verify.lane(list(argv), catalog or self.CATALOG, root=root, command=["lane-check"], env=env,
                               out=out)
        return code, out.getvalue(), err.getvalue()

    def report(self, text: str) -> dict:
        reports, report = lanefather_verify.report_of(text)
        self.assertEqual(1, len(reports))
        assert report is not None
        return report

    def test_a_passing_run_writes_the_report_lanefather_reads(self) -> None:
        root = repository(self)
        catalog = [(i, verify.Step(s.name, s.argv, s.memory, s.timeout, root)) for i, s in self.CATALOG]
        code, text, _ = self.lane(root, "--suites", "verify", "--jobs", "1", catalog=catalog)
        self.assertEqual(0, code)
        report = self.report(text)
        self.assertEqual({"v", "head", "tree", "exit", "environment", "interrupted", "checks"}, set(report))
        self.assertEqual((1, run_git(root, "rev-parse", "HEAD"), run_git(root, "rev-parse", "HEAD^{tree}"), 0,
                          None, None),
                         tuple(report[key] for key in ("v", "head", "tree", "exit", "environment", "interrupted")))
        self.assertEqual(["verify:a", "verify:b%20c"], [row["id"] for row in report["checks"]])
        for row in report["checks"]:
            self.assertEqual({"id", "suite", "status", "seconds", "timed_out", "log", "blocked_by"}, set(row))
            self.assertEqual(("verify", "passed", False, []), (row["suite"], row["status"], row["timed_out"],
                                                              row["blocked_by"]))
            self.assertFalse(lanefather_verify.is_executed_failure(row))
        self.assertEqual("a ran\n", Path(report["checks"][0]["log"]).read_text())
        self.assertIn("[verify:a] a ran", text)
        self.assertIn(f"{os.sep}fathers-verification{os.sep}run-", report["checks"][0]["log"])

    def test_a_failure_cancels_the_rest_and_lanefather_counts_both(self) -> None:
        root = repository(self)
        catalog = [("verify:bad", step("bad", "raise SystemExit(2)")),
                   ("verify:slow", step("slow", "import time\nwhile True: time.sleep(0.05)", timeout=0)),
                   ("verify:big", step("big", memory=100))]
        code, text, _ = self.lane(root, catalog=catalog)
        self.assertEqual(1, code)
        rows = {row["id"]: row for row in self.report(text)["checks"]}
        self.assertEqual({"verify:bad": "failed", "verify:slow": "cancelled", "verify:big": "cancelled"},
                         {key: row["status"] for key, row in rows.items()})
        self.assertTrue(all(lanefather_verify.is_executed_failure(row) for row in rows.values()))

    def test_a_dirty_checkout_is_refused(self) -> None:
        root = repository(self)
        (root / "new.txt").write_text("untracked\n")
        code, text, err = self.lane(root)
        self.assertEqual(1, code)
        self.assertIn("lane-check: verification requires a clean committed checkout", err)
        self.assertNotIn("Verification report:", text)

    def test_a_run_that_changes_its_inputs_is_void(self) -> None:
        root = repository(self)
        catalog = [("verify:edit", step("edit", "open('README.md', 'a').write('x')", cwd=root))]
        code, text, _ = self.lane(root, catalog=catalog)
        self.assertEqual(1, code)
        report = self.report(text)
        self.assertEqual("inputs-changed", report["environment"]["class"])
        self.assertEqual(["void"], [row["status"] for row in report["checks"]])

    def test_help_and_refusals_on_the_command_line(self) -> None:
        root = repository(self)
        code, text, _ = self.lane(root, "--help")
        self.assertEqual(0, code)
        self.assertIn("--checks IDS | --suites SUITES", text)
        code, _, err = self.lane(root, "--checks", "verify:nope")
        self.assertEqual((1, "lane-check: unknown check selection\n"), (code, err))


class EnvironmentTests(unittest.TestCase):
    def test_a_hooks_git_variables_are_dropped(self) -> None:
        cleaned = verify.clean_environment({"GIT_DIR": "x", "GIT_INDEX_FILE": "y", "GIT_QUARANTINE_PATH": "z",
                                            "GIT_AUTHOR_NAME": "kept", "PATH": "/bin"})
        self.assertEqual({"GIT_AUTHOR_NAME": "kept", "PATH": "/bin"}, cleaned)


if __name__ == "__main__":
    unittest.main()
