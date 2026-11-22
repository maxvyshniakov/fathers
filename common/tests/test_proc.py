"""The shared process-table reader, fathers_common.proc (fathers#171): lstart in both clocks, ps rows by column,
fail-closed tables, identities, trees, working directories and Linux start times.

Run from the repository root: python3 -m unittest discover -s common/tests
"""
from __future__ import annotations

import calendar
from collections.abc import Iterator
import contextlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import Any
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fathers_common import proc  # noqa: E402
from fathers_common.proc import _lstart  # noqa: E402  # pyright: ignore[reportPrivateUsage]

NEW_YORK = "EST5EDT,M3.2.0,M11.1.0"  # POSIX rules: no tz database needed; clocks go back 2026-11-01 02:00
BERLIN = "CET-1CEST,M3.5.0,M10.5.0/3"
KOLKATA = "IST-5:30"

# `ps -ax -ww -o pid=,ppid=,pgid=,uid=,pcpu=,tty=,lstart=,command=` as macOS and procps-ng print it.
MACOS = """\
    1     0     1     0   0.3 ??       Mon Nov  2 08:00:00 2026     /sbin/launchd
 4242     1  4242   501  87.5 ??       Thu Nov 12 10:00:00 2026     node server.js --port 3000
 4243  4242  4242   501   0,5 ttys001  Thu Nov 12 10:00:01 2026     -zsh
"""
PROCPS = """\
      1       0       1       0  0.0 ?        Sun Nov  1 07:59:59 2026 /sbin/init splash
   2001       1    2001    1000  1.5 pts/0    Tue Nov  3 09:05:01 2026 bash -l
   2002    2001    2001    1000  0.0 pts/0    Tue Nov  3 09:05:02 2026
"""
TABLE = proc.TABLE


@contextlib.contextmanager
def zone(name: str) -> Iterator[None]:
    """Run with TZ=`name`, restoring the caller's zone after."""
    before = os.environ.get("TZ")
    os.environ["TZ"] = name
    time.tzset()
    try:
        yield
    finally:
        if before is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = before
        time.tzset()


def answer(code: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["ps"], code, stdout, stderr)


def utc(text: str) -> int:
    return calendar.timegm(time.strptime(text, _lstart.FORMAT))


