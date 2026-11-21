"""Tests of install.py: releases beside each other, `current` swapped in one rename, the command on its interpreter."""
from __future__ import annotations

import fcntl
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

from support import ROOT


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="lanefather-install-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.registry = self.tmp / "projects.json"

    def base_env(self, extra_env: dict[str, str] | None = None) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env["LANEFATHER_REGISTRY"] = str(self.registry)
        if extra_env:
            env.update(extra_env)
        return env

    def install(self, *extra: str, source: pathlib.Path | None = None,
                env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        installer = (source or ROOT) / "install.py"
        return subprocess.run(
            [sys.executable, str(installer), "--bin", str(self.tmp / "bin"),
             "--share", str(self.tmp / "share"), *extra],
            capture_output=True, text=True, env=env if env is not None else self.base_env(),
        )

    def make_source_repo(self, name: str = "source") -> tuple[pathlib.Path, dict[str, str]]:
        source = self.tmp / name / "lanefather"
        shutil.copytree(ROOT / "lanefather", source / "lanefather",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copytree(ROOT / "hooks", source / "hooks")
        shutil.copytree(ROOT.parent / "common/fathers_common", source.parent / "common/fathers_common",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copy2(ROOT / "install.py", source / "install.py")
        if (ROOT / "CHANGELOG.md").is_file():
            shutil.copy2(ROOT / "CHANGELOG.md", source / "CHANGELOG.md")
        clean_env = self.base_env()
        self._git(source, clean_env, "init", "-q", "-b", "codex/source", str(source.parent))
        self._git(source, clean_env, "config", "user.email", "install@example.invalid")
        self._git(source, clean_env, "config", "user.name", "Install test")
        self._git(source, clean_env, "add", ".", "../common")
        self._git(source, clean_env, "commit", "-qm", "source-1")
        return source, clean_env

    def _git(self, repo: pathlib.Path, env: dict[str, str], *args: str) -> str:
        result = subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def _commit_change(self, source: pathlib.Path, env: dict[str, str], label: str) -> str:
        target = source / "lanefather" / "errors.py"
        target.write_text(target.read_text() + f"\n# {label}\n")
        self._git(source, env, "add", "lanefather/errors.py")
        self._git(source, env, "commit", "-qm", label)
        return self._git(source, env, "rev-parse", "HEAD")

    def _register_state_dir(self, state_dir: pathlib.Path) -> None:
        state_dir.mkdir(parents=True, exist_ok=True)
        payload = {"v": 1, "projects": [{"state_dir": str(state_dir), "project": "test"}]}
        self.registry.write_text(json.dumps(payload))

    def test_the_command_runs_the_current_release_and_names_its_commit(self):
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        command = self.tmp / "bin" / "lanefather"
        # The interpreter as found, not its resolved versioned file, which a Python upgrade removes.
        found = os.path.abspath(shutil.which(sys.executable) or sys.executable)
        self.assertTrue(command.read_text().startswith(f"#!{found}\n"))
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "LANEFATHER_CONFIG")}
        shown = subprocess.run([str(command), "--version"], capture_output=True, text=True, env=env, cwd=self.tmp)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
        self.assertIn(f"lanefather 0.1.0 ({head}", shown.stdout)
        current = (self.tmp / "share" / "current").resolve()
        self.assertTrue((current / "lanefather" / "cli.py").is_file())
        self.assertFalse(list(current.rglob("__pycache__")))
        # fathers#104: the release ships its changelog, and `changelog --json` reads that copy.
        self.assertEqual((current / "lanefather" / "CHANGELOG.md").read_text(), (ROOT / "CHANGELOG.md").read_text())
        news = subprocess.run([str(command), "changelog", "--json"], capture_output=True, text=True, env=env,
                              cwd=self.tmp)
        answer = json.loads(news.stdout)
        self.assertEqual((answer["tool"], answer["source"]),
                         ("lanefather", str((current / "lanefather" / "CHANGELOG.md").resolve())))
        unreleased = [e for e in answer["entries"] if e.get("unreleased")]
        self.assertTrue(unreleased and all(e["source"] == head and e["version"] is None for e in unreleased))

    def test_old_releases_beyond_three_go(self):
        releases = self.tmp / "share" / "releases"
        releases.mkdir(parents=True)
        for index in range(4):
            (releases / f"0.0.{index}-old").mkdir()
            # Intentionally invert mtimes to confirm pruning uses version/build order, not mtime.
            os.utime(releases / f"0.0.{index}-old", (100 - index, 100 - index))
        self.assertEqual(self.install().returncode, 0)
        self.assertEqual(sorted(p.name for p in releases.iterdir())[:2], ["0.0.2-old", "0.0.3-old"])
        self.assertEqual(len(list(releases.iterdir())), 3)

    def test_prune_keeps_release_pinned_by_live_lane_and_drops_done_lane_pin(self):
        releases = self.tmp / "share" / "releases"
        releases.mkdir(parents=True)
        for index in range(4):
            (releases / f"0.0.{index}-old").mkdir()
        state_dir = self.tmp / "state"
        self._register_state_dir(state_dir)
        (state_dir / "live-lane.json").write_text(json.dumps({
            "id": "live-lane",
            "status": "active",
            "release": "0.0.0-old",
        }))
        (state_dir / "done-lane.json").write_text(json.dumps({
            "id": "done-lane",
            "status": "done",
            "release": "0.0.1-old",
        }))
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        kept = {p.name for p in releases.iterdir()}
        self.assertIn("0.0.0-old", kept)
        self.assertNotIn("0.0.1-old", kept)
        self.assertIn("0.0.2-old", kept)
        self.assertIn("0.0.3-old", kept)
        self.assertEqual(len(kept), 4)

    def test_no_switch_leaves_current_and_launcher_alone(self):
        source, clean_env = self.make_source_repo()
        c1 = self._git(source, clean_env, "rev-parse", "HEAD")
        first = self.install(source=source, env=clean_env)
        self.assertEqual(first.returncode, 0, first.stderr)
        current = self.tmp / "share" / "current"
        launcher = self.tmp / "bin" / "lanefather"
        before_link = current.readlink()
        before_launcher = launcher.read_bytes()

        c2 = self._commit_change(source, clean_env, "source-2")
        staged = self.install("--no-switch", source=source, env=clean_env)
        self.assertEqual(staged.returncode, 0, staged.stderr)
        self.assertEqual(current.readlink(), before_link)
        self.assertEqual(launcher.read_bytes(), before_launcher)
        releases = {p.name for p in (self.tmp / "share" / "releases").iterdir()}
        self.assertIn(f"0.1.0-{c1[:12]}", releases)
        self.assertIn(f"0.1.0-{c2[:12]}", releases)

    def test_release_in_use_survives_prune(self):
        releases = self.tmp / "share" / "releases"
        releases.mkdir(parents=True)
        for index in range(5):
            (releases / f"0.0.{index}-old").mkdir()
        # 0.0.0-old is locked by a running process via flock; 0.0.1-old has a live pid file; 0.0.2-old has a dead pid.
        lock_file = releases / "0.0.0-old" / ".in-use.lock"
        lock_file.write_text("")
        (releases / "0.0.1-old" / "worker.pid").write_text(f"{os.getpid()}\n")
        (releases / "0.0.2-old" / "worker.pid").write_text("99999999\n")
        with lock_file.open("r+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                result = self.install()
                self.assertEqual(result.returncode, 0, result.stderr)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        kept = {p.name for p in releases.iterdir()}
        self.assertIn("0.0.0-old", kept)
        self.assertIn("0.0.1-old", kept)
        self.assertNotIn("0.0.2-old", kept)
        self.assertIn("0.0.3-old", kept)
        self.assertIn("0.0.4-old", kept)

    def test_old_commit_reinstalled_with_no_switch_does_not_count_as_newest_and_never_moves_current_backwards(self):
        source, clean_env = self.make_source_repo()
        c1 = self._git(source, clean_env, "rev-parse", "HEAD")
        c2 = self._commit_change(source, clean_env, "source-2")
        c3 = self._commit_change(source, clean_env, "source-3")
        c4 = self._commit_change(source, clean_env, "source-4")

        for rev in (c1, c2, c3):
            self._git(source, clean_env, "checkout", "-q", rev)
            res = self.install(source=source, env=clean_env)
            self.assertEqual(res.returncode, 0, res.stderr)

        # Normal install of older c1 must refuse rather than move current backwards.
        self._git(source, clean_env, "checkout", "-q", c1)
        backwards = self.install(source=source, env=clean_env)
        self.assertEqual(backwards.returncode, 1)
        self.assertIn("newer", backwards.stderr)
        self.assertEqual((self.tmp / "share" / "current").readlink().name, f"0.1.0-{c3[:12]}")

        # Re-installing c1 with --no-switch leaves current at c3 and touches c1 most recently.
        reinstalled = self.install("--no-switch", source=source, env=clean_env)
        self.assertEqual(reinstalled.returncode, 0, reinstalled.stderr)
        self.assertEqual((self.tmp / "share" / "current").readlink().name, f"0.1.0-{c3[:12]}")
        os.utime(self.tmp / "share" / "releases" / f"0.1.0-{c1[:12]}", None)

        # Installing c4 prunes c1 (oldest commit), keeping c2, c3, c4 (newest three by build order).
        self._git(source, clean_env, "checkout", "-q", c4)
        res4 = self.install(source=source, env=clean_env)
        self.assertEqual(res4.returncode, 0, res4.stderr)
        kept = {p.name for p in (self.tmp / "share" / "releases").iterdir()}
        self.assertEqual(kept, {f"0.1.0-{c2[:12]}", f"0.1.0-{c3[:12]}", f"0.1.0-{c4[:12]}"})

    def test_rollback_and_uninstall_round_trip(self):
        source, clean_env = self.make_source_repo()
        c1 = self._git(source, clean_env, "rev-parse", "HEAD")
        self.assertEqual(self.install(source=source, env=clean_env).returncode, 0)
        c2 = self._commit_change(source, clean_env, "source-2")
        self.assertEqual(self.install(source=source, env=clean_env).returncode, 0)
        current = self.tmp / "share" / "current"
        self.assertEqual(current.readlink().name, f"0.1.0-{c2[:12]}")

        rolled = self.install("--rollback", source=source, env=clean_env)
        self.assertEqual(rolled.returncode, 0, rolled.stderr)
        self.assertEqual(current.readlink().name, f"0.1.0-{c1[:12]}")
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "LANEFATHER_CONFIG")}
        shown = subprocess.run([str(self.tmp / "bin" / "lanefather"), "--version"],
                               capture_output=True, text=True, env=env, cwd=self.tmp)
        self.assertIn(c1[:12], shown.stdout)

        # Pin c1 with a live lane: uninstall removes command, current, and unpinned c2, while keeping pinned c1.
        state_dir = self.tmp / "state"
        self._register_state_dir(state_dir)
        (state_dir / "pinned.json").write_text(json.dumps({
            "id": "pinned",
            "status": "active",
            "lanefather_commit": c1,
        }))
        removed = self.install("--uninstall", source=source, env=clean_env)
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertFalse((self.tmp / "bin" / "lanefather").exists())
        self.assertFalse(current.exists())
        self.assertFalse(current.is_symlink())
        remaining = {p.name for p in (self.tmp / "share" / "releases").iterdir()}
        self.assertEqual(remaining, {f"0.1.0-{c1[:12]}"})

        # Once the lane is done, uninstall removes the last release too.
        (state_dir / "pinned.json").write_text(json.dumps({
            "id": "pinned",
            "status": "done",
            "lanefather_commit": c1,
        }))
        removed_all = self.install("--uninstall", source=source, env=clean_env)
        self.assertEqual(removed_all.returncode, 0, removed_all.stderr)
        self.assertFalse((self.tmp / "share" / "releases").exists())

    def test_stale_pending_dir_is_cleaned_while_live_pending_stays(self):
        releases = self.tmp / "share" / "releases"
        releases.mkdir(parents=True)
        stale = releases / ".pending-99999999"
        stale.mkdir()
        (stale / "leftover.txt").write_text("stale")
        live = releases / f".pending-{os.getpid()}"
        live.mkdir()
        (live / "active.txt").write_text("live")

        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(stale.exists())
        self.assertTrue(live.exists())

    def test_uncommitted_shipping_inputs_are_refused_before_current_or_launcher_changes(self):
        source, clean_env = self.make_source_repo()
        command = [sys.executable, str(source / "install.py"), "--bin", str(self.tmp / "bin"),
                   "--share", str(self.tmp / "share")]
        installed = subprocess.run(command, capture_output=True, text=True, env=clean_env)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        current = self.tmp / "share" / "current"
        launcher = self.tmp / "bin" / "lanefather"
        before = (current.readlink(), launcher.read_bytes(), sorted((self.tmp / "share" / "releases").iterdir()))
        decoy = self.tmp / "decoy"
        shutil.copytree(source.parent, decoy)
        hook_env = {**clean_env, "GIT_DIR": str(decoy / ".git"), "GIT_WORK_TREE": str(decoy)}
        for relative in ["lanefather/__init__.py", "hooks/reference-transaction", "install.py",
                         "lanefather/untracked.py", "../common/fathers_common/cli.py"]:
            with self.subTest(relative=relative):
                path = source / relative
                original = path.read_bytes() if path.exists() else None
                path.write_bytes((original or b"") + b"\n# uncommitted input\n")
                for environment in (clean_env, hook_env):
                    refused = subprocess.run(command, capture_output=True, text=True, env=environment)
                    self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
                    self.assertIn("uncommitted", refused.stderr)
                self.assertEqual((current.readlink(), launcher.read_bytes(),
                                  sorted((self.tmp / "share" / "releases").iterdir())), before)
                if original is None:
                    path.unlink()
                else:
                    path.write_bytes(original)

    def test_an_old_interpreter_is_refused(self):
        result = self.install("--python", "/usr/bin/false")
        self.assertEqual(result.returncode, 1)
        self.assertIn("is not Python 3.11 or newer", result.stderr)
        self.assertFalse((self.tmp / "bin" / "lanefather").exists())


if __name__ == "__main__":
    unittest.main()
