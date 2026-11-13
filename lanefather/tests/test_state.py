"""Durable state files (fathers#129): one atomic write for lane records, receipts, settings, the registry and the
installer's launcher, and a lane record a write cut short is reported, never read as no lane."""
from __future__ import annotations

import os
import pathlib
import shutil
import stat
import tempfile
from typing import Any
import unittest
import unittest.mock

from support import LaneCase


class AtomicWriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="lane-state-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_the_file_and_its_directory_are_synced_before_the_write_returns(self):
        from lanefather import state
        target = self.tmp / "a.json"
        synced: list[int] = []
        real_fsync = os.fsync

        def fsync(fd: int) -> None:
            synced.append(os.fstat(fd).st_mode)
            real_fsync(fd)
        with unittest.mock.patch("os.fsync", side_effect=fsync):
            state.atomic_write(target, "one\n")
        self.assertEqual(target.read_text(), "one\n")
        self.assertEqual([stat.S_ISREG(m) for m in synced], [True, False], "the file, then its directory")
        self.assertTrue(stat.S_ISDIR(synced[1]))
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["a.json"], "no temporary file is left")

    def test_two_writers_never_share_a_temporary_file(self):
        from lanefather import state
        names: list[str] = []
        real_replace = os.replace

        def replace(source: str, target: str) -> None:
            names.append(os.fspath(source))
            real_replace(source, target)
        with unittest.mock.patch("os.replace", side_effect=replace):
            state.atomic_write(self.tmp / "a.json", "one\n")
            state.atomic_write(self.tmp / "a.json", "two\n")
        self.assertEqual(len(set(names)), 2, names)
        self.assertNotIn(str(self.tmp / "a.json.pending"), names)
        for name in names:
            self.assertFalse(name.endswith(".json"), f"{name} would read as a lane record")

    def test_a_failed_write_keeps_the_old_file_and_leaves_no_temporary_file(self):
        from lanefather import state
        target = self.tmp / "a.json"
        state.atomic_write(target, "old\n")
        with unittest.mock.patch("os.replace", side_effect=OSError("disk full")), self.assertRaises(OSError):
            state.atomic_write(target, "new\n")
        self.assertEqual(target.read_text(), "old\n")
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["a.json"])

    def test_the_mode_is_the_given_one_or_the_umask_default(self):
        from lanefather import state
        state.atomic_write(self.tmp / "secret", "s\n", mode=0o600)
        state.atomic_write(self.tmp / "tool", "#!/bin/sh\n", mode=0o755)
        previous = os.umask(0o022)
        try:
            state.atomic_write(self.tmp / "plain", "p\n")
        finally:
            os.umask(previous)
        self.assertEqual(stat.S_IMODE((self.tmp / "secret").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.tmp / "tool").stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE((self.tmp / "plain").stat().st_mode), 0o644)

    def durable(self, target: pathlib.Path, write: Any) -> None:
        """`write` replaces `target` through a temporary file of its own, synced before the rename, and syncs the
        directory after it: a direct write_text or a shared .pending file fails here."""
        events: list[tuple[str, str]] = []
        real_fsync, real_replace = os.fsync, os.replace

        def fsync(fd: int) -> None:
            events.append(("dir-sync" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file-sync", ""))
            real_fsync(fd)

        def replace(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
            events.append(("replace", os.fspath(source)))
            self.assertEqual(os.path.realpath(destination), os.path.realpath(target))
            real_replace(source, destination)
        with unittest.mock.patch("os.fsync", side_effect=fsync), unittest.mock.patch("os.replace", side_effect=replace):
            write()
        kinds = [kind for kind, _ in events]
        self.assertEqual(kinds[-3:], ["file-sync", "replace", "dir-sync"], f"{target.name}: {events}")
        temporary = events[-2][1]
        self.assertTrue(temporary.endswith(".tmp") and pathlib.Path(temporary).parent == target.parent, temporary)
        self.assertTrue(target.exists())

    def test_every_write_site_writes_durably(self):
        import importlib
        import subprocess

        from lanefather import config, registry, settings, state
        repo = self.tmp / "repo"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / ".lanefather.toml").write_text('project = "Durable"\n')
        environment = {"LANEFATHER_STATE": str(self.tmp / "state"), "LANEFATHER_SETTINGS": str(self.tmp / "settings.json"),
                       "LANEFATHER_REGISTRY": str(self.tmp / "projects.json")}
        with unittest.mock.patch.dict(os.environ, environment):
            config.use(config.Config())
            self.addCleanup(config.use, None)
            self.durable(self.tmp / "state" / "a.json", lambda: state.save({"id": "a", "status": "planned"}))
            self.durable(self.tmp / "settings.json", lambda: settings.write({"review.start": "manual"}, None))
            self.durable(self.tmp / "projects.json", lambda: registry.note(config.load(repo / ".lanefather.toml")))
            install = importlib.import_module("install")
            launcher = self.tmp / "bin" / "lanefather"
            self.durable(launcher, lambda: install.write_atomic(launcher, "#!/bin/sh\n", 0o755))

    def test_a_directory_sync_failure_is_reported_unless_the_file_system_cannot_sync_directories(self):
        import errno

        from lanefather import state
        real_fsync = os.fsync
        for code, raised in ((errno.EIO, True), (errno.EINVAL, False), (errno.ENOTSUP, False)):
            def fsync(fd: int, code: int = code) -> None:
                if stat.S_ISDIR(os.fstat(fd).st_mode):
                    raise OSError(code, os.strerror(code))
                real_fsync(fd)
            with self.subTest(errno.errorcode[code]), unittest.mock.patch("os.fsync", side_effect=fsync):
                if raised:
                    with self.assertRaises(OSError):
                        state.atomic_write(self.tmp / "a.json", "x\n")
                else:
                    state.atomic_write(self.tmp / "a.json", "x\n")


class CutShortRecordTests(unittest.TestCase):
    """A lane record a crash left empty or half written names itself; it is not 'no lane'."""

    def setUp(self):
        from lanefather import config
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="lane-state-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = self.tmp / "state"
        self.state.mkdir()
        patcher = unittest.mock.patch.dict(os.environ, {"LANEFATHER_STATE": str(self.state)})
        patcher.start()
        self.addCleanup(patcher.stop)
        config.use(config.Config())
        self.addCleanup(config.use, None)

    def test_load_reports_a_cut_short_record(self):
        from lanefather import state
        from lanefather.errors import LaneError
        for text in ("", '{\n  "id": "a",\n  "status": "act'):
            (self.state / "a.json").write_text(text)
            with self.assertRaises(LaneError) as caught:
                state.load("a")
            self.assertIn("cut short", str(caught.exception))
            self.assertIn(str(self.state / "a.json"), str(caught.exception))
            self.assertNotIn("no lane", str(caught.exception))

    def test_listing_the_lanes_reports_a_cut_short_record_rather_than_dropping_its_claims(self):
        from lanefather import state
        from lanefather.errors import LaneError
        state.save({"id": "b", "status": "active", "claims": ["src/**"]})
        (self.state / "a.json").write_text("")
        for strict in (False, True):
            with self.assertRaises(LaneError) as caught:
                state.Store(state.config.current()).lanes(strict=strict)
            self.assertIn("cut short", str(caught.exception))
        with self.assertRaises(LaneError):
            state.occupying()

    def test_show_reports_a_cut_short_record(self):
        from lanefather import show
        from lanefather.errors import LaneError
        (self.state / "a.json").write_text('{"id": "a", "sta')
        with self.assertRaises(LaneError) as caught:
            show.summary("a", self.state)
        self.assertIn("cut short", str(caught.exception))
        self.assertIn(str(self.state / "a.json"), str(caught.exception))
        with self.assertRaises(LaneError) as caught:
            show.summary("b", self.state)
        self.assertEqual(str(caught.exception), "no lane b")

    def test_a_read_only_listing_names_the_damaged_record_beside_the_healthy_lanes(self):
        from lanefather import state
        state.save({"id": "b", "status": "active", "claims": ["src/**"]})
        (self.state / "a.json").write_text("")
        damaged: list[pathlib.Path] = []
        found = state.Store(state.config.current()).lanes(damaged=damaged)
        self.assertEqual([r["id"] for r in found], ["b"])
        self.assertEqual(damaged, [self.state / "a.json"])

    def test_a_missing_record_is_still_no_lane(self):
        from lanefather import state
        from lanefather.errors import LaneError
        with self.assertRaises(LaneError) as caught:
            state.load("a")
        self.assertIn("no lane a", str(caught.exception))
        self.assertEqual(state.lanes(), [])

    def test_a_saved_record_round_trips(self):
        from lanefather import state
        state.save({"id": "a", "status": "planned"})
        self.assertEqual(state.load("a")["status"], "planned")
        self.assertEqual(sorted(p.name for p in self.state.iterdir()), ["a.json"])


class CutShortStartTests(LaneCase):
    def test_start_refuses_over_a_cut_short_record_and_keeps_its_bytes(self):
        self.state.mkdir(parents=True, exist_ok=True)
        damaged = self.state / "a.json"
        damaged.write_text('{\n  "id": "a",\n  "sta')
        output = self.start("a", "src/**", ok=False)
        self.assertIn("cut short", output)
        self.assertEqual(damaged.read_text(), '{\n  "id": "a",\n  "sta')


if __name__ == "__main__":
    unittest.main()
