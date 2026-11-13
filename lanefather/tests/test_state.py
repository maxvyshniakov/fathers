"""Durable state files (fathers#129): one atomic write for lane records, receipts, settings, the registry and the
installer's launcher, and a lane record a write cut short is reported, never read as no lane."""
from __future__ import annotations

import os
import pathlib
import shutil
import stat
import tempfile
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

    def test_every_write_site_uses_the_one_helper(self):
        root = pathlib.Path(__file__).resolve().parent.parent
        for name in ("lanefather/state.py", "lanefather/settings.py", "lanefather/registry.py", "install.py"):
            text = (root / name).read_text()
            self.assertNotIn("NamedTemporaryFile", text, name)
            self.assertNotIn('".pending"', text, name)


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
