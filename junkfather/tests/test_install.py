"""install.py: a release beside the others, `current` swapped in one rename, the command on its interpreter."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from unittest import mock

from support import ROOT, Case


class InstallTests(Case):
    def install(self, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(ROOT / "install.py"), "--bin", str(self.tmp / "bin"),
                               "--share", str(self.tmp / "share"), *extra], capture_output=True, text=True)

    def test_the_command_runs_the_current_release_and_names_its_commit(self):
        dirty = subprocess.run(["git", "status", "--porcelain", "--", "junkfather", "install.py"], cwd=ROOT,
                               capture_output=True, text=True).stdout.strip()
        result = self.install()
        if dirty:
            self.assertNotEqual(0, result.returncode, "uncommitted shipping inputs are refused")
            self.assertRegex(result.stderr, "uncommitted|committed Git checkout")
            return
        self.assertEqual(0, result.returncode, result.stderr)
        command = self.tmp / "bin" / "junkfather"
        self.assertTrue(command.read_text().startswith("#!/"))
        shown = subprocess.run([str(command), "version"], capture_output=True, text=True, env=self.env,
                               cwd=self.tmp)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout
        self.assertIn(f"({head.strip()[:12]})", shown.stdout)
        current = (self.tmp / "share" / "current").resolve()
        self.assertTrue((current / "junkfather" / "cli.py").is_file())
        self.assertTrue((current / "CHANGELOG.md").is_file())
        self.assertFalse(list(current.rglob("__pycache__")))
        self.assertEqual(0, self.install().returncode, "a second install of the same commit")
        self.assertEqual(1, len(os.listdir(self.tmp / "share" / "releases")))


class InstallOptionTests(Case):
    """--launchd and --mastack against fakes: a scratch HOME, a fake launchctl on PATH, a fake Mastack."""

    def setUp(self) -> None:
        super().setUp()
        import install
        self.install_module = install
        self.home = self.tmp / "home"
        self.fakes = self.tmp / "fakes"
        self.fakes.mkdir()
        self.calls = self.tmp / "calls.log"
        self.command = self.script("junkfather", 'echo "<plist $*>"')
        self.launchctl = self.script("launchctl", 'echo "launchctl $*" >> "$CALLS"; exit "${LAUNCHCTL_EXIT:-0}"')
        environment = {"HOME": str(self.home), "PATH": f"{self.fakes}{os.pathsep}{os.environ['PATH']}",
                       "CALLS": str(self.calls), "MASTACK_CONFIG": str(self.tmp / "mastack-config"),
                       "MASTACK_ROOT": str(self.tmp / "mastack")}
        patcher = mock.patch.dict(os.environ, environment)
        patcher.start()
        self.addCleanup(patcher.stop)
        # Never the real launchd or Mastack: stop here unless the fakes are what the installer would run.
        self.assertEqual(str(self.launchctl), shutil.which("launchctl"))

    def script(self, name: str, body: str, where: "Path | None" = None) -> Path:
        path = (where or self.fakes) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"#!/bin/sh\n{body}\n")
        path.chmod(0o755)
        return path

    def test_launchd_writes_the_agent_and_loads_it_again(self):
        import plistlib
        real = self.script("junkfather-checkout", f'cd "{ROOT}" && exec "{sys.executable}" -m junkfather "$@"')
        plist = self.install_module.launchd(real)
        self.assertEqual(self.home / "Library" / "LaunchAgents" / "com.junkfather.sweep.plist", plist)
        agent = plistlib.loads(plist.read_bytes())
        self.assertEqual("com.junkfather.sweep", agent["Label"])
        self.assertEqual(["sweep", "--quiet"], agent["ProgramArguments"][-2:])
        self.assertEqual((3600, True), (agent["StartInterval"], agent["RunAtLoad"]))
        self.assertEqual(str(self.state), agent["EnvironmentVariables"]["JUNKFATHER_STATE"])
        self.assertEqual(str(self.state / "sweep.out"), agent["StandardOutPath"])
        domain = f"gui/{os.getuid()}"
        self.assertEqual([f"launchctl bootout {domain}/com.junkfather.sweep", f"launchctl bootstrap {domain} {plist}"],
                         self.calls.read_text().splitlines())

    def test_launchd_fails_when_bootstrap_keeps_failing(self):
        os.environ["LAUNCHCTL_EXIT"] = "5"
        with mock.patch.object(self.install_module.time, "sleep") as pause, \
                self.assertRaises(self.install_module.InstallError):
            self.install_module.launchd(self.command)
        self.assertEqual(5, sum("bootstrap" in line for line in self.calls.read_text().splitlines()))
        self.assertEqual(5, pause.call_count)

    def test_mastack_adds_the_hooks_once_keeps_the_symlink_and_runs_its_installer(self):
        config = self.tmp / "mastack-config"
        config.mkdir()
        real = self.tmp / "dotfiles" / "hooks.json"
        real.parent.mkdir()
        real.write_text(json.dumps({"lifecycle": {"Stop": ["other hook"]}, "kept": True}))
        (config / "hooks.json").symlink_to(real)
        self.script("install", 'echo "mastack $*" >> "$CALLS"', where=self.tmp / "mastack" / "bin")
        self.install_module.mastack(self.command)
        self.install_module.mastack(self.command)
        hook = f"{self.command} hook"
        data = json.loads(real.read_text())
        self.assertTrue((config / "hooks.json").is_symlink())
        self.assertTrue(data["kept"])
        self.assertEqual(["other hook", hook], data["lifecycle"]["Stop"])
        for event in ("SubagentStop", "SessionEnd"):
            self.assertEqual([hook], data["lifecycle"][event])
        self.assertEqual([{"command": hook, "contains": ["junkfather"]}], data["lifecycle"]["PreToolUse"])
        self.assertEqual(["mastack --only hooks"] * 2, self.calls.read_text().splitlines())

    def test_mastack_refuses_missing_or_broken_settings_and_a_failing_installer(self):
        with self.assertRaisesRegex(self.install_module.InstallError, "install Mastack's hooks first"):
            self.install_module.mastack(self.command)
        config = self.tmp / "mastack-config"
        config.mkdir()
        (config / "hooks.json").write_text("{")
        with self.assertRaisesRegex(self.install_module.InstallError, "not JSON"):
            self.install_module.mastack(self.command)
        (config / "hooks.json").write_text("{}")
        self.script("install", "exit 3", where=self.tmp / "mastack" / "bin")
        with self.assertRaisesRegex(self.install_module.InstallError, "Mastack's installer failed"):
            self.install_module.mastack(self.command)

    def test_the_flags_run_launchd_and_mastack_on_the_installed_command(self):
        module = self.install_module
        with mock.patch.object(module, "install", return_value=self.tmp / "release"), \
                mock.patch.object(module, "launchd", return_value=self.tmp / "agent.plist") as launchd, \
                mock.patch.object(module, "mastack") as mastack:
            code = module.main(["--bin", str(self.tmp / "bin"), "--share", str(self.tmp / "share"), "--launchd",
                                "--mastack"])
        self.assertEqual(0, code)
        launchd.assert_called_once_with(self.tmp / "bin" / "junkfather")
        mastack.assert_called_once_with(self.tmp / "bin" / "junkfather")
