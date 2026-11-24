"""Installed capability negotiation and immutable source identity, without the machine's installation."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from support import ROOT


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='lanefather-runtime-'))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.source = self.tmp / 'source' / 'lanefather'
        shutil.copytree(ROOT / 'lanefather', self.source / 'lanefather',
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        shutil.copytree(ROOT / 'hooks', self.source / 'hooks')
        shutil.copytree(ROOT.parent / 'common/fathers_common', self.source.parent / 'common/fathers_common',
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        shutil.copy2(ROOT / 'install.py', self.source / 'install.py')
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(('GIT_', 'LANEFATHER_', 'STRAYLIGHT_')) and key != 'PYTHONPATH'}
        self.git('init', '-q', '-b', 'codex/runtime', str(self.source.parent))
        self.git('config', 'user.email', 'runtime@example.invalid')
        self.git('config', 'user.name', 'Runtime test')
        self.git('add', '.', '../common')
        self.git('commit', '-qm', 'runtime source')
        self.head = self.git('rev-parse', 'HEAD').strip()
        self.install()
        self.command = self.tmp / 'bin' / 'lanefather'
        self.release = (self.tmp / 'share' / 'current').resolve()

    def git(self, *args: str) -> str:
        result = subprocess.run(['git', '-C', str(self.source), *args], capture_output=True, text=True, env=self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def install(self):
        result = subprocess.run([sys.executable, str(self.source / 'install.py'), '--bin', str(self.tmp / 'bin'),
                                 '--share', str(self.tmp / 'share')], capture_output=True, text=True, env=self.env)
        self.assertEqual(result.returncode, 0, result.stderr)

    def capabilities(self):
        return subprocess.run([str(self.command), 'capabilities', '--json'], cwd=self.tmp,
                              capture_output=True, text=True, env=self.env)

    def test_installed_capabilities_certify_the_committed_payload_and_canonical_hook(self):
        result = self.capabilities()
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['v'], 1)
        self.assertEqual(report['state_schema'], 1)
        self.assertEqual(report['slotfather_query_versions'], [1, 2])
        self.assertTrue({'project.readiness', 'project.update_queue', 'project-hook', 'lane.load_advisory'}
                        <= set(report['capabilities']))
        self.assertEqual(report['source']['commit'], self.head)
        self.assertTrue(report['source']['installed'])
        self.assertTrue(report['source']['verified'])
        self.assertEqual(len(report['source']['sha256']), 64)
        self.assertEqual((self.release / 'hooks/reference-transaction').read_bytes(),
                         (self.source / 'hooks/reference-transaction').read_bytes())
        self.assertFalse(list(self.release.rglob('__pycache__')))

    def test_reinstall_reuses_identical_release_bytes_and_refuses_tampered_runtime(self):
        package = self.release / 'lanefather' / 'cli.py'
        identity = (package.stat().st_ino, package.read_bytes())
        self.install()
        self.assertEqual((package.stat().st_ino, package.read_bytes()), identity)
        for relative in ['lanefather/cli.py', 'hooks/reference-transaction', 'fathers_common/cli.py']:
            with self.subTest(relative=relative):
                path = self.release / relative
                before = path.read_bytes()
                path.write_bytes(before + b'\n# tampered\n')
                result = self.capabilities()
                self.assertEqual(result.returncode, 1)
                self.assertIn('installed payload', result.stderr)
                path.write_bytes(before)

    def test_unknown_or_missing_installed_manifest_is_refused(self):
        manifest = self.release / 'lanefather' / 'INSTALL.json'
        before = manifest.read_bytes()
        data = json.loads(before)
        data['v'] = 99
        manifest.write_text(json.dumps(data))
        self.assertEqual(self.capabilities().returncode, 1)
        manifest.unlink()
        self.assertEqual(self.capabilities().returncode, 1)
        manifest.write_bytes(before)

    def test_only_configured_project_hooks_run_in_the_installed_interpreter_from_any_cwd(self):
        project = self.tmp / 'project'
        project.mkdir()
        script = project / 'policy.py'
        (project / 'policy_support.py').write_text('VALUE = 42\n')
        script.write_text("import json,sys,lanefather,policy_support\n"
                          "print(json.dumps({'root': str(LANEFATHER_PROJECT.config.path.parent), "
                          "'args': sys.argv[1:], 'package': lanefather.__file__, 'support': policy_support.VALUE}))\n"
                          "raise SystemExit(7)\n")
        configuration = project / '.lanefather.toml'
        configuration.write_text('project = "fixture"\n[hooks.project]\npolicy = "policy.py"\n')
        command = [str(self.command), '--config', str(configuration), 'project-hook']
        run = subprocess.run([*command, 'policy', '--', '--ref', 'abc def'], cwd=self.tmp,
                             env=self.env, capture_output=True, text=True)
        self.assertEqual(run.returncode, 7, run.stderr)
        report = json.loads(run.stdout)
        self.assertEqual(report['root'], str(project.resolve()))
        self.assertEqual(report['args'], ['--ref', 'abc def'])
        self.assertEqual(report['support'], 42)
        self.assertTrue(Path(report['package']).resolve().is_relative_to(self.release))
        refused = subprocess.run([*command, str(script)], cwd=self.tmp, env=self.env, capture_output=True, text=True)
        self.assertEqual(refused.returncode, 1)
        self.assertIn('configured project hook', refused.stderr)

    def test_project_hook_restores_the_callers_working_directory_argv_and_import_path(self):
        import argparse

        from lanefather import config, runtime
        project = self.tmp / 'restoration'
        project.mkdir()
        (project / 'policy.py').write_text("import sys,os\nassert sys.path[0] == os.getcwd()\nraise SystemExit(7)\n")
        configuration = project / '.lanefather.toml'
        configuration.write_text('[hooks.project]\npolicy="policy.py"\n')
        previous = (Path.cwd(), sys.argv, sys.path[:])
        result = runtime.project_hook(
            argparse.Namespace(name='policy', arguments=['--', 'value']),
            config.load(configuration),
        )
        self.assertEqual(result, 7)
        self.assertEqual(Path.cwd(), previous[0])
        self.assertIs(sys.argv, previous[1])
        self.assertEqual(sys.path, previous[2])


class FreeDiskMinimumTests(unittest.TestCase):
    """Projects read Lanefather's free-disk minimum from capabilities and in their hooks, not from host.py
    (fathers#46)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='lanefather-minimum-'))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_capabilities_report_the_free_disk_minimum(self):
        import argparse
        import contextlib
        import io
        from unittest.mock import patch

        from lanefather import host, runtime
        out = io.StringIO()
        # A state directory and machine settings of its own: no lane of the machine's sets the lane peak, and no
        # saved host.min_disk_gb the floor.
        isolated = {'LANEFATHER_STATE': str(self.tmp / 'state'), 'LANEFATHER_SETTINGS': str(self.tmp / 'settings.json')}
        with contextlib.redirect_stdout(out), patch.dict(os.environ, isolated):
            self.assertEqual(runtime.capabilities(argparse.Namespace(json=True)), 0)
            expected = round(runtime.minimum_gb(), 1)
        report = json.loads(out.getvalue())
        self.assertEqual(report['host'], {'min_disk_gb': expected})
        self.assertGreaterEqual(report['host']['min_disk_gb'], host.DISK_RESERVE_GB)
        self.assertIn('host.min_disk_gb', report['capabilities'])
        self.assertIn('project.policy', report['capabilities'])
        self.assertEqual(report['policy'], {
            'promoted': ['dev', 'main'],
            'worktrees_root': '.claude/worktrees',
            'lane_targets': [],
            'target_label': 'Cargo target',
            'cache_partitions': None,
            'cache_depfiles': ['debug/*.d', 'release/*.d'],
            'toolchain': False,
            'toolchain_name': 'Rust toolchain',
            'min_slots': {},
        })

    def test_capabilities_report_configured_project_policy(self):
        import argparse
        import contextlib
        import io
        from unittest.mock import patch

        from lanefather import config, runtime
        cfg_file = self.tmp / '.lanefather.toml'
        cfg_file.write_text(
            '[checks]\ntoolchain = ["python3", "tools/tc.py"]\ntoolchain_pin = "tc.toml"\n'
            'toolchain_name = "Node toolchain"\n'
            '[checks.min_slots]\nbrowser = 2\n'
            '[lane]\nworktrees_root = ".worktrees"\ntargets = ["{id}-build"]\n'
            'target_label = "Build dir"\ncache_partitions = "shared"\ncache_depfiles = ["*.d"]\n'
            '[promote]\nbranches = ["main"]\n'
        )
        isolated = {'LANEFATHER_STATE': str(self.tmp / 'state'), 'LANEFATHER_SETTINGS': str(self.tmp / 'settings.json')}
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch.dict(os.environ, isolated):
            self.assertEqual(runtime.capabilities(argparse.Namespace(json=True), config.load(cfg_file)), 0)
        report = json.loads(out.getvalue())
        self.assertEqual(report['policy'], {
            'promoted': ['main'],
            'worktrees_root': '.worktrees',
            'lane_targets': ['{id}-build'],
            'target_label': 'Build dir',
            'cache_partitions': 'shared',
            'cache_depfiles': ['*.d'],
            'toolchain': True,
            'toolchain_name': 'Node toolchain',
            'min_slots': {'browser': 2},
        })

    def test_a_project_hook_reads_the_minimum_next_to_disk_free_gb(self):
        import argparse
        from unittest.mock import patch

        from lanefather import config, runtime
        project = self.tmp / 'project'
        project.mkdir()
        seen = self.tmp / 'seen.json'
        (project / 'policy.py').write_text(
            "import json, os, pathlib\n"
            "names = ('LANEFATHER_MIN_DISK_GB', 'STRAYLIGHT_LANE_MIN_DISK_GB', 'STRAYLIGHT_LANE_DISK_FREE_GB')\n"
            f"pathlib.Path({str(seen)!r}).write_text(json.dumps({{n: os.environ.get(n) for n in names}}))\n")
        configuration = project / '.lanefather.toml'
        configuration.write_text('legacy_env_prefix = "STRAYLIGHT_LANE_"\n'
                                 'shared_env = ["DISK_FREE_GB"]\n[hooks.project]\npolicy = "policy.py"\n')
        clean = {k: v for k, v in os.environ.items() if not k.endswith(('MIN_DISK_GB', 'DISK_FREE_GB'))}
        with patch.dict(os.environ, {**clean, 'STRAYLIGHT_LANE_DISK_FREE_GB': '500',
                                     'LANEFATHER_STATE': str(self.tmp / 'state'),
                                     'LANEFATHER_SETTINGS': str(self.tmp / 'settings.json')}, clear=True):
            loaded = config.load(configuration)
            self.assertEqual(runtime.project_hook(argparse.Namespace(name='policy', arguments=[]), loaded), 0)
            # fathers#157: the floor derived for the project, as start applies it.
            floor = f"{runtime.minimum_gb(loaded):g}"
            self.assertNotIn('LANEFATHER_MIN_DISK_GB', os.environ, 'the hook\'s environment is restored')
            self.assertNotIn('STRAYLIGHT_LANE_MIN_DISK_GB', os.environ)
        self.assertEqual(json.loads(seen.read_text()), {'LANEFATHER_MIN_DISK_GB': floor,
                                                        'STRAYLIGHT_LANE_MIN_DISK_GB': floor,
                                                        'STRAYLIGHT_LANE_DISK_FREE_GB': '500'})
