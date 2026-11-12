"""The dashboard is Godfather's (#28): `slotfather dashboard ARGS` and a bare `slotfather` in a terminal become
`godfather ARGS`, and say where to get it when it is missing; admission and status load no terminal code."""
from __future__ import annotations

import sys as _path_sys
from pathlib import Path as _Path
_path_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "src"))

import contextlib
import json
import os
import pty
import select
import shlex
import subprocess
import sys
import time
import unittest

from test_slotfather import ROOT, TOOL, SlotCase

MISSING = ("slotfather: the dashboard is part of godfather now; install it from "
           "https://github.com/maxvyshniakov/fathers/tree/main/godfather (python3 install.py godfather)\n")
TERMINAL = ("termios", "tty", "pty", "curses", "readline", "slotfather.dashboard", "slotfather.dashboard_settings",
            "slotfather.theme", "slotfather.keys")


class GodfatherCase(SlotCase):
    def setUp(self) -> None:
        super().setUp()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.calls = self.tmp / "calls.jsonl"

    def fake_godfather(self, code: int) -> None:
        """A `godfather` that records its argv (argv[0] by name) and exits with `code`: a /bin/sh script (no long or
        spaced interpreter path in its #! line) handing its arguments to a recorder in Python."""
        recorder = self.tmp / "record.py"
        recorder.write_text("import json, os, sys\n"
                            f"with open({str(self.calls)!r}, 'a') as out:\n"
                            "    out.write(json.dumps([os.path.basename(sys.argv[1]), *sys.argv[2:]]) + '\\n')\n"
                            f"sys.exit({code})\n")
        fake = self.bin / "godfather"
        fake.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(recorder))} "$0" "$@"\n')
        fake.chmod(0o755)

    def calls_made(self) -> list[list[str]]:
        if not self.calls.exists():
            return []
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def run_tool(self, *args: str) -> subprocess.CompletedProcess[str]:
        """slotfather with only the fake bin on PATH (no installed godfather can answer), stdin not a terminal."""
        return subprocess.run([sys.executable, TOOL, *args], env={**self.env, "PATH": str(self.bin)},
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20)

    def in_terminal(self, *args: str, stdout_to: str | None = None) -> tuple[int, str]:
        """slotfather in a pseudo-terminal (stdin and stdout a TTY, or stdout to the file `stdout_to`): its exit code
        and what it wrote to the terminal."""
        pid, fd = pty.fork()
        if pid == 0:  # the child: become slotfather
            try:
                if stdout_to is not None:
                    os.dup2(os.open(stdout_to, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 1)
                os.execve(sys.executable, [sys.executable, TOOL, *args], {**self.env, "PATH": str(self.bin)})
            finally:
                os._exit(127)
        output, deadline, status = b"", time.monotonic() + 20, None
        while status is None and time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.2)
            chunk = b""
            if ready:
                try:
                    chunk = os.read(fd, 4096)
                except OSError:  # Linux: EIO once the child has exited
                    chunk = b""
                output += chunk
            if not chunk:
                done, found = os.waitpid(pid, os.WNOHANG)
                status = found if done else None
        if status is None:
            os.kill(pid, 9)
            _, status = os.waitpid(pid, 0)
        with contextlib.suppress(OSError):  # the child is reaped: read to the end (b"", or EIO on Linux)
            while select.select([fd], [], [], 2)[0]:
                chunk = os.read(fd, 4096)
                if not chunk:
                    break
                output += chunk
        os.close(fd)
        return os.waitstatus_to_exitcode(status), output.decode(errors="replace").replace("\r\n", "\n")


