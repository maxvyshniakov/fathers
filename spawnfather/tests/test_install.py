"""install.py: release staging, legacy `junkfather` forwarding shim, state/settings migration, launchd and hooks."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import unittest
from unittest import mock

from support import ROOT, Case, as_entry

from spawnfather import background as hook_line
from spawnfather.state import Store


class InstallTests(Case):
    def install(self, *extra: str) -> subprocess.CompletedProcess[str]:
        env = {**self.env, "HOME": str(self.tmp / "home")}
        return subprocess.run(
            [sys.executable, str(ROOT / "install.py"), "--bin", str(self.tmp / "bin"),
             "--share", str(self.tmp / "share"), *extra],
            capture_output=True, text=True, env=env,
        )

    def test_the_command_runs_the_current_release_and_names_its_commit(self) -> None:
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--", "spawnfather", "install.py", "../common/fathers_common"],
            cwd=ROOT, capture_output=True, text=True,
        ).stdout.strip()
        result = self.install()
        if dirty:
            self.assertNotEqual(0, result.returncode, "uncommitted shipping inputs are refused")
            self.assertRegex(result.stderr, "uncommitted|committed Git checkout")
            return
        self.assertEqual(0, result.returncode, result.stderr)
        command = self.tmp / "bin" / "spawnfather"
        shim = self.tmp / "bin" / "junkfather"
        self.assertTrue(command.read_text().startswith("#!/"))
        self.assertTrue(shim.read_text().startswith("#!/"))
        shown = subprocess.run([str(command), "version"], capture_output=True, text=True, env=self.env, cwd=self.tmp)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout
        self.assertIn(f"({head.strip()[:12]})", shown.stdout)
        current = (self.tmp / "share" / "current").resolve()
        self.assertTrue((current / "spawnfather" / "cli.py").is_file())
        self.assertTrue((current / "fathers_common" / "cli.py").is_file())
        self.assertTrue((current / "CHANGELOG.md").is_file())
        self.assertFalse(list(current.rglob("__pycache__")))
        self.assertEqual(0, self.install().returncode, "a second install of the same commit")
        self.assertEqual(1, len(os.listdir(self.tmp / "share" / "releases")))

    def test_stage_install_creates_spawnfather_and_legacy_junkfather_forwarding_shim(self) -> None:
        import install as install_mod

        old_releases = self.tmp / "home" / ".local" / "share" / "junkfather" / "releases" / "0.1.0-oldcommit"
        old_releases.mkdir(parents=True)
        (old_releases / "sentinel.txt").write_text("keep live supervisor release\n")

        bin_dir = self.tmp / "staged-bin"
        share_dir = self.tmp / "staged-share"
        release = install_mod.install(share_dir, bin_dir, sys.executable, built="abcdef1234567890")
        self.assertTrue((release / "spawnfather" / "cli.py").is_file())
        self.assertEqual("keep live supervisor release\n", (old_releases / "sentinel.txt").read_text())

        spawn_bin = bin_dir / "spawnfather"
        junk_shim = bin_dir / "junkfather"
        self.assertTrue(spawn_bin.is_file() and junk_shim.is_file())

        # JSON output on stdout stays completely unpolluted while stderr carries the single-line deprecation notice.
        listed = subprocess.run([str(junk_shim), "list", "--json"], capture_output=True, text=True, env=self.env)
        self.assertEqual(0, listed.returncode, listed.stderr)
        self.assertEqual("junkfather: deprecated; use spawnfather\n", listed.stderr)
        parsed = json.loads(listed.stdout)
        self.assertEqual([], parsed["entries"])

        # Usage error forwards exit code 2 and adds only the single deprecation line before spawnfather's error.
        bad = subprocess.run([str(junk_shim), "unknown-subcmd"], capture_output=True, text=True, env=self.env)
        self.assertEqual(2, bad.returncode)
        self.assertEqual("", bad.stdout)
        self.assertTrue(bad.stderr.startswith("junkfather: deprecated; use spawnfather\nspawnfather: "))

        # Attached `own` forwards exact argv (spaces, quotes, `--`) and non-zero exit code without touching stdout.
        payload = '{"spaced": "two words", "flag": "--not-spawnfathers"}'
        owned = subprocess.run(
            [str(junk_shim), "own", "--ttl", "1m", "--label", "two words", "--",
             sys.executable, "-c", "import sys; sys.stdout.write(sys.argv[1]); sys.exit(7)", payload],
            capture_output=True, text=True, env=self.env,
        )
        self.assertEqual(7, owned.returncode, owned.stderr)
        self.assertEqual(payload, owned.stdout)
        stderr_lines = owned.stderr.splitlines()
        self.assertEqual("junkfather: deprecated; use spawnfather", stderr_lines[0])
        self.assertEqual(2, len(stderr_lines), owned.stderr)
        self.assertTrue(stderr_lines[1].startswith("spawnfather: "), owned.stderr)


class MigrationTests(Case):
    def setUp(self) -> None:
        super().setUp()
        import install as install_mod
        self.install_module = install_mod
        self.home = self.tmp / "home"
        self.home.mkdir()

    def test_live_old_helper_store_continues_through_symlink_and_shares_flock(self) -> None:
        old_state = self.home / ".local" / "state" / "junkfather"
        new_state = self.home / ".local" / "state" / "spawnfather"
        old_share = self.home / ".local" / "share" / "junkfather" / "releases" / "0.1.0-old"
        old_share.mkdir(parents=True)
        (old_share / "keep.py").write_text("# live old supervisor\n")

        live_old_store = Store(old_state)
        live_old_store.save(as_entry({"v": 1, "id": "jlive01", "state": "running", "label": "before-migrate"}))
        (old_state / "events.jsonl").write_text('{"v":1,"source":"junkfather","kind":"entry.owned"}\n')
        (old_state / "settings.json").write_text('{"v": 1, "settings": {"reap.grace": 4}}\n')

        env = {"HOME": str(self.home)}
        migrated = self.install_module.migrate_state_dir(env=env)
        self.assertEqual(new_state, migrated)
        self.assertTrue(new_state.is_dir() and not new_state.is_symlink())
        self.assertTrue(old_state.is_symlink())
        self.assertEqual(new_state.resolve(), old_state.resolve())
        self.assertTrue((old_share / "keep.py").is_file(), "old ~/.local/share/junkfather releases stay untouched")

        # Live old helper still holding `Store(old_state)` updates and creates entries in `new_state` without
        # recreating a split store, and shares the same flock inode with `Store(new_state)`.
        new_store = Store(new_state)
        self.assertEqual("before-migrate", (new_store.load("jlive01") or {}).get("label"))
        live_old_store.update("jlive01", lambda e: e.update(label="updated-by-old-helper"))
        live_old_store.save(as_entry({"v": 1, "id": "jlive02", "state": "running", "label": "created-after"}))
        self.assertTrue(old_state.is_symlink(), "old_state remains a symlink rather than a recreated directory")
        self.assertEqual("updated-by-old-helper", (new_store.load("jlive01") or {}).get("label"))
        self.assertEqual("created-after", (new_store.load("jlive02") or {}).get("label"))
        self.assertEqual('{"v": 1, "settings": {"reap.grace": 4}}\n', (new_state / "settings.json").read_text())

        with (
            live_old_store.lock("lock", blocking=True) as outer_held,
            new_store.lock("lock", blocking=False) as inner_held,
        ):
            self.assertTrue(outer_held)
            self.assertFalse(inner_held, "both Store instances lock the exact same inode through the symlink")

        # Idempotent second run
        self.assertEqual(new_state, self.install_module.migrate_state_dir(env=env))
        self.assertTrue(old_state.is_symlink())

    def test_default_xdg_and_explicit_env_migrations_for_state_and_config(self) -> None:
        # 1. Default ~/.config/junkfather/settings.json -> ~/.config/spawnfather/settings.json
        old_cfg = self.home / ".config" / "junkfather"
        old_cfg.mkdir(parents=True)
        (old_cfg / "settings.json").write_text('{"v": 1, "settings": {"own.default_ttl": 1800}}\n')
        new_cfg = self.install_module.migrate_config_dir(env={"HOME": str(self.home)})
        self.assertEqual(self.home / ".config" / "spawnfather", new_cfg)
        self.assertTrue(old_cfg.is_symlink() and old_cfg.resolve() == new_cfg.resolve())
        self.assertEqual(1800, json.loads((new_cfg / "settings.json").read_text())["settings"]["own.default_ttl"])
        self.assertEqual(new_cfg, self.install_module.migrate_config_dir(env={"HOME": str(self.home)}))

        # 2. Custom XDG_STATE_HOME and XDG_CONFIG_HOME
        xdg_state = self.tmp / "xdg-state"
        xdg_cfg = self.tmp / "xdg-cfg"
        (xdg_state / "junkfather").mkdir(parents=True)
        (xdg_state / "junkfather" / "sweep.json").write_text('{"v": 1}\n')
        (xdg_cfg / "junkfather").mkdir(parents=True)
        (xdg_cfg / "junkfather" / "settings.json").write_text('{"v": 1, "settings": {"reap.grace": 2}}\n')
        xdg_env = {"HOME": str(self.home), "XDG_STATE_HOME": str(xdg_state), "XDG_CONFIG_HOME": str(xdg_cfg)}
        self.install_module.check_migration_conflicts(xdg_env)
        self.assertEqual(xdg_state / "spawnfather", self.install_module.migrate_state_dir(env=xdg_env))
        self.assertEqual(xdg_cfg / "spawnfather", self.install_module.migrate_config_dir(env=xdg_env))
        self.assertTrue((xdg_state / "junkfather").is_symlink())
        self.assertTrue((xdg_cfg / "junkfather").is_symlink())
        self.assertEqual('{"v": 1}\n', (xdg_state / "spawnfather" / "sweep.json").read_text())

        # 3. Explicit JUNKFATHER_STATE + SPAWNFATHER_STATE overrides, and SPAWNFATHER_STATE-only isolation
        custom_old = self.tmp / "custom-old"
        custom_new = self.tmp / "custom-new"
        custom_old.mkdir()
        (custom_old / "unowned.json").write_text('{"v": 1, "items": []}\n')
        both_env = {"HOME": str(self.home), "JUNKFATHER_STATE": str(custom_old), "SPAWNFATHER_STATE": str(custom_new)}
        self.assertEqual(custom_new, self.install_module.migrate_state_dir(env=both_env))
        self.assertTrue(custom_old.is_symlink() and custom_old.resolve() == custom_new.resolve())

        only_new_env = {"HOME": str(self.home), "SPAWNFATHER_STATE": str(self.tmp / "sandbox-only")}
        self.assertEqual((None, self.tmp / "sandbox-only"), self.install_module.resolve_state_paths(only_new_env))
        self.assertEqual((None, self.home / ".config" / "spawnfather"),
                         self.install_module.resolve_config_paths(only_new_env))

    def test_independent_stores_or_configs_refuse_cleanly_before_any_mutation(self) -> None:
        old_state = self.home / ".local" / "state" / "junkfather"
        new_state = self.home / ".local" / "state" / "spawnfather"
        (old_state / "entries").mkdir(parents=True)
        (new_state / "entries").mkdir(parents=True)
        (old_state / "entries" / "jold.json").write_text('{"id": "jold"}\n')
        (new_state / "entries" / "jnew.json").write_text('{"id": "jnew"}\n')

        bin_dir = self.tmp / "conflict-bin"
        share_dir = self.tmp / "conflict-share"
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}, clear=False), \
                mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SPAWNFATHER_STATE", None)
            os.environ.pop("JUNKFATHER_STATE", None)
            with mock.patch.object(self.install_module, "install") as stage_spy, \
                    mock.patch.object(self.install_module, "launchd") as launchd_spy, \
                    mock.patch.object(self.install_module, "claude_hooks") as hooks_spy, \
                    mock.patch("sys.stderr", err):
                code = self.install_module.main([
                    "--bin", str(bin_dir), "--share", str(share_dir), "--launchd", "--claude-hooks",
                ])
        self.assertEqual(1, code)
        self.assertIn("cannot migrate state", err.getvalue())
        self.assertFalse(stage_spy.called or launchd_spy.called or hooks_spy.called,
                         "conflict check fails before staging releases, launchd, or hook edits")
        self.assertFalse(bin_dir.exists() or share_dir.exists())
        self.assertFalse(old_state.is_symlink() or new_state.is_symlink())
        self.assertEqual('{"id": "jold"}\n', (old_state / "entries" / "jold.json").read_text())
        self.assertEqual('{"id": "jnew"}\n', (new_state / "entries" / "jnew.json").read_text())

        # Conflicting config directories also refuse cleanly; identical single settings.json deduplicates cleanly.
        shutil.rmtree(new_state)
        old_cfg = self.home / ".config" / "junkfather"
        new_cfg = self.home / ".config" / "spawnfather"
        old_cfg.mkdir(parents=True)
        new_cfg.mkdir(parents=True)
        (old_cfg / "settings.json").write_text('{"v": 1, "settings": {"reap.grace": 2}}\n')
        (new_cfg / "settings.json").write_text('{"v": 1, "settings": {"reap.grace": 9}}\n')
        with self.assertRaisesRegex(self.install_module.InstallError, "cannot migrate config"):
            self.install_module.check_migration_conflicts({"HOME": str(self.home)})
        self.assertFalse(old_cfg.is_symlink())

        (new_cfg / "settings.json").write_text('{"v": 1, "settings": {"reap.grace": 2}}\n')
        self.install_module.check_migration_conflicts({"HOME": str(self.home)})
        self.install_module.migrate_config_dir(env={"HOME": str(self.home)})
        self.assertTrue(old_cfg.is_symlink() and old_cfg.resolve() == new_cfg.resolve())


class InstallOptionTests(Case):
    """--launchd, --mastack, and --claude-hooks against fakes in a scratch HOME."""

    def setUp(self) -> None:
        super().setUp()
        import install as install_mod
        self.install_module = install_mod
        self.home = self.tmp / "home"
        self.fakes = self.tmp / "fakes"
        self.fakes.mkdir()
        self.calls = self.tmp / "calls.log"
        self.command = self.script("spawnfather", 'echo "<plist $*>"')
        self.launchctl = self.script("launchctl", 'echo "launchctl $*" >> "$CALLS"; exit "${LAUNCHCTL_EXIT:-0}"')
        environment = {
            "HOME": str(self.home),
            "PATH": f"{self.fakes}{os.pathsep}{os.environ['PATH']}",
            "CALLS": str(self.calls),
            "MASTACK_CONFIG": str(self.tmp / "mastack-config"),
            "MASTACK_ROOT": str(self.tmp / "mastack"),
            "CLAUDE_CONFIG_DIR": str(self.tmp / "claude-config"),
        }
        patcher = mock.patch.dict(os.environ, environment)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.assertEqual(str(self.launchctl), shutil.which("launchctl"))

    def script(self, name: str, body: str, where: "Path | None" = None) -> Path:
        path = (where or self.fakes) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"#!/bin/sh\n{body}\n")
        path.chmod(0o755)
        return path

    def test_launchd_retires_legacy_agent_writes_spawnfather_agent_and_loads_it(self) -> None:
        agents = self.home / "Library" / "LaunchAgents"
        agents.mkdir(parents=True)
        legacy_plist = agents / "com.junkfather.sweep.plist"
        legacy_plist.write_text("<plist>old</plist>\n")

        real = self.script("spawnfather-checkout", f'cd "{ROOT}" && exec "{sys.executable}" -m spawnfather "$@"')
        plist = self.install_module.launchd(real)
        self.assertEqual(agents / "com.spawnfather.sweep.plist", plist)
        self.assertFalse(legacy_plist.exists(), "legacy com.junkfather.sweep.plist was unlinked")
        agent = plistlib.loads(plist.read_bytes())
        self.assertEqual("com.spawnfather.sweep", agent["Label"])
        self.assertEqual(["sweep", "--quiet"], agent["ProgramArguments"][-2:])
        self.assertEqual((3600, True), (agent["StartInterval"], agent["RunAtLoad"]))
        self.assertEqual(str(self.state), agent["EnvironmentVariables"]["SPAWNFATHER_STATE"])
        self.assertEqual(str(self.state / "sweep.out"), agent["StandardOutPath"])
        domain = f"gui/{os.getuid()}"
        self.assertEqual(
            [
                f"launchctl bootout {domain}/com.junkfather.sweep",
                f"launchctl bootout {domain}/com.spawnfather.sweep",
                f"launchctl bootstrap {domain} {plist}",
            ],
            self.calls.read_text().splitlines(),
        )

    def test_launchd_bootstrap_failure_then_retry_leaves_only_spawnfather_sweep(self) -> None:
        agents = self.home / "Library" / "LaunchAgents"
        agents.mkdir(parents=True)
        legacy_plist = agents / "com.junkfather.sweep.plist"
        legacy_plist.write_text("<plist>old</plist>\n")
        real = self.script("spawnfather-checkout", f'cd "{ROOT}" && exec "{sys.executable}" -m spawnfather "$@"')

        self.script(
            "launchctl",
            'echo "launchctl $*" >> "$CALLS"\n'
            'if [ "$1" = "bootstrap" ] && [ "${FAIL_BOOTSTRAP:-0}" = "1" ]; then\n'
            '  echo "Bootstrap failed: 5: Input/output error" >&2\n'
            '  exit 5\n'
            'fi\n'
            'exit 0',
        )
        os.environ["FAIL_BOOTSTRAP"] = "1"
        with mock.patch.object(self.install_module.time, "sleep") as pause, \
                self.assertRaises(self.install_module.InstallError):
            self.install_module.launchd(real)
        self.assertEqual(5, pause.call_count)
        self.assertFalse(legacy_plist.exists(), "legacy plist unlinked before bootstrap retry")

        os.environ["FAIL_BOOTSTRAP"] = "0"
        self.calls.write_text("")
        plist = self.install_module.launchd(real)
        self.assertTrue(plist.is_file())
        self.assertFalse(legacy_plist.exists())
        self.assertEqual(["com.spawnfather.sweep.plist"], sorted(p.name for p in agents.iterdir()))
        domain = f"gui/{os.getuid()}"
        self.assertEqual(
            [
                f"launchctl bootout {domain}/com.junkfather.sweep",
                f"launchctl bootout {domain}/com.spawnfather.sweep",
                f"launchctl bootstrap {domain} {plist}",
            ],
            self.calls.read_text().splitlines(),
        )

    def test_mastack_rewrites_old_hooks_preserves_symlink_and_preserves_disabled_rewriter(self) -> None:
        config = self.tmp / "mastack-config"
        config.mkdir()
        real = self.tmp / "dotfiles" / "hooks.json"
        real.parent.mkdir()
        # Post-#176 / disabled-rewriter config with old junkfather paths and unrelated hooks:
        real.write_text(json.dumps({
            "lifecycle": {
                "Stop": ["other hook", "/old/bin/junkfather hook"],
                "SubagentStop": ["/old/bin/junkfather hook"],
                "SessionEnd": ["sh -c '[ -x \"$0\" ] || exit 0; exec \"$0\" hook' /old/bin/junkfather"],
                "PreToolUse": [
                    {"command": "other pre-hook"},
                    {"command": "/old/bin/junkfather hook", "contains": ["junkfather"]},
                ],
            },
            "kept": True,
        }))
        (config / "hooks.json").symlink_to(real)
        self.script("install", 'echo "mastack $*" >> "$CALLS"', where=self.tmp / "mastack" / "bin")

        self.assertFalse(self.install_module.mastack(self.command))
        self.assertFalse(self.install_module.mastack(self.command))
        hook = f"{self.command} hook"
        data = json.loads(real.read_text())
        self.assertTrue((config / "hooks.json").is_symlink())
        self.assertTrue(data["kept"])
        self.assertEqual(["other hook", hook], data["lifecycle"]["Stop"])
        for event in ("SubagentStop", "SessionEnd"):
            self.assertEqual([hook], data["lifecycle"][event])
        self.assertEqual(
            [
                {"command": "other pre-hook"},
                {"command": hook, "contains": ["spawnfather", "junkfather"]},
            ],
            data["lifecycle"]["PreToolUse"],
            "disabled background rewriter stays disabled (#176): no rewrite:true entry was inserted",
        )
        self.assertEqual(["mastack --only hooks"] * 2, self.calls.read_text().splitlines())

        # If a user already had an active `"rewrite": true` entry, migrating updates it in place to spawnfather:
        real.write_text(json.dumps({
            "lifecycle": {
                "PreToolUse": [
                    {"command": "/old/bin/junkfather hook", "contains": ["junkfather"]},
                    {
                        "command": "/old/bin/junkfather hook",
                        "background": True,
                        "rewrite": True,
                        "allow": ["Bash(/old/bin/junkfather background:*)"],
                        "replaces": ['sh -c \'[ -x "$0" ] || exit 0; exec "$0" hook\' /custom/junkfather'],
                    },
                ],
            },
        }))
        self.assertTrue(self.install_module.mastack(self.command))
        migrated = json.loads(real.read_text())["lifecycle"]["PreToolUse"]
        self.assertEqual(2, len(migrated))
        self.assertEqual({"command": hook, "contains": ["spawnfather", "junkfather"]}, migrated[0])
        self.assertEqual(hook, migrated[1]["command"])
        self.assertTrue(migrated[1]["rewrite"] and migrated[1]["background"])
        self.assertEqual([f"Bash({self.command} background:*)"], migrated[1]["allow"])
        self.assertIn('sh -c \'[ -x "$0" ] || exit 0; exec "$0" hook\' /custom/junkfather', migrated[1]["replaces"])
        self.assertIn('sh -c \'[ -x "$0" ] || exit 0; exec "$0" hook\' /old/bin/junkfather', migrated[1]["replaces"])
        self.assertIn(f'sh -c \'[ -x "$0" ] || exit 0; exec "$0" hook\' {self.command}', migrated[1]["replaces"])

    def test_claude_hooks_rewrites_old_entries_preserves_symlink_and_preserves_disabled_rewriter(self) -> None:
        claude_dir = self.tmp / "claude-config"
        claude_dir.mkdir()
        real = self.tmp / "dotfiles" / "claude-settings.json"
        real.parent.mkdir(exist_ok=True)
        real.write_text(json.dumps({
            "hooks": {
                "Stop": [{"hooks": [
                    {"type": "command", "command": "other-tool hook"},
                    {"type": "command", "command": "/opt/bin/junkfather hook"},
                ]}],
                "SessionEnd": [{"hooks": [
                    {
                        "type": "command",
                        "command": 'sh -c \'[ -x "$0" ] || exit 0; exec "$0" hook\' /opt/bin/junkfather',
                    },
                ]}],
            },
            "permissions": {
                "allow": ["Bash(git status:*)", "Bash(/opt/bin/junkfather background:*)"],
            },
            "theme": "dark",
        }))
        (claude_dir / "settings.json").symlink_to(real)

        self.install_module.claude_hooks(self.command)
        self.install_module.claude_hooks(self.command)
        self.assertTrue((claude_dir / "settings.json").is_symlink())
        updated = json.loads(real.read_text())
        self.assertEqual("dark", updated["theme"])
        self.assertNotIn("PreToolUse", updated["hooks"], "disabled background rewriter stays absent")
        self.assertEqual(
            [{"hooks": [
                {"type": "command", "command": "other-tool hook"},
                {"type": "command", "command": f"{self.command} hook"},
            ]}],
            updated["hooks"]["Stop"],
        )
        self.assertEqual(
            [{"hooks": [{"type": "command", "command": f"{self.command} hook"}]}],
            updated["hooks"]["SubagentStop"],
        )
        self.assertEqual(
            [{"hooks": [{
                "type": "command",
                "command": f'sh -c \'[ -x "$0" ] || exit 0; exec "$0" hook\' {self.command}',
            }]}],
            updated["hooks"]["SessionEnd"],
        )
        self.assertEqual(
            ["Bash(git status:*)", f"Bash({self.command} background:*)"],
            updated["permissions"]["allow"],
        )

    def test_mastack_refuses_missing_or_broken_settings_and_a_failing_installer(self) -> None:
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

    def test_the_rule_allows_the_line_the_hook_writes(self) -> None:
        command = self.tmp / "bin dir" / "spawnfather"
        rule = self.install_module.allow_rule(command)
        prefix = rule[len("Bash("):-len(":*)")]
        line = hook_line.adopting_line("toolu_1", hook_line.shlex.quote(str(command)))
        self.assertTrue(line.startswith(prefix + " "), (rule, line))

    def test_the_flags_run_launchd_mastack_and_claude_hooks_on_the_installed_command(self) -> None:
        module = self.install_module
        for flags, darwin, loads, rewriter in (
            (["--launchd"], False, True, False),
            (["--mastack"], True, True, False),
            (["--mastack", "--enable-background-rewriter"], False, False, True),
            (["--claude-hooks"], False, False, False),
        ):
            with self.subTest(flags=flags, darwin=darwin), \
                    mock.patch.object(module, "install", return_value=self.tmp / "release"), \
                    mock.patch.object(module, "launchd", return_value=self.tmp / "agent.plist") as launchd, \
                    mock.patch.object(module, "mastack", return_value=rewriter) as mastack, \
                    mock.patch.object(module, "claude_hooks", return_value=self.tmp / "settings.json") as claude_h, \
                    mock.patch.object(module.sys, "platform", "darwin" if darwin else "linux"), \
                    mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                code = module.main(["--bin", str(self.tmp / "bin"), "--share", str(self.tmp / "share"), *flags])
            self.assertEqual(0, code)
            self.assertEqual(loads, launchd.called, "on macOS --mastack loads the sweep agent too")
            self.assertEqual("--mastack" in flags, mastack.called)
            self.assertEqual("--claude-hooks" in flags, claude_h.called)
            if mastack.called:
                mastack.assert_called_once_with(self.tmp / "bin" / "spawnfather", enable_rewriter=rewriter)
                if rewriter:
                    self.assertIn(f"Bash({self.tmp / 'bin' / 'spawnfather'} background:*)", out.getvalue())
                else:
                    self.assertIn("background rewriter left disabled", out.getvalue())

    def test_a_relative_bin_is_made_absolute(self) -> None:
        module = self.install_module
        with mock.patch.object(module, "install", return_value=self.tmp / "release") as install, \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            here = os.getcwd()
            os.chdir(self.tmp)
            self.addCleanup(os.chdir, here)
            self.assertEqual(0, module.main(["--bin", "bin", "--share", "share"]))
        self.assertEqual((self.tmp / "share", self.tmp / "bin"), install.call_args.args[:2])


if __name__ == "__main__":
    unittest.main()
