"""The canonical protected-ref hook through real Git transactions in a scratch repository."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parent.parent


class ReferenceHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix='lanefather-hook-')
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name)
        self.env = {key: value for key, value in os.environ.items() if not key.startswith('CODEX_')}
        self.env.pop('CLAUDECODE', None)
        self.env.pop('LANDING_TOKEN', None)
        self.git('init', '-q', '-b', 'dev')
        self.git('config', 'user.email', 'hook@example.invalid')
        self.git('config', 'user.name', 'Hook test')
        self.git('config', 'core.hooksPath', str(self.repo / 'hooks'))
        (self.repo / 'content').write_text('before\n')
        self.git('add', 'content')
        self.git('commit', '-qm', 'seed')
        self.before = self.git('rev-parse', 'HEAD').stdout.strip()
        self.git('branch', 'main')
        self.git('switch', '-qc', 'lane/test')
        (self.repo / 'content').write_text('after\n')
        self.git('commit', '-qam', 'candidate')
        self.tip = self.git('rev-parse', 'HEAD').stdout.strip()
        hooks = self.repo / 'hooks'
        hooks.mkdir()
        shutil.copy2(ROOT / 'hooks' / 'reference-transaction', hooks / 'reference-transaction')
        self.env['CLAUDECODE'] = '1'

    def git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(['git', '-C', str(self.repo), *args], env=self.env,
                              text=True, capture_output=True, check=False)

    def lock(self, acquired: int | None = None) -> None:
        (self.repo / '.git' / 'landing.lock').write_text(
            f'token expected\nholder hook-test\nacquired {acquired or int(time.time())}\n')

    def test_an_agent_is_refused_and_receives_generic_lanefather_instructions(self) -> None:
        result = self.git('update-ref', 'refs/heads/dev', self.tip)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('lanefather promote', result.stderr)
        self.assertIn('lanefather sync --drop-local', result.stderr)
        self.assertNotIn('Straylight', result.stderr)
        self.assertEqual(self.git('rev-parse', 'dev').stdout.strip(), self.before)
        self.assertEqual(self.git('update-ref', 'refs/remotes/origin/dev', self.tip).returncode, 0)

    def test_matching_token_allows_the_same_transaction_and_wrong_token_does_not(self) -> None:
        self.lock()
        self.env['LANDING_TOKEN'] = 'wrong'
        self.assertNotEqual(self.git('update-ref', 'refs/heads/dev', self.tip).returncode, 0)
        self.env['LANDING_TOKEN'] = 'expected'
        self.assertEqual(self.git('update-ref', 'refs/heads/dev', self.tip).returncode, 0)

    def test_expiration_and_the_historical_malformed_lock_behavior_are_preserved(self) -> None:
        self.lock(int(time.time()) - 7201)
        self.env['LANDING_TOKEN'] = 'expected'
        expired = self.git('update-ref', 'refs/heads/dev', self.tip)
        self.assertNotEqual(expired.returncode, 0)
        self.assertIn('expired after 2 h', expired.stderr)
        (self.repo / '.git' / 'landing.lock').write_text('malformed\n')
        malformed = self.git('update-ref', 'refs/heads/dev', self.tip)
        self.assertEqual(malformed.returncode, 0)
        self.assertIn('unreadable or malformed', malformed.stderr)

    def test_non_agent_transactions_do_not_require_a_landing_token(self) -> None:
        self.env.pop('CLAUDECODE')
        self.assertEqual(self.git('update-ref', 'refs/heads/main', self.tip).returncode, 0)