class LstartTests(unittest.TestCase):
    def test_timegm_is_calendars(self) -> None:
        for moment in ((1970, 1, 1, 0, 0, 0), (2000, 2, 29, 23, 59, 59), (2026, 11, 3, 9, 5, 1),
                       (2100, 3, 1, 0, 0, 0), (1969, 12, 31, 23, 59, 59), (2400, 12, 31, 12, 0, 0)):
            self.assertEqual(calendar.timegm((*moment, 0, 0, 0)), _lstart.timegm(moment), moment)

    def test_utc_text_names_one_epoch_both_ways(self) -> None:
        epoch = utc("Thu Nov 12 10:00:00 2026")
        self.assertEqual((epoch,), proc.epochs("Thu Nov 12 10:00:00 2026", "utc"))
        self.assertEqual(epoch, proc.parse_lstart("Thu  Nov 12  10:00:00 2026"))
        self.assertEqual("Thu Nov 12 10:00:00 2026", proc.render(epoch, "utc"))

    def test_days_one_to_nine_are_padded_as_ps_prints_them_and_unpadded_once_normalised(self) -> None:
        for day in range(1, 10):
            epoch = calendar.timegm((2026, 11, day, 9, 5, 1, 0, 0, 0))
            padded, plain = proc.render(epoch, "utc", padded=True), proc.render(epoch, "utc")
            self.assertEqual(f"Nov  {day} 09:05:01 2026", padded[4:], padded)
            self.assertEqual(f"Nov {day} 09:05:01 2026", plain[4:], plain)
            self.assertEqual(plain, proc.norm(padded))
            self.assertEqual(epoch, proc.parse_lstart(padded))
        self.assertEqual("Tue Nov 10 09:05:01 2026",
                         proc.render(calendar.timegm((2026, 11, 10, 9, 5, 1, 0, 0, 0)), "utc", padded=True))

    def test_what_is_not_lstart_names_nothing(self) -> None:
        for text in ("", "Thu Nov 12 10:00 2026", "Thu Foo 12 10:00:00 2026", "Thu Feb 30 10:00:00 2026",
                     "Thu Nov 12 24:00:00 2026", "пятница, 13 ноября 2026 г. 15:50:34", "Thu Nov 12 10:00:00 2026x"):
            self.assertEqual((), proc.epochs(text, "utc"), text)
            self.assertEqual((), proc.epochs(text, "local"), text)
            self.assertIsNone(proc.parse_lstart(text), text)

    def test_local_text_is_read_in_the_callers_zone(self) -> None:
        epoch = utc("Thu Nov 12 15:00:00 2026")
        for name, shown in ((NEW_YORK, "Thu Nov 12 10:00:00 2026"), (BERLIN, "Thu Nov 12 16:00:00 2026"),
                            (KOLKATA, "Thu Nov 12 20:30:00 2026"), ("UTC0", "Thu Nov 12 15:00:00 2026")):
            with self.subTest(zone=name), zone(name):
                self.assertEqual(shown, proc.render(epoch, "local"))
                self.assertEqual((epoch,), proc.epochs(shown, "local"))
                self.assertEqual("Thu Nov 12 15:00:00 2026", proc.render(epoch, "utc"))

    def test_the_hour_repeated_when_the_clocks_go_back_names_two_epochs(self) -> None:
        with zone(NEW_YORK):
            first = utc("Sun Nov 1 05:30:00 2026")  # 01:30 EDT
            second = first + 3600  # 01:30 EST
            self.assertEqual(proc.render(first, "local"), proc.render(second, "local"))
            self.assertEqual("Sun Nov 1 01:30:00 2026", proc.render(first, "local"))
            self.assertEqual((first, second), proc.epochs("Sun Nov 1 01:30:00 2026", "local"))
            self.assertEqual(first, proc.parse_lstart("Sun Nov 1 01:30:00 2026", "local"))
            self.assertTrue(proc.names("Sun Nov 1 01:30:00 2026", second, "local"))
            self.assertFalse(proc.names("Sun Nov 1 01:30:00 2026", second + 2, "local", slack=1))
            # Either side of the repeated hour names one epoch again.
            self.assertEqual((first - 3600,), proc.epochs("Sun Nov 1 00:30:00 2026", "local"))
            self.assertEqual((second + 3600,), proc.epochs("Sun Nov 1 02:30:00 2026", "local"))

    def test_the_hour_skipped_when_the_clocks_go_forward_names_none(self) -> None:
        with zone(NEW_YORK):
            self.assertEqual((), proc.epochs("Sun Mar 8 02:30:00 2026", "local"))
            self.assertIsNone(proc.parse_lstart("Sun Mar 8 02:30:00 2026", "local"))
            self.assertEqual(1, len(proc.epochs("Sun Mar 8 03:30:00 2026", "local")))

    def test_utc_reading_is_the_same_in_every_zone(self) -> None:
        text = "Sun Nov 1 01:30:00 2026"
        expected = utc(text)
        for name in (NEW_YORK, BERLIN, KOLKATA):
            with self.subTest(zone=name), zone(name):
                self.assertEqual((expected,), proc.epochs(text, "utc"))
                self.assertEqual(text, proc.render(expected, "utc"))


