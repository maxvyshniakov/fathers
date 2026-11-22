"""Read-only process query through the public package and command interface."""
from __future__ import annotations

import importlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


class ProcsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="slotfather-procs-")
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name) / "state"
        self.environment = mock.patch.dict(os.environ, {
            "SLOTFATHER_DIR": str(self.directory), "SLOTFATHER_LEGACY_DIRS": "",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.procs = importlib.import_module("slotfather.procs")
        platform = mock.patch("sys.platform", "linux")
        platform.start()
        self.addCleanup(platform.stop)

    def ps(self, *rows: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 0, "\n".join(rows) + "\n", "")

    def test_pid_query_is_read_only_and_exposes_identity_without_cwd_scan(self):
        output = self.ps("51 1 501 ?? S Wed Nov  4 10:00:00 2026 /usr/local/bin/claude",
                         "52 51 501 ?? S Wed Nov  4 10:00:01 2026 python3 test.py")
        with mock.patch("subprocess.run", return_value=output) as run:
            found = self.procs.query(pid=52)
        self.assertEqual(2, found["v"])
        self.assertEqual({"pid": 52, "cwd": None, "start": "Wed Nov 4 10:00:01 2026"}, found["query"])
        self.assertEqual([52], [item["pid"] for item in found["processes"]])
        item = found["processes"][0]
        self.assertEqual([51], item["ancestors"])
        self.assertEqual([51], [session["pid"] for session in item["sessions"]])
        self.assertEqual([], item["leases"])
        self.assertFalse(self.directory.exists(), "queries must never create or reap queue state")
        self.assertEqual(1, run.call_count, "the fast PID query reads ps once and never runs lsof")

    def lease(self, name: str, *, pid: int = 51, start: str = "Wed Nov  4 10:00:00 2026", **extra: object) -> Path:
        queue = self.directory / "queue"
        queue.mkdir(parents=True, exist_ok=True)
        path = queue / f"{name}.json"
        path.write_text(json.dumps({"pid": pid, "start": start, "state": "held", "count": 2,
                                    "label": name, "priority": "batch", "meta": {"session": "session-a"}, **extra}))
        return path

    def test_live_leases_join_by_ancestry_and_reused_holders_are_excluded_without_reaping(self):
        valid = self.lease("active")
        stale = self.lease("stale", start="Tue Nov  3 10:00:00 2026")
        before = {path: path.read_bytes() for path in (valid, stale)}
        output = self.ps("51 1 501 ?? S Wed Nov  4 10:00:00 2026 /usr/local/bin/claude",
                         "52 51 501 ?? S Wed Nov  4 10:00:01 2026 python3 test.py")
        with mock.patch("subprocess.run", return_value=output):
            found = self.procs.query(pid=52)
        leases = found["processes"][0]["leases"]
        self.assertEqual(["active"], [item["id"] for item in leases])
        self.assertEqual((51, "batch", "tree", "session-a"),
                         (leases[0]["pid"], leases[0]["priority"], leases[0]["via"], leases[0]["meta"]["session"]))
        self.assertEqual(before, {path: path.read_bytes() for path in before})
        self.assertEqual({"queue"}, {path.name for path in self.directory.iterdir()})

    def test_cwd_filter_matches_directory_boundaries_and_omits_other_users_and_callers(self):
        uid = os.geteuid()
        output = self.ps(f"51 1 {uid} ?? S Wed Nov  4 10:00:00 2026 /bin/zsh",
                         f"52 51 {uid} ?? S Wed Nov  4 10:00:01 2026 python3 test.py",
                         f"53 51 {uid} ?? S Wed Nov  4 10:00:02 2026 node neighbor.js",
                         f"54 1 {uid + 1} ?? S Wed Nov  4 10:00:03 2026 node other.js",
                         f"9999 51 {uid} ?? S Wed Nov  4 10:00:04 2026 slotfather procs")
        cwds = subprocess.CompletedProcess([], 0,
            "p51\nn/work/lane\np52\nn/work/lane/tests\np53\nn/work/lane-other\np54\nn/work/lane\np9999\nn/work/lane\n", "")
        with mock.patch("sys.platform", "darwin"), mock.patch("os.getpid", return_value=9999), \
                mock.patch("subprocess.run", side_effect=[output, cwds]):
            found = self.procs.query(cwd="/work/lane")
        self.assertEqual([52], [item["pid"] for item in found["processes"]])
        self.assertEqual("/work/lane/tests", found["processes"][0]["cwd"])
        self.assertEqual("/work/lane", found["query"]["cwd"])

    def test_detached_process_matches_exact_lease_environment_without_exposing_environment(self):
        lease = self.lease("active")
        output = self.ps("51 1 501 ?? S Wed Nov  4 10:00:00 2026 python3 holder.py",
                         "53 1 501 ?? S Wed Nov  4 10:00:01 2026 node detached.js")
        environment = self.ps(f"53 node detached.js SECRET=never-print SLOTFATHER_LEASE={lease} NEXT=value")
        with mock.patch("sys.platform", "darwin"), mock.patch("subprocess.run", side_effect=[output, environment]):
            found = self.procs.query()
        self.assertEqual("env", found["processes"][-1]["leases"][0]["via"])
        self.assertNotIn("never-print", json.dumps(found))
        wrong = self.ps(f"53 node detached.js SLOTFATHER_LEASE={lease}-other")
        running = self.ps("53 S Wed Nov  4 10:00:01 2026")  # the re-check of an unknown (fathers#158)
        with mock.patch("sys.platform", "darwin"), mock.patch("subprocess.run", side_effect=[output, wrong, running]):
            found = self.procs.query()
        self.assertEqual([], found["processes"][-1]["leases"])
        self.assertEqual([53], found["lease_unknown"], "psE cannot prove that a negative environment was readable")

    def test_command_returns_versioned_json_and_rejects_ambiguous_options(self):
        output = self.ps("52 1 501 ?? S Wed Nov  4 10:00:01 2026 python3 test.py")
        stdout = io.StringIO()
        with mock.patch("subprocess.run", return_value=output), mock.patch("sys.stdout", stdout):
            self.assertEqual(0, self.procs.command(["--json", "--pid", "52"]))
        self.assertEqual(52, json.loads(stdout.getvalue())["query"]["pid"])
        for args in (["--pid", "52"], ["--json", "--pid", "0"], ["--json", "--pid", "52", "--pid", "53"],
                     ["--json", "--cwd"], ["--json", "--unknown"]):
            with self.subTest(args=args), self.assertRaises(self.procs.ProcsError):
                self.procs.command(args)

    def test_unreadable_process_or_cwd_tables_fail_closed(self):
        failed = subprocess.CompletedProcess([], 1, "", "permission denied")
        with mock.patch("subprocess.run", return_value=failed), self.assertRaises(self.procs.ProcsError):
            self.procs.query(pid=52)
        output = self.ps("52 1 501 ?? S Wed Nov  4 10:00:01 2026 python3 test.py")
        with mock.patch("sys.platform", "darwin"), mock.patch("subprocess.run", side_effect=[output, failed]), \
                self.assertRaises(self.procs.ProcsError):
            self.procs.query(cwd="/work/lane")
        with mock.patch("subprocess.run", return_value=self.ps()), self.assertRaises(self.procs.ProcsError):
            self.procs.query()

    def test_linux_cwd_uses_local_mount_namespace_and_reports_unknown_paths(self):
        uid = os.geteuid()
        proc = Path(self.tmp.name) / "proc"
        (proc / "self/ns").mkdir(parents=True)
        (proc / "self/ns/mnt").symlink_to("mnt:[111]")
        for number, namespace in ((52, "mnt:[111]"), (53, "mnt:[222]")):
            (proc / str(number) / "ns").mkdir(parents=True)
            (proc / str(number) / "ns/mnt").symlink_to(namespace)
            (proc / str(number) / "cwd").symlink_to("/work/lane/tests")
        output = self.ps(f"52 1 {uid} ?? S Wed Nov  4 10:00:01 2026 python3 test.py",
                         f"53 1 {uid} ?? S Wed Nov  4 10:00:02 2026 node container.js")
        running = self.ps("53 S Wed Nov  4 10:00:02 2026")  # the re-check of an unknown (fathers#158)
        with mock.patch("sys.platform", "linux"), mock.patch.dict(os.environ, {"SLOTFATHER_PROC": str(proc)}), \
                mock.patch("subprocess.run", side_effect=[output, running]):
            found = self.procs.query(cwd="/work/lane")
        self.assertEqual([52], [item["pid"] for item in found["processes"]])
        self.assertEqual([53], found["cwd_unknown"], "foreign paths must never be treated as local directories")

    def test_missing_and_zombie_pid_return_no_live_identity(self):
        output = self.ps("51 1 501 ?? S Wed Nov  4 10:00:00 2026 /bin/sh",
                         "52 1 501 ?? Z Wed Nov  4 10:00:01 2026 dead")
        with mock.patch("subprocess.run", return_value=output):
            found = self.procs.query(pid=52)
        self.assertEqual([], found["processes"])
        self.assertIsNone(found["query"]["start"])

    def test_malformed_live_identity_is_unknown_and_never_an_unleased_answer(self):
        path = self.lease("active")
        path.write_text(json.dumps({"pid": 51, "state": "held", "count": 2}))
        output = self.ps("51 1 501 ?? S Wed Nov  4 10:00:00 2026 python3 holder.py")
        with mock.patch("subprocess.run", return_value=output), self.assertRaises(self.procs.ProcsError):
            self.procs.query(pid=51)
        self.assertTrue(path.exists())
        self.lease("active", start="")
        with mock.patch("subprocess.run", return_value=output), self.assertRaises(self.procs.ProcsError):
            self.procs.query(pid=51)

    def test_future_schema_is_refused_without_modifying_the_queue(self):
        self.directory.mkdir()
        path = self.directory / "schema.json"
        path.write_text('{"v": 999}\n')
        output = self.ps("51 1 501 ?? S Wed Nov  4 10:00:00 2026 python3 holder.py")
        errors = importlib.import_module("slotfather.config")
        with mock.patch("subprocess.run", return_value=output), self.assertRaisesRegex(errors.SlotError, "schema"):
            self.procs.query(pid=51)
        self.assertEqual('{"v": 999}\n', path.read_text())


class TransientUnknownTests(unittest.TestCase):
    """fathers#158: a pid whose cwd or environment cannot be read is re-checked; one that exited in between is gone,
    only one still running is unknown, and each unknown is named by its command. Real child processes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="slotfather-procs-")
        self.addCleanup(self.tmp.cleanup)
        self.work = os.path.realpath(self.tmp.name)
        self.environment = mock.patch.dict(os.environ, {
            "SLOTFATHER_DIR": str(Path(self.work) / "state"), "SLOTFATHER_LEGACY_DIRS": "",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.procs = importlib.import_module("slotfather.procs")
        self.child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "fathers-158-child"],
                                      cwd=self.work)
        self.addCleanup(self.stop_child)

    def stop_child(self):
        if self.child.poll() is None:
            self.child.kill()
        self.child.wait()

    def test_a_process_that_exits_before_its_cwd_is_read_is_not_unknown(self):
        real = self.procs.read_cwds

        def exit_first(processes: dict[int, object], uid: int, exempt: set[int] | None = None) -> dict[int, str]:
            self.assertIn(self.child.pid, processes, "the child was enumerated")
            self.stop_child()
            return real(processes, uid, exempt)

        with mock.patch.object(self.procs, "read_cwds", exit_first):
            found = self.procs.query(cwd=self.work)
        self.assertNotIn(self.child.pid, found["cwd_unknown"])
        self.assertNotIn(self.child.pid, [item["pid"] for item in found["unknown"]])

    def test_a_live_process_whose_cwd_cannot_be_read_is_still_unknown_and_named(self):
        real = self.procs.read_cwds

        def hide_child(processes: dict[int, object], uid: int, exempt: set[int] | None = None) -> dict[int, str]:
            found = real(processes, uid, exempt)
            found.pop(self.child.pid, None)
            return found

        with mock.patch.object(self.procs, "read_cwds", hide_child):
            found = self.procs.query(cwd=self.work)
        self.assertIn(self.child.pid, found["cwd_unknown"])
        named = {item["pid"]: item for item in found["unknown"]}[self.child.pid]
        self.assertIn("fathers-158-child", named["cmd"])
        self.assertEqual((True, False), (named["cwd_unknown"], named["lease_unknown"]))
        self.assertTrue(named["start"])


class RecheckTests(unittest.TestCase):
    """The re-check of unknowns through ps, with the tables given (fathers#158)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="slotfather-procs-")
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name) / "state"
        self.environment = mock.patch.dict(os.environ, {
            "SLOTFATHER_DIR": str(self.directory), "SLOTFATHER_LEGACY_DIRS": "",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.procs = importlib.import_module("slotfather.procs")
        queue = self.directory / "queue"
        queue.mkdir(parents=True)
        self.lease = queue / "active.json"
        self.lease.write_text(json.dumps({"pid": 51, "start": "Wed Nov  4 10:00:00 2026", "state": "held",
                                          "count": 1, "label": "active"}))
        self.table = subprocess.CompletedProcess([], 0, "51 1 501 ?? S Wed Nov  4 10:00:00 2026 python3 holder.py\n"
                                                 "53 1 501 ?? S Wed Nov  4 10:00:01 2026 git status\n", "")
        self.hidden = subprocess.CompletedProcess([], 0, "53 git status\n", "")

    def query(self, recheck: subprocess.CompletedProcess[str]) -> dict[str, object]:
        with mock.patch("sys.platform", "darwin"), \
                mock.patch("slotfather.macos_process.environment", return_value=None), \
                mock.patch("subprocess.run", side_effect=[self.table, self.hidden, recheck]) as run:
            found = self.procs.query()
        self.assertIn("-p", run.call_args.args[0], "the re-check asks ps for the unknown pids only")
        self.assertIn("53", run.call_args.args[0])
        return found

    def test_an_exited_process_whose_environment_was_unreadable_is_not_lease_unknown(self):
        found = self.query(subprocess.CompletedProcess([], 1, "", ""))
        self.assertEqual(([], []), (found["lease_unknown"], found["unknown"]))

    def test_a_reused_pid_is_not_the_unknown_process(self):
        found = self.query(subprocess.CompletedProcess([], 0, "53 S Wed Nov  4 11:30:00 2026\n", ""))
        self.assertEqual([], found["lease_unknown"])

    def test_a_running_process_whose_environment_is_unreadable_stays_lease_unknown_and_named(self):
        found = self.query(subprocess.CompletedProcess([], 0, "53 S Wed Nov  4 10:00:01 2026\n", ""))
        self.assertEqual([53], found["lease_unknown"])
        self.assertEqual([{"pid": 53, "start": "Wed Nov 4 10:00:01 2026", "cmd": "git status",
                           "cwd_unknown": False, "lease_unknown": True}], found["unknown"])

    def test_linux_exited_pid_is_dropped_while_a_live_pid_denying_its_cwd_stays_unknown(self):
        """/proc: a pid gone between the ps scan and the cwd read (no /proc entry) is dropped by the re-check, while
        a live one whose cwd answers EACCES (another credential, hidepid) stays in `cwd_unknown` and is named."""
        uid = os.geteuid()
        proc = Path(self.tmp.name) / "proc"
        (proc / "self/ns").mkdir(parents=True)
        (proc / "self/ns/mnt").symlink_to("mnt:[111]")
        for number, directory in ((51, "/home/user"), (52, "/work/lane"), (54, "/work/lane")):
            (proc / str(number) / "ns").mkdir(parents=True)
            (proc / str(number) / "ns/mnt").symlink_to("mnt:[111]")
            (proc / str(number) / "cwd").symlink_to(directory)
        real = os.readlink

        def denied(path: str | os.PathLike[str]) -> str:
            if Path(path).parent.name == "54" or Path(path).parent.parent.name == "54":
                raise PermissionError(13, "Permission denied", str(path))
            return real(path)

        table = subprocess.CompletedProcess([], 0, "\n".join((
            f"51 1 {uid} ?? S Wed Nov  4 10:00:00 2026 /bin/zsh",
            f"52 51 {uid} ?? S Wed Nov  4 10:00:01 2026 python3 test.py",
            f"53 51 {uid} ?? S Wed Nov  4 10:00:02 2026 git status",
            f"54 51 {uid} ?? S Wed Nov  4 10:00:03 2026 node hidden.js")) + "\n", "")
        live = "54 S Wed Nov  4 10:00:03 2026\n"
        rechecks = {"exited": subprocess.CompletedProcess([], 1, live, ""),
                    "zombie": subprocess.CompletedProcess([], 0, "53 Z Wed Nov  4 10:00:02 2026\n" + live, ""),
                    "reused": subprocess.CompletedProcess([], 0, "53 S Wed Nov  4 11:30:00 2026\n" + live, "")}
        for name, recheck in rechecks.items():
            with self.subTest(recheck=name), mock.patch("sys.platform", "linux"), \
                    mock.patch.dict(os.environ, {"SLOTFATHER_PROC": str(proc)}), \
                    mock.patch("os.getpid", return_value=9999), mock.patch("os.readlink", denied), \
                    mock.patch("subprocess.run", side_effect=[table, recheck]) as run:
                found = self.procs.query(cwd="/work/lane")
            self.assertEqual([52], [item["pid"] for item in found["processes"]])
            self.assertEqual([54], found["cwd_unknown"], "only the live pid that denies its cwd is unknown")
            self.assertEqual([{"pid": 54, "start": "Wed Nov 4 10:00:03 2026", "cmd": "node hidden.js",
                               "cwd_unknown": True, "lease_unknown": False}], found["unknown"])
            self.assertEqual({"53", "54"}, set(run.call_args.args[0][-1].split(",")),
                             "one ps re-checks exactly the pids without a cwd")

    def test_a_recheck_ps_that_fails_fails_the_query(self):
        for answer in (subprocess.CompletedProcess([], 1, "", "ps: permission denied"),
                       subprocess.TimeoutExpired(["ps"], 5)):
            with self.subTest(answer=answer), mock.patch("sys.platform", "darwin"), \
                    mock.patch("slotfather.macos_process.environment", return_value=None), \
                    mock.patch("subprocess.run", side_effect=[self.table, self.hidden, answer]), \
                    self.assertRaises(self.procs.ProcsError):
                self.procs.query()


class LinuxNonDumpableTests(unittest.TestCase):
    """fathers#164: non-dumpable systemd user-session managers and managed service MainPIDs are omitted from
    cwd_unknown, while any non-dumpable process started inside a lane's worktree still counts as unknown."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="slotfather-procs-164-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(os.path.realpath(self.tmp.name))
        self.directory = self.root / "state"
        self.worktree = self.root / "worktree"
        self.worktree.mkdir()
        self.proc = self.root / "proc"
        (self.proc / "self/ns").mkdir(parents=True)
        (self.proc / "self/ns/mnt").symlink_to("mnt:[111]")
        self.environment = mock.patch.dict(os.environ, {
            "SLOTFATHER_DIR": str(self.directory),
            "SLOTFATHER_LEGACY_DIRS": "",
            "SLOTFATHER_PROC": str(self.proc),
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.procs = importlib.import_module("slotfather.procs")

    def add_proc(self, pid: int, cgroup: str, cwd: str | None = None, ns: str = "mnt:[111]") -> None:
        entry = self.proc / str(pid)
        (entry / "ns").mkdir(parents=True, exist_ok=True)
        (entry / "cgroup").write_text(f"0::{cgroup}\n")
        if cwd is not None:
            (entry / "ns/mnt").symlink_to(ns)
            (entry / "cwd").symlink_to(cwd)

    def test_non_dumpable_systemd_user_sd_pam_and_service_main_pid_are_omitted_while_worktree_process_refuses(
            self) -> None:
        import errno
        uid = os.geteuid()
        # 850: systemd --user, 851: (sd-pam), 860: ssh-agent.service MainPID,
        # 870: detached non-dumpable child inside actions.runner.service (not MainPID),
        # 880: non-dumpable process started inside a lane's worktree under shell 879,
        # 890: readable process inside the worktree.
        self.add_proc(850, f"/user.slice/user-{uid}.slice/user@{uid}.service/init.scope")
        self.add_proc(851, f"/user.slice/user-{uid}.slice/user@{uid}.service/init.scope")
        self.add_proc(860, f"/user.slice/user-{uid}.slice/user@{uid}.service/app.slice/ssh-agent.service")
        self.add_proc(870, "/ci.slice/actions.runner.x.service")
        self.add_proc(879, f"/user.slice/user-{uid}.slice/session-3.scope", cwd=str(self.worktree))
        self.add_proc(880, f"/user.slice/user-{uid}.slice/session-3.scope")
        self.add_proc(890, f"/user.slice/user-{uid}.slice/session-3.scope", cwd=str(self.worktree))

        real_readlink = os.readlink
        denied = {
            str(self.proc / str(pid) / sub)
            for pid in (850, 851, 860, 870, 880)
            for sub in ("ns/mnt", "cwd")
        }

        def fake_readlink(path: os.PathLike[str] | str) -> str:
            if os.fspath(path) in denied:
                raise PermissionError(errno.EACCES, "Permission denied", os.fspath(path))
            return real_readlink(path)

        ps_table = subprocess.CompletedProcess([], 0, "\n".join([
            f"850 1 {uid} ?? S Wed Nov  4 10:00:00 2026 /usr/lib/systemd/systemd --user",
            f"851 850 {uid} ?? S Wed Nov  4 10:00:00 2026 (sd-pam)",
            f"860 850 {uid} ?? S Wed Nov  4 10:00:01 2026 /usr/bin/ssh-agent -D",
            f"870 1 {uid} ?? S Wed Nov  4 10:00:02 2026 /work/secret-helper",
            f"879 1 {uid} ?? S Wed Nov  4 10:00:03 2026 /bin/bash",
            f"880 879 {uid} ?? S Wed Nov  4 10:00:04 2026 ./non-dumpable-in-lane",
            f"890 879 {uid} ?? S Wed Nov  4 10:00:05 2026 python3 test.py",
        ]) + "\n", "")
        recheck_ps = subprocess.CompletedProcess([], 0, "\n".join([
            "870 S Wed Nov  4 10:00:02 2026",
            "880 S Wed Nov  4 10:00:04 2026",
        ]) + "\n", "")

        def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            if argv[0] == "systemctl":
                unit = argv[-1]
                main = "860\n" if unit == "ssh-agent.service" else "500\n"
                return subprocess.CompletedProcess(argv, 0, main, "")
            if "-p" in argv:
                return recheck_ps
            return ps_table

        with mock.patch("sys.platform", "linux"), mock.patch("os.readlink", side_effect=fake_readlink), \
                mock.patch("subprocess.run", side_effect=fake_run):
            found = self.procs.query(cwd=str(self.worktree))

        self.assertEqual([879, 890], [item["pid"] for item in found["processes"]])
        self.assertEqual([870, 880], found["cwd_unknown"],
                         "systemd --user (850), (sd-pam) (851) and ssh-agent MainPID (860) are omitted; "
                         "non-MainPID service worker (870) and lane process (880) stay unknown")
        self.assertEqual([870, 880], [item["pid"] for item in found["unknown"]])

    def test_systemctl_failure_fails_closed_keeping_service_process_unknown(self) -> None:
        import errno
        uid = os.geteuid()
        self.add_proc(860, f"/user.slice/user-{uid}.slice/user@{uid}.service/app.slice/ssh-agent.service")
        real_readlink = os.readlink
        denied = {str(self.proc / "860/ns/mnt"), str(self.proc / "860/cwd")}

        def fake_readlink(path: os.PathLike[str] | str) -> str:
            if os.fspath(path) in denied:
                raise PermissionError(errno.EACCES, "Permission denied", os.fspath(path))
            return real_readlink(path)

        ps_table = subprocess.CompletedProcess(
            [], 0, f"860 1 {uid} ?? S Wed Nov  4 10:00:01 2026 /usr/bin/ssh-agent -D\n", "")
        recheck_ps = subprocess.CompletedProcess([], 0, "860 S Wed Nov  4 10:00:01 2026\n", "")

        def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            if argv[0] == "systemctl":
                return subprocess.CompletedProcess(argv, 1, "", "Failed to connect to bus")
            return recheck_ps if "-p" in argv else ps_table

        with mock.patch("sys.platform", "linux"), mock.patch("os.readlink", side_effect=fake_readlink), \
                mock.patch("subprocess.run", side_effect=fake_run):
            found = self.procs.query(cwd=str(self.worktree))
        self.assertEqual([860], found["cwd_unknown"])

    @unittest.skipUnless(sys.platform.startswith("linux"), "PR_SET_DUMPABLE is Linux-specific")
    def test_real_non_dumpable_process_started_in_worktree_counts_as_unknown(self) -> None:
        script = (
            "import ctypes, sys, time\n"
            "libc = ctypes.CDLL(None, use_errno=True)\n"
            "assert libc.prctl(4, 0, 0, 0, 0) == 0\n"
            "print('ready', flush=True)\n"
            "time.sleep(60)\n"
        )
        with mock.patch.dict(os.environ, {"SLOTFATHER_PROC": "/proc"}):
            child = subprocess.Popen([sys.executable, "-c", script], cwd=self.worktree,
                                     stdout=subprocess.PIPE, text=True)
            assert child.stdout is not None
            self.addCleanup(lambda: (child.kill(), child.wait(), child.stdout and child.stdout.close()))
            self.assertEqual("ready", child.stdout.readline().strip())
            try:
                os.readlink(f"/proc/{child.pid}/cwd")
            except PermissionError:
                pass
            else:
                self.skipTest("CAP_SYS_PTRACE/root allows reading non-dumpable /proc/PID/cwd here")
            found = self.procs.query(cwd=str(self.worktree))
        self.assertIn(child.pid, found["cwd_unknown"])


if __name__ == "__main__":
    unittest.main()
