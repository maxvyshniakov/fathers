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
        self.source = self.tmp / 'source'
        shutil.copytree(ROOT / 'lanefather', self.source / 'lanefather',
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        shutil.copytree(ROOT / 'hooks', self.source / 'hooks')
        shutil.copy2(ROOT / 'install.py', self.source / 'install.py')
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(('GIT_', 'LANEFATHER_', 'STRAYLIGHT_')) and key != 'PYTHONPATH'}
        self.git('init', '-q', '-b', 'codex/runtime')
        self.git('config', 'user.email', 'runtime@example.invalid')
        self.git('config', 'user.name', 'Runtime test')
        self.git('add', '.')
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
        self.assertTrue({'project.readiness', 'project.update_queue', 'project-hook'} <= set(report['capabilities']))
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
        for relative in ['lanefather/cli.py', 'hooks/reference-transaction']:
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