class ParseTests(unittest.TestCase):
    def test_macos_rows(self) -> None:
        found = proc.parse(MACOS, TABLE)
        self.assertEqual([1, 4242, 4243], sorted(found))
        server = found[4242]
        self.assertEqual((4242, 1, 4242, 501, "", "Thu Nov 12 10:00:00 2026", "node server.js --port 3000", 87.5),
                         tuple(server)[:8])
        self.assertEqual(utc("Thu Nov 12 10:00:00 2026"), server.epoch)
        self.assertEqual(float(utc("Thu Nov 12 10:00:00 2026")), server.started)
        self.assertEqual("node", server.program)
        self.assertEqual(("ttys001", 0.5, "-zsh"), (found[4243].tty, found[4243].cpu, found[4243].cmd))
        self.assertEqual("Mon Nov 2 08:00:00 2026", found[1].start)

    def test_procps_rows_with_an_empty_command(self) -> None:
        found = proc.parse(PROCPS, TABLE)
        self.assertEqual(("", "/sbin/init splash"), (found[1].tty, found[1].cmd))
        self.assertEqual(("pts/0", "Tue Nov 3 09:05:01 2026", "bash -l"),
                         (found[2001].tty, found[2001].start, found[2001].cmd))
        self.assertEqual("", found[2002].cmd)

    def test_lines_that_do_not_read_are_skipped(self) -> None:
        text = ("PID PPID PGID UID %CPU TT STARTED COMMAND\n"
                "  12 1 12 501 x ?? Thu Nov 12 10:00:00 2026 bad cpu\n"
                "  13 1 13 me 0.0 ?? Thu Nov 12 10:00:00 2026 bad uid\n"
                "  14 1 14 501 0.0 ?? Thu Nov\n"
                "\n"
                "  15 1 15 501 0.0 ?? Thu Nov 12 10:00:00 2026 good\n")
        self.assertEqual([15], list(proc.parse(text, TABLE)))

    def test_zombies_stat_rss_and_comm(self) -> None:
        text = (" 7 1 501 2048 Z  Thu Nov 12 10:00:00 2026 (node)\n"
                " 8 1 501 1024 S+ Thu Nov 12 10:00:01 2026 /usr/bin/python3\n")
        found = proc.parse(text, "pid=,ppid=,uid=,rss=,stat=,lstart=,comm=")
        self.assertTrue(found[7].zombie)
        self.assertFalse(found[8].zombie)
        self.assertEqual((1024, "S+", "/usr/bin/python3"), (found[8].rss, found[8].stat, found[8].cmd))

    def test_a_local_table_carries_the_epoch_its_text_names_there(self) -> None:
        with zone(NEW_YORK):
            found = proc.parse(" 9 1 501 Thu Nov 12 10:00:00 2026 worker 9\n", "pid=,ppid=,uid=,lstart=,command=",
                               "local")
            self.assertEqual(utc("Thu Nov 12 15:00:00 2026"), found[9].epoch)
            self.assertEqual("Thu Nov 12 10:00:00 2026", found[9].start)

    def test_columns_are_checked(self) -> None:
        self.assertEqual("pid=,ppid=,lstart=", proc.fields(["pid", "ppid", "lstart"]))
        self.assertEqual(("pid", "stat", "lstart"), proc.columns("pid=,stat=,lstart="))
        for spec in ("pid=,wchan=", "pid=,command=,ppid=", "ppid=,pid=", ""):
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                proc.columns(spec)


