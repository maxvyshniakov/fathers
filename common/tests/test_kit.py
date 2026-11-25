"""Unit tests for the shared runtime kit in `common/fathers_common` (fathers#117)."""

from __future__ import annotations

import contextlib
import errno
import fcntl
import io
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import eventcheck
from fathers_common import atomic, changelog, cli, errors, events, installer


class FormatTopHelpTests(unittest.TestCase):
    def test_formats_grouped_commands_and_options_with_aligned_columns(self) -> None:
        text = cli.format_top_help(
            ("tool <command>", "tool --version"),
            "One-line summary.",
            (("core", (("run", "run a command"), ("capabilities", "show capabilities"))),),
            (("-h, --help", "show this help and exit"),),
            "Run 'tool <command> --help' for details.",
        )
        lines = text.splitlines()
        self.assertEqual("usage: tool <command>", lines[0])
        self.assertEqual("       tool --version", lines[1])
        self.assertIn("  run           run a command", lines)
        self.assertIn("  capabilities  show capabilities", lines)
        self.assertIn("  -h, --help    show this help and exit", lines)
        self.assertEqual("Run 'tool <command> --help' for details.", lines[-1])


class ErrorsTests(unittest.TestCase):
    def test_exit_codes_and_refusals_follow_cli_convention(self) -> None:
        self.assertEqual((0, 1, 2, 3, 4, 5, 6, 75), (
            errors.OK, errors.FAILED, errors.USAGE, errors.REFUSED,
            errors.FOREIGN, errors.READ_ONLY, errors.NOT_FOUND, errors.TEMPORARY,
        ))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = errors.refuse("spawnfather", errors.Refused("entry j1 is not running", errors.REFUSED))
        self.assertEqual(3, code)
        self.assertEqual(["spawnfather: entry j1 is not running"], err.getvalue().splitlines())

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = errors.refuse("godfather", errors.Usage("unknown option --bad", "events"), command="ignored")
        self.assertEqual(2, code)
        self.assertEqual(["godfather: unknown option --bad", "Try 'godfather events --help'."],
                         err.getvalue().splitlines())

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = errors.refuse("slotfather", errors.Usage("no command"), command="run")
        self.assertEqual(2, code)
        self.assertEqual(["slotfather: no command", "Try 'slotfather run --help'."], err.getvalue().splitlines())


class AtomicTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="common-atomic-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_atomic_write_syncs_file_then_directory_and_uses_unique_temp_names(self) -> None:
        target = self.tmp / "nested" / "record.json"
        synced: list[str] = []
        temps: list[str] = []
        real_fsync, real_replace = os.fsync, os.replace

        def spy_fsync(fd: int) -> None:
            synced.append("dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
            real_fsync(fd)

        def spy_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
            temps.append(os.fspath(src))
            real_replace(src, dst)

        with mock.patch("os.fsync", side_effect=spy_fsync), mock.patch("os.replace", side_effect=spy_replace):
            atomic.atomic_write(target, '{"v": 1}\n')
            atomic.atomic_write(target, b'{"v": 2}\n', mode=0o600)
        self.assertEqual('{"v": 2}\n', target.read_text(encoding="utf-8"))
        self.assertEqual(0o600, stat.S_IMODE(target.stat().st_mode))
        self.assertEqual(["file", "dir", "file", "dir"], synced)
        self.assertEqual(2, len(set(temps)))
        self.assertTrue(all(name.endswith(".tmp") and not name.endswith(".json") for name in temps))
        self.assertEqual(["record.json"], [p.name for p in target.parent.iterdir()])

    def test_failed_or_short_write_leaves_target_and_cleans_temp_file(self) -> None:
        target = self.tmp / "state.json"
        atomic.atomic_write(target, "original\n")
        real_write = os.write

        def half_write(fd: int, data: bytes) -> int:
            return real_write(fd, data[: len(data) // 2])

        with mock.patch("os.write", side_effect=half_write), self.assertRaises(OSError):
            atomic.atomic_write(target, "replacement payload\n")
        self.assertEqual("original\n", target.read_text(encoding="utf-8"))
        self.assertEqual(["state.json"], [p.name for p in self.tmp.iterdir()])

        real_fsync = os.fsync

        def dir_fsync_error(code: int):
            def _fsync(fd: int) -> None:
                if stat.S_ISDIR(os.fstat(fd).st_mode):
                    raise OSError(code, os.strerror(code))
                real_fsync(fd)
            return _fsync

        with mock.patch("os.fsync", side_effect=dir_fsync_error(errno.EINVAL)):
            atomic.atomic_write(target, "unsyncable-dir-ok\n")
        self.assertEqual("unsyncable-dir-ok\n", target.read_text(encoding="utf-8"))
        with mock.patch("os.fsync", side_effect=dir_fsync_error(errno.EIO)), self.assertRaises(OSError):
            atomic.atomic_write(target, "dir-eio\n")

    def test_atomic_symlink_swaps_target(self) -> None:
        r1 = self.tmp / "releases" / "r1"
        r2 = self.tmp / "releases" / "r2"
        r1.mkdir(parents=True)
        r2.mkdir(parents=True)
        current = self.tmp / "current"
        atomic.atomic_symlink(current, r1, relative_to=self.tmp)
        self.assertEqual(Path("releases/r1"), Path(os.readlink(current)))
        atomic.atomic_symlink(current, r2, relative_to=self.tmp)
        self.assertEqual(r2.resolve(), current.resolve())


class EventsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="common-events-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.log = self.tmp / "events.jsonl"

    def test_sanitizes_control_separators_caps_and_passes_strict_validator(self) -> None:
        ev = events.build_event(
            "slotfather",
            "lease.granted",
            {"type": "request", "id": "r1", "label": "a\x1b[31mb\u2028c\u2029d", "none": None},
            {"type": "process", "pid": 42},
            {
                "cmd": events.command(["python3", "x" * 600]),
                "long": "z" * 1500,
                "nan": float("nan"),
                "inf": float("inf"),
                "deep": {"a": {"b": {"c": {"d": 1}}}},
            },
        )
        self.assertTrue(events.append_event(self.log, ev))
        self.assertEqual([], eventcheck.validate_file(str(self.log), strict=True))
        self.assertEqual(0o600, stat.S_IMODE(self.log.stat().st_mode))
        raw_lines = self.log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(1, len(raw_lines))
        decoded = json.loads(raw_lines[0])
        self.assertEqual("a [31mb c d", decoded["subject"]["label"])
        self.assertNotIn("none", decoded["subject"])
        self.assertEqual(events.MAX_COMMAND, len(decoded["details"]["cmd"]))
        self.assertEqual(events.MAX_STRING, len(decoded["details"]["long"]))
        self.assertIsNone(decoded["details"]["nan"])
        self.assertIsNone(decoded["details"]["inf"])
        self.assertIsInstance(decoded["details"]["deep"]["a"]["b"], str)

    def test_oversized_payload_truncates_details_and_bounded_lock_drops_when_held(self) -> None:
        huge = events.build_event(
            "lanefather",
            "lane.checked",
            {"type": "lane", "id": "l1"},
            {"type": "process", "pid": 1},
            {"message": "kept message", "items": ["x" * 1000 for _ in range(100)]},
        )
        self.assertTrue(events.append_event(self.log, huge))
        self.assertEqual([], eventcheck.validate_file(str(self.log), strict=True))
        decoded = json.loads(self.log.read_text(encoding="utf-8"))
        self.assertEqual({"truncated": True, "message": "kept message"}, decoded["details"])

        with open(self.tmp / "events.lock", "a", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertFalse(events.append_event(self.log, huge, lock_wait=0.05))

    def test_rotates_and_repairs_torn_trailing_line(self) -> None:
        self.log.write_bytes(b'{"torn":1}')  # no trailing newline
        ev = events.build_event("spawnfather", "sweep.finished", {"type": "sweep", "id": "s1"}, {"type": "tool"})
        self.assertTrue(events.append_event(self.log, ev))
        lines = self.log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(2, len(lines))
        self.assertEqual([], eventcheck.validate_line(lines[1], strict=True))

        for index in range(5):
            item = events.build_event("spawnfather", "sweep.finished", {"type": "sweep", "id": f"s-{index}"},
                                      {"type": "tool"}, {"i": index})
            self.assertTrue(events.append_event(self.log, item, rotate_at=350))
        self.assertTrue((self.tmp / "events.jsonl.1").is_file())


class ChangelogTests(unittest.TestCase):
    def test_report_parses_unreleased_builds_and_validates_since(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            md = Path(raw) / "CHANGELOG.md"
            md.write_text(
                "# Tool Changelog\n\n## Unreleased\n\n### Added\n\n- Next\n  line.\n\n"
                "## 0.1.0+b2 - 2026-11-12\n\n### Fixed\n\n- Build fix.\n\n"
                "## 0.1.0 - 2026-11-10\n\n### Added\n\n- Initial.\n",
                encoding="utf-8",
            )
            full = changelog.report("slotfather", "0.1.0", md, commit="deadbeef1234")
            self.assertEqual(3, len(full["entries"]))
            self.assertEqual("Next line.", full["entries"][0]["text"])
            self.assertEqual("deadbeef1234", full["entries"][0]["source"])
            self.assertNotIn("unreleased", full["entries"][1])
            filtered = changelog.report("slotfather", "0.1.0", md, since="0.1.0")
            self.assertEqual([None, "0.1.0+b2"], [e["version"] for e in filtered["entries"]])
            missing = changelog.report("slotfather", "0.1.0", Path(raw) / "missing.md")
            self.assertEqual({"source": None, "entries": []},
                             {"source": missing["source"], "entries": missing["entries"]})
            with self.assertRaisesRegex(ValueError, "--since"):
                changelog.report("slotfather", "0.1.0", md, since="bad")


class InstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="common-installer-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_stage_release_verifies_immutability_and_swap_commands_rolls_back(self) -> None:
        releases = self.tmp / "releases"
        pkg = self.tmp / "pkg"
        pkg.mkdir()
        (pkg / "mod.py").write_text("X = 1\n", encoding="utf-8")

        def populate(pending: Path) -> None:
            installer.copy_python_tree(pkg, pending / "pkg")

        rel = installer.stage_release(releases, "0.1.0-abc", populate, world_readable=True)
        self.assertEqual(0o644, stat.S_IMODE((rel / "pkg" / "mod.py").stat().st_mode))
        self.assertEqual(rel, installer.stage_release(releases, "0.1.0-abc", populate, world_readable=True))
        (rel / "pkg" / "mod.py").write_text("tampered\n", encoding="utf-8")
        with self.assertRaisesRegex(installer.InstallError, "immutable installed release differs"):
            installer.stage_release(releases, "0.1.0-abc", populate)

        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "cmd-a").write_text("old-a\n", encoding="utf-8")
        (bin_dir / "cmd-b").write_text("old-b\n", encoding="utf-8")

        def fail_b(_target: Path) -> None:
            raise OSError("disk full")

        with self.assertRaisesRegex(installer.InstallError, "restored previous commands"):
            installer.swap_commands(
                bin_dir,
                [
                    ("cmd-a", lambda target: installer.write_launcher(target, "new-a\n")),
                    ("cmd-b", fail_b),
                ],
            )
        self.assertEqual("old-a\n", (bin_dir / "cmd-a").read_text(encoding="utf-8"))
        self.assertEqual("old-b\n", (bin_dir / "cmd-b").read_text(encoding="utf-8"))

    def test_install_lock_times_out_when_held(self) -> None:
        lock_file = self.tmp / "install.lock"
        with lock_file.open("a", encoding="utf-8") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(installer.InstallError, "in progress"), \
                    installer.install_lock(lock_file, wait=0.05):
                pass


if __name__ == "__main__":
    unittest.main()