class ForwardTests(GodfatherCase):
    def test_dashboard_runs_godfather_with_its_arguments_and_exit_code(self) -> None:
        self.fake_godfather(7)
        result = self.run_tool("dashboard", "--once", "--width", "156", "--height", "43", "--color")
        self.assertEqual(7, result.returncode, result.stderr)
        self.assertEqual([["godfather", "--once", "--width", "156", "--height", "43", "--color"]], self.calls_made())
        self.assertEqual("", result.stderr)

    def test_dashboard_without_arguments_and_a_zero_exit(self) -> None:
        self.fake_godfather(0)
        result = self.run_tool("dashboard")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual([["godfather"]], self.calls_made())

    def test_arguments_slotfather_does_not_know_pass_through_untouched(self) -> None:
        self.fake_godfather(3)
        result = self.run_tool("dashboard", "--", "-n", "all", "two words", "")
        self.assertEqual(3, result.returncode, result.stderr)
        self.assertEqual([["godfather", "--", "-n", "all", "two words", ""]], self.calls_made())

    def test_a_bare_slotfather_in_a_terminal_runs_godfather(self) -> None:
        self.fake_godfather(5)
        code, _ = self.in_terminal()
        self.assertEqual(5, code)
        self.assertEqual([["godfather"]], self.calls_made())

    def test_a_bare_slotfather_with_only_stdin_a_terminal_prints_usage_and_runs_nothing(self) -> None:
        self.fake_godfather(0)
        code, output = self.in_terminal(stdout_to=str(self.tmp / "piped"))
        self.assertEqual(2, code)
        self.assertIn("usage: slotfather", output)
        self.assertEqual([], self.calls_made())

    def test_a_bare_slotfather_without_a_terminal_prints_usage_and_runs_nothing(self) -> None:
        self.fake_godfather(0)
        result = self.run_tool()
        self.assertEqual(2, result.returncode)
        self.assertIn("usage: slotfather", result.stderr)
        self.assertEqual([], self.calls_made())


class MissingTests(GodfatherCase):
    def test_dashboard_without_godfather_says_where_to_get_it_and_exits_2(self) -> None:
        result = self.run_tool("dashboard", "--once")
        self.assertEqual(2, result.returncode)
        self.assertEqual(MISSING, result.stderr)
        self.assertEqual("", result.stdout)

    def test_a_bare_slotfather_in_a_terminal_without_godfather_says_so_too(self) -> None:
        code, output = self.in_terminal()
        self.assertEqual(2, code)
        self.assertEqual(MISSING, output)

    def test_a_godfather_that_is_not_executable_counts_as_missing(self) -> None:
        (self.bin / "godfather").write_text("#!/bin/sh\nexit 0\n")
        result = self.run_tool("dashboard")
        self.assertEqual(2, result.returncode)
        self.assertEqual(MISSING, result.stderr)


class ExecFailureTests(GodfatherCase):
    def test_a_godfather_that_cannot_be_run_is_said_and_exits_126(self) -> None:
        fake = self.bin / "godfather"
        fake.write_bytes(b"\x00\x01 not a program\n")  # executable, but no #! line and no binary format
        fake.chmod(0o755)
        result = self.run_tool("dashboard")
        self.assertEqual(126, result.returncode, result.stderr)
        self.assertTrue(result.stderr.startswith(f"slotfather: cannot run {fake}: "), result.stderr)
        self.assertNotIn("Traceback", result.stderr)


class ImportGraphTests(SlotCase):
    """Admission (`run`, `lane-admission`), status (`status`, `--json`, `--full`) and the other read commands load
    no terminal module: CI runners take leases without one, and the dashboard's code is Godfather's."""

    PROBE = """
import contextlib, io, json, sys
sys.path.insert(0, sys.argv[1])
from slotfather import cli
calls = [["status"], ["status", "--json"], ["status", "--json", "--full"], ["capabilities", "--json"],
         ["settings", "--json"], ["run", "-n", "1", "--", sys.executable, "-c", "pass"], ["procs", "--json"],
         ["stats", "--json"], ["history"], ["version", "--json"], ["slots"], ["order"], ["available"]]
for argv in calls:
    with contextlib.redirect_stdout(io.StringIO()):
        assert cli.main(argv) == 0, argv
sys.stdin = io.StringIO(json.dumps({"v": 1}))
with contextlib.redirect_stdout(io.StringIO()):
    cli.main(["lane-admission", "--json"])
print(json.dumps(sorted(sys.modules)))
"""

    def test_admission_and_status_import_no_terminal_code(self) -> None:
        result = subprocess.run([sys.executable, "-c", self.PROBE, str(ROOT / "src")], env=self.env,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(0, result.returncode, result.stderr)
        loaded = set(json.loads(result.stdout))
        self.assertIn("slotfather.board", loaded, "the probe reached status --json --full")
        self.assertIn("slotfather.leases", loaded, "the probe reached admission")
        self.assertIn("slotfather.lane_admission", loaded)
        self.assertEqual([], sorted(name for name in TERMINAL if name in loaded))

    def test_the_terminal_modules_are_gone_from_the_package(self) -> None:
        package = ROOT / "src" / "slotfather"
        for name in ("dashboard", "dashboard_settings", "theme", "keys"):
            self.assertFalse((package / f"{name}.py").exists(), name)
        self.assertFalse((ROOT / "slotfather-dashboard").exists())


if __name__ == "__main__":
    unittest.main()
