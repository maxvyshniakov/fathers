"""tools/hygiene.py (fathers#235): what counts as a fixed sleep or a raw timing assertion, the markers that allow
one, which files are tests, and that this repository has none."""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import hygiene  # tools/hygiene.py, beside this file

ROOT = Path(__file__).resolve().parents[1]
HEADER = "import time\nimport unittest\nstarted = time.monotonic()\nelapsed = time.monotonic() - started\n"


class SleepTests(unittest.TestCase):
    def lines(self, source: str) -> list[int]:
        return [line for line, _ in hygiene.fixed_sleeps(source)]

    def test_fixed_sleeps_are_found(self) -> None:
        for source in ("time.sleep(0.2)", "time.sleep(1_000)", "sleep(.5)", "asyncio.sleep(2)", "time.sleep( 3 )",
                       "page.wait_for_timeout(100)", "page.wait_for_timeout(delay)", "x = 1; time.sleep(4)",
                       "s = 'it\\'s'; time.sleep(4)", "time.sleep(3)  # sleep-ok:"):
            with self.subTest(source=source):
                self.assertEqual([1], self.lines(source))

    def test_short_computed_and_quoted_sleeps_and_methods_are_not(self) -> None:
        for source in ("time.sleep(0.19)", "time.sleep(delay)", "clock.sleep(5)", "x.sleep(5)", "s = 'time.sleep(5)'",
                       "# time.sleep(9)", "time.sleep(0.1); time.sleep(5)", "asleep(5)"):
            with self.subTest(source=source):
                self.assertEqual([], self.lines(source))

    def test_a_reason_on_the_line_or_in_the_comments_above_allows_one(self) -> None:
        self.assertEqual([], self.lines("time.sleep(3)  # sleep-ok: the server needs a real pause"))
        self.assertEqual([], self.lines("# sleep-ok: a real pause\n# more about it\ntime.sleep(3)"))
        self.assertEqual([3], self.lines("# sleep-ok: a real pause\nx = 1\ntime.sleep(3)"))


class TimingTests(unittest.TestCase):
    def lines(self, body: str) -> list[int]:
        offset = HEADER.count("\n")
        return [line - offset for line, _ in hygiene.timing_asserts(HEADER + body)]

    def test_tight_bounds_on_a_measurement_are_found(self) -> None:
        for body in ("self.assertLess(elapsed, 0.2)", "self.assertLessEqual(elapsed, 29.9)", "assert elapsed < 5",
                     "assert 5 > elapsed", "assert time.monotonic() - started <= 2", "self.assertGreater(1.5, elapsed)",
                     "self.assertTrue(elapsed < 2)", "assert elapsed < bound",
                     "self.assertLess(\n    elapsed,\n    0.5,\n)"):
            with self.subTest(body=body):
                self.assertEqual([1], self.lines(body))

    def test_loose_bounds_budgets_and_unmeasured_values_are_not(self) -> None:
        for body in ("self.assertLess(elapsed, 30)", "assert elapsed < 60", "assert count < 3",
                     "assert perf_budget('x', elapsed, 1)", "self.assertGreater(elapsed, 0.1)",
                     "assert elapsed > 0", "s = 'assert elapsed < 1'"):
            with self.subTest(body=body):
                self.assertEqual([], self.lines(body))

    def test_deadlines_carry_their_bound(self) -> None:
        self.assertEqual([2], self.lines("deadline = time.monotonic() + 5\nassert time.monotonic() < deadline"))
        self.assertEqual([], self.lines("late = time.monotonic() + 60\nassert time.monotonic() < late"))

    def test_what_is_computed_from_a_measurement_measures_too(self) -> None:
        body = ("samples = []\nsamples.append(time.perf_counter() - started)\nmedian = sorted(samples)[0]\n"
                "count = len(samples)\nassert median < 1\nassert count < 3")
        self.assertEqual([5], self.lines(body))

    def test_a_load_tolerant_reason_of_three_words_allows_one(self) -> None:
        self.assertEqual([], self.lines("# load-tolerant: a slow runner gets this\nself.assertLess(elapsed, 0.2)"))
        self.assertEqual([2], self.lines("# load-tolerant:\nself.assertLess(elapsed, 0.2)"))
        self.assertEqual([], self.lines("self.assertLess(elapsed, 0.2)  # load-tolerant: CI is slow here"))

    def test_a_file_without_a_clock_has_no_timing_assertion(self) -> None:
        self.assertEqual([], hygiene.timing_asserts("elapsed = end - start\nassert elapsed < 1\n"))

    def test_the_perf_helper_counts_where_it_is_called_not_defined(self) -> None:
        self.assertTrue(hygiene.calls_perf_helper("perf_budget('x', 1)\n"))
        self.assertFalse(hygiene.calls_perf_helper("def perf_budget(name, seconds):\n    pass\n"))
        self.assertFalse(hygiene.calls_perf_helper("# perf_budget('x', 1)\n"))


class FileTests(unittest.TestCase):
    def test_test_files_are_matched_like_repo_kits_globs(self) -> None:
        is_test, excluded = hygiene.matcher(hygiene.TESTS), hygiene.matcher(hygiene.EXCLUDE)
        for path in ("tests/test_a.py", "tests/deep/helper.py", "test/x.py", "a/b/test_c.py", "a/c_test.py",
                     "test-root.py", "tools/sub/test-tool.py", "tools/test_checks.py"):
            with self.subTest(path=path):
                self.assertTrue(is_test(path))
        for path in ("a/tests/support.py", "a/test-x.py", "test_a.txt", "a/tests/helper.py"):
            with self.subTest(path=path):
                self.assertFalse(is_test(path))
        self.assertTrue(excluded("a/tests/fixtures/test_b.py"))
        self.assertTrue(hygiene.matcher(hygiene.PERF_SUITE)("a/tests/perf/test_x.py"))
        self.assertTrue(hygiene.matcher(hygiene.PERF_SUITE)("a/tests/test_perf_x.py"))

    def test_a_scan_reports_findings_and_unrun_budgets(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / "pkg/tests/perf").mkdir(parents=True)
            (root / "pkg/tests/test_a.py").write_text("import time\ntime.sleep(1)\n")
            (root / "pkg/tests/test_budget.py").write_text("perf_budget('x', 1)\n")
            (root / "pkg/tests/perf/test_ok.py").write_text("perf_budget('x', 1)\n")
            (root / "pkg/tests/fixtures").mkdir()
            (root / "pkg/tests/fixtures/test_skip.py").write_text("import time\ntime.sleep(1)\n")
            findings, unrun, scanned = hygiene.scan(root)
            self.assertEqual(3, scanned)
            self.assertEqual([("fixed-sleep", "pkg/tests/test_a.py", 2)], [(f.rule, f.file, f.line) for f in findings])
            self.assertEqual(["pkg/tests/test_budget.py"], unrun)
            result = subprocess.run([sys.executable, str(ROOT / "tools/hygiene.py")], cwd=root, capture_output=True,
                                    text=True)
            self.assertEqual(1, result.returncode)
            self.assertIn("fixed-sleep: pkg/tests/test_a.py:2: time.sleep(1)", result.stderr)
            self.assertIn("pkg/tests/test_budget.py states a perf budget", result.stderr)

    def test_this_repository_has_no_finding(self) -> None:
        findings, unrun, scanned = hygiene.scan(ROOT)
        self.assertEqual([], [f"{f.file}:{f.line}: {f.text}" for f in findings])
        self.assertEqual([], unrun)
        self.assertGreater(scanned, 150)


if __name__ == "__main__":
    unittest.main()