class FailClosedTests(unittest.TestCase):
    def test_a_ps_that_fails_or_lists_nothing_is_no_table(self) -> None:
        for name, result in (("exit", answer(1, MACOS, "ps: broken")), ("empty", answer(0, "")),
                             ("header only", answer(0, "PID PPID\n"))):
            with self.subTest(name), self.assertRaises(proc.ProcTableError):
                proc.table_rows(result)

    def test_a_table_without_the_caller_is_no_table_when_the_caller_is_required(self) -> None:
        with self.assertRaises(proc.ProcTableError) as raised:
            proc.table_rows(answer(0, MACOS), caller=True)
        self.assertEqual("ps listed 3 processes and not this one", raised.exception.detail)
        mine = f"{os.getpid()} 1 1 501 0.0 ?? Thu Nov 12 10:00:00 2026 me\n"
        self.assertEqual(4, len(proc.table_rows(answer(0, MACOS + mine), caller=True)))
        self.assertEqual(3, len(proc.table_rows(answer(0, MACOS))))

    def test_a_command_that_cannot_run_or_answer_raises(self) -> None:
        with self.assertRaises(proc.ProcTableError):
            proc.run(["/nonexistent/ps-for-fathers-tests"], timeout=5)

        def stalled(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            raise subprocess.TimeoutExpired(args[0], float(kwargs["timeout"]))
        with self.assertRaises(proc.ProcTableError) as raised:
            proc.run(["ps"], timeout=3, runner=stalled)
        self.assertEqual("ps did not answer in 3 s", str(raised.exception))

        def failed(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            return answer(2)
        with self.assertRaises(proc.ProcTableError):
            proc.run(["ps"], timeout=3, ok=(0,), runner=failed)

    def test_read_drops_zombies_on_request_and_never_returns_an_empty_table(self) -> None:
        text = (f"{os.getpid()} 1 501 S Thu Nov 12 10:00:00 2026 me\n"
                " 7 1 501 Z Thu Nov 12 10:00:00 2026 (gone)\n")
        spec = "pid=,ppid=,uid=,stat=,lstart=,command="
        with mock.patch("subprocess.run", return_value=answer(0, text)) as ran:
            table = proc.read(spec, timeout=5, zombies=False, caller=True)
        self.assertEqual([os.getpid()], list(table.procs))
        self.assertEqual("UTC", ran.call_args.kwargs["env"]["TZ"])
        self.assertEqual("C", ran.call_args.kwargs["env"]["LC_ALL"])
        only_zombie = " 7 1 501 Z Thu Nov 12 10:00:00 2026 (gone)\n"
        with mock.patch("subprocess.run", return_value=answer(0, only_zombie)), \
                self.assertRaises(proc.ProcTableError):
            proc.read(spec, timeout=5, zombies=False)

    def test_a_local_read_keeps_the_callers_zone(self) -> None:
        with mock.patch.dict(os.environ, {"TZ": BERLIN}), \
                mock.patch("subprocess.run", return_value=answer(0, MACOS)) as ran:
            proc.read(timeout=5, clock="local")
        self.assertEqual(BERLIN, ran.call_args.kwargs["env"]["TZ"])

    def test_identities_leave_out_zombies_and_gone_pids_and_strict_refuses_an_error(self) -> None:
        text = " 41 S Thu Nov 12 10:00:00 2026 worker 1\n 42 Z Thu Nov 12 10:00:00 2026 (worker)\n"
        with mock.patch("subprocess.run", return_value=answer(1, text)):
            self.assertEqual([41], list(proc.identities([41, 42, 43], timeout=5)))
        with mock.patch("subprocess.run", return_value=answer(1, text, "ps: permission denied")), \
                self.assertRaises(proc.ProcTableError):
            proc.identities([41, 42], timeout=5, strict=True)
        with mock.patch("subprocess.run", return_value=answer(2, "")), self.assertRaises(proc.ProcTableError):
            proc.identities([41], timeout=5)
        self.assertEqual({}, proc.identities([], timeout=5))


class IdentityTests(unittest.TestCase):
    START = "Thu Nov 12 10:00:00 2026"

    def here(self, pid: int = 50, cmd: str = "node server.js", epoch: int | None = None) -> proc.Proc:
        return proc.Proc(pid, 1, pid, 501, "", self.START, cmd, epoch=epoch)

    def test_the_same_process_within_the_btime_slack(self) -> None:
        seen = float(utc(self.START))
        for drift in (-1, 0, 1):  # /proc's btime rounds either way between two reads
            self.assertTrue(proc.same_process(seen + drift, "node server.js", self.here()), drift)
        self.assertFalse(proc.same_process(seen + 2, "node server.js", self.here()))
        self.assertFalse(proc.same_process(seen + 1, "node server.js", self.here(), slack=0))

    def test_a_pid_taken_again_in_the_same_second_is_told_by_its_command(self) -> None:
        seen = float(utc(self.START))
        self.assertFalse(proc.same_process(seen, "node server.js", self.here(cmd="python3 other.py")))
        self.assertTrue(proc.same_process(seen, "node server.js", self.here(cmd="/usr/bin/node renamed")))
        self.assertTrue(proc.same_process(seen, None, self.here(cmd="python3 other.py")))

    def test_an_unknown_start_is_never_the_same_process(self) -> None:
        unknown = proc.Proc(50, 1, 50, 501, "", "not a time", "node")
        self.assertFalse(proc.same_process(float(utc(self.START)), "node", unknown))
        self.assertFalse(proc.same_process(None, "node", self.here()))
        self.assertFalse(proc.same_process(0.0, "node", None))

    def test_an_epoch_wins_over_the_text(self) -> None:
        self.assertEqual(123.0, self.here(epoch=123).started)
        self.assertEqual(float(utc(self.START)), self.here().started)

    def test_local_text_that_names_no_epoch_is_never_read_as_utc(self) -> None:
        with zone(NEW_YORK):
            found = proc.parse(" 9 1 501 Sun Mar 8 02:30:00 2026 worker\n", "pid=,ppid=,uid=,lstart=,command=", "local")
            self.assertIsNone(found[9].epoch)
            self.assertIsNone(found[9].started)
            self.assertEqual("local", found[9].clock)

    def test_command_matches(self) -> None:
        self.assertTrue(proc.command_matches("a b", "a b"))
        self.assertTrue(proc.command_matches("/usr/bin/a x", "a y"))
        self.assertFalse(proc.command_matches("", "b"))
        self.assertFalse(proc.command_matches("b", "a"))


class TableTests(unittest.TestCase):
    def setUp(self) -> None:
        start = "Thu Nov 12 10:00:00 2026"
        rows = [(10, 1, 10), (11, 10, 10), (12, 11, 10), (13, 10, 13), (14, 13, 13), (20, 1, 20)]
        self.table = proc.Table({pid: proc.Proc(pid, ppid, pgid, 501, "", start, f"cmd{pid}")
                                 for pid, ppid, pgid in rows}, moment=float(utc(start)) + 60)

    def test_trees_groups_and_ages(self) -> None:
        self.assertEqual([11, 10], self.table.ancestors(12))
        self.assertEqual([11, 12, 13, 14], sorted(p.pid for p in self.table.descendants(10)))
        self.assertEqual([11, 12], sorted(p.pid for p in self.table.descendants(10, prune=lambda p: p.pid == 13)))
        self.assertEqual([10, 11, 12], sorted(p.pid for p in self.table.group(10)))
        here = self.table.get(12)
        assert here is not None
        self.assertEqual(60.0, self.table.age(here))
        self.assertIsNone(self.table.get("12"))

    def test_alive_by_text_or_by_identity(self) -> None:
        self.assertTrue(self.table.alive(12, "Thu Nov  12 10:00:00 2026"))
        self.assertFalse(self.table.alive(12, ""))
        self.assertFalse(self.table.alive(99, "Thu Nov 12 10:00:00 2026"))
        seen = float(utc("Thu Nov 12 10:00:00 2026"))
        self.assertTrue(self.table.alive(12, started=seen + 1, cmd="cmd12"))
        self.assertFalse(self.table.alive(12, started=seen, cmd="other"))

    def test_a_cycle_ends_the_ancestry(self) -> None:
        start = "Thu Nov 12 10:00:00 2026"
        looped = proc.Table({5: proc.Proc(5, 6, 5, 0, "", start, "a"), 6: proc.Proc(6, 5, 5, 0, "", start, "b")})
        self.assertEqual([6, 5], looped.ancestors(5))


class WorkingDirTests(unittest.TestCase):
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name) / "proc"
        self.work = Path(scratch.name) / "work"
        self.work.mkdir()
        self.uid = 501

    def pid(self, pid: int, cwd: str | None, ns: str = "mnt:[1]") -> None:
        (self.root / str(pid) / "ns").mkdir(parents=True)
        (self.root / str(pid) / "ns" / "mnt").symlink_to(ns)
        if cwd is not None:
            (self.root / str(pid) / "cwd").symlink_to(cwd)

    def test_proc_reads_paths_removed_dirs_and_other_namespaces(self) -> None:
        (self.root / "self" / "ns").mkdir(parents=True)
        (self.root / "self" / "ns" / "mnt").symlink_to("mnt:[1]")
        self.pid(100, str(self.work))
        self.pid(101, "/gone/dir (deleted)")
        self.pid(102, "/elsewhere", ns="mnt:[2]")
        self.pid(103, None)  # its cwd cannot be read
        self.pid(104, str(self.work))  # another user's
        uids = {100: self.uid, 101: self.uid, 102: self.uid, 103: self.uid, 104: 0}
        found = proc.working_dirs(uids, self.uid, use_proc=True, proc_root=str(self.root))
        self.assertEqual(proc.Cwd(str(self.work), False), found.paths[100])
        self.assertEqual(proc.Cwd("/gone/dir", True), found.paths[101])
        self.assertEqual({102, 103}, found.foreign)
        self.assertEqual([100, 101], sorted(found.paths))
        self.assertTrue(found.complete)
        self.assertEqual(["2 process(es) in another mount namespace (a container) left out"], found.notes)
        strict = proc.working_dirs(uids, self.uid, use_proc=True, proc_root=str(self.root), strict=True)
        self.assertEqual({102}, strict.foreign)
        self.assertEqual(proc.Cwd(os.path.realpath(self.work), None), strict.paths[100])

    def test_proc_without_its_own_namespace_is_unreadable_when_strict(self) -> None:
        self.root.mkdir()
        with self.assertRaises(proc.ProcTableError):
            proc.working_dirs({1: self.uid}, self.uid, use_proc=True, proc_root=str(self.root), strict=True)
        self.assertEqual({}, proc.working_dirs({1: self.uid}, self.uid, use_proc=True,
                                               proc_root=str(self.root)).paths)

    def test_lsof_listing(self) -> None:
        listing = f"p300\nn{self.work}\np301\nn/tmp\np302\n"
        calls: list[list[str]] = []

        def lsof(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, listing, "")
        lenient = proc.working_dirs({}, self.uid, use_proc=False, runner=lsof)
        self.assertEqual({300: proc.Cwd(str(self.work), None), 301: proc.Cwd("/tmp", None)}, lenient.paths)
        self.assertTrue(lenient.complete)
        self.assertEqual(["lsof", "-nP", "-a", "-d", "cwd", "-u", "501", "-w", "-Fpn"], calls[0])
        strict = proc.working_dirs({300: self.uid, 301: 0}, self.uid, use_proc=False, strict=True, runner=lsof)
        self.assertEqual({300: proc.Cwd(os.path.realpath(self.work), None)}, strict.paths)

    def test_lsof_that_cannot_answer_is_unknown_never_nothing(self) -> None:
        def missing(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            raise FileNotFoundError(2, "No such file or directory", "lsof")

        def broken(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, 2, "", "lsof: bad")
        lenient = proc.working_dirs({}, self.uid, use_proc=False, runner=missing)
        self.assertFalse(lenient.complete)
        self.assertEqual(1, len(lenient.notes))
        self.assertTrue(lenient.notes[0].startswith("working directories unknown (lsof: "), lenient.notes)
        self.assertFalse(proc.working_dirs({}, self.uid, use_proc=False, runner=broken).complete)
        for runner in (missing, broken):
            with self.subTest(runner=runner.__name__), self.assertRaises(proc.ProcTableError):
                proc.working_dirs({}, self.uid, use_proc=False, strict=True, runner=runner)

    def test_removed_by_inode_through_the_callers_check(self) -> None:
        def lsof(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, 0, "p400\ni77\nn/old\n", "")
        seen: list[tuple[str, int | None]] = []

        def check(path: str, inode: int | None) -> bool | None:
            seen.append((path, inode))
            return True
        found = proc.working_dirs({}, self.uid, use_proc=False, check_removed=check, runner=lsof)
        self.assertEqual(proc.Cwd("/old", True), found.paths[400])
        self.assertEqual([("/old", 77)], seen)


class ProcStartTests(unittest.TestCase):
    def proc_root(self, btime: int, ticks: int) -> str:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        root = Path(scratch.name)
        (root / "42").mkdir()
        after = ["S", "1", "42", "42", "0", "-1", "4194560"] + ["0"] * 12 + [str(ticks), "0"]
        (root / "42" / "stat").write_text(f"42 (a (tricky) name) {' '.join(after)}\n")
        (root / "stat").write_text(f"cpu  1 2 3\nbtime {btime}\nprocesses 9\n")
        return str(root)

    def test_start_from_btime_and_ticks(self) -> None:
        tick = os.sysconf("SC_CLK_TCK")
        self.assertEqual(1_790_000_000 + 25, proc.proc_start_epoch(42, self.proc_root(1_790_000_000, 25 * tick)))

    def test_btime_a_second_apart_is_still_the_same_process(self) -> None:
        tick = os.sysconf("SC_CLK_TCK")
        first = proc.proc_start_epoch(42, self.proc_root(1_790_000_000, 25 * tick))
        second = proc.proc_start_epoch(42, self.proc_root(1_790_000_001, 25 * tick))
        assert first is not None and second is not None
        now = proc.Proc(42, 1, 42, 0, "", "", "worker", epoch=second)
        self.assertTrue(proc.same_process(float(first), "worker", now))
        self.assertFalse(proc.same_process(float(first), "worker", now, slack=0))

    def test_an_unreadable_proc_is_unknown(self) -> None:
        self.assertIsNone(proc.proc_start_epoch(42, "/nonexistent/proc-for-fathers-tests"))
        root = self.proc_root(1_790_000_000, 25)
        (Path(root) / "stat").write_text("cpu 1 2 3\n")
        self.assertIsNone(proc.proc_start_epoch(42, root))


class RealTableTests(unittest.TestCase):
    """One read of this machine's table: the caller is in it, in both clocks, with the same epoch."""

    def test_this_process_in_both_clocks(self) -> None:
        here = proc.read(timeout=10, caller=True).get(os.getpid())
        local = proc.read(timeout=10, clock="local").get(os.getpid())
        assert here is not None and local is not None
        self.assertEqual(here.epoch, local.epoch)
        assert here.epoch is not None
        self.assertEqual(proc.render(here.epoch, "utc"), here.start)
        self.assertEqual(proc.render(here.epoch, "local"), local.start)
        self.assertEqual([os.getpid()], list(proc.identities([os.getpid()], timeout=10)))


if __name__ == "__main__":
    unittest.main()
