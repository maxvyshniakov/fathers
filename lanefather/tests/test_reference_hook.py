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

    def test_pack_refs_succeeds_silently_in_agent_sessions(self) -> None:
        for label, agent_env in (('claude', {'CLAUDECODE': '1'}), ('codex', {'CODEX_THREAD_ID': '1'})):
            with self.subTest(agent=label):
                # Restore loose refs for both main and dev outside an agent session without dropping HEAD's branch.
                self.env.pop('CLAUDECODE', None)
                self.env.pop('CODEX_THREAD_ID', None)
                packed_refs = self.repo / '.git' / 'packed-refs'
                if packed_refs.exists():
                    kept = [
                        line for line in packed_refs.read_text().splitlines()
                        if not line.endswith((' refs/heads/main', ' refs/heads/dev'))
                    ]
                    packed_refs.write_text('\n'.join(kept) + '\n')
                (self.repo / '.git' / 'refs' / 'heads' / 'main').write_text(self.before + '\n')
                (self.repo / '.git' / 'refs' / 'heads' / 'dev').write_text(self.before + '\n')
                self.env.update(agent_env)

                result = self.git('pack-refs', '--all')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, '')
                self.assertEqual(result.stderr, '')
                self.assertFalse((self.repo / '.git' / 'refs' / 'heads' / 'main').exists())
                self.assertFalse((self.repo / '.git' / 'refs' / 'heads' / 'dev').exists())
                packed_lines = packed_refs.read_text().splitlines()
                self.assertIn(f'{self.before} refs/heads/main', packed_lines)
                self.assertIn(f'{self.before} refs/heads/dev', packed_lines)
                self.assertEqual(self.git('rev-parse', 'refs/heads/main').stdout.strip(), self.before)
                self.assertEqual(self.git('rev-parse', 'refs/heads/dev').stdout.strip(), self.before)

                # Mixed state: main already packed + loose, dev loose only, pruned from a linked worktree.
                (self.repo / '.git' / 'refs' / 'heads' / 'main').write_text(self.before + '\n')
                packed_refs.write_text(
                    '\n'.join(line for line in packed_lines if not line.endswith(' refs/heads/dev')) + '\n'
                )
                (self.repo / '.git' / 'refs' / 'heads' / 'dev').write_text(self.before + '\n')
                wt = self.repo / f'wt-{label}'
                self.assertEqual(self.git('worktree', 'add', '-q', '-b', f'lane/wt-{label}', str(wt)).returncode, 0)
                wt_pack = subprocess.run(
                    ['git', '-C', str(wt), 'pack-refs', '--all', '--prune'],
                    env=self.env, text=True, capture_output=True, check=False,
                )
                self.assertEqual(wt_pack.returncode, 0, wt_pack.stderr)
                self.assertEqual(wt_pack.stdout, '')
                self.assertEqual(wt_pack.stderr, '')
                self.assertFalse((self.repo / '.git' / 'refs' / 'heads' / 'main').exists())
                self.assertFalse((self.repo / '.git' / 'refs' / 'heads' / 'dev').exists())
                self.assertEqual(self.git('rev-parse', 'refs/heads/main').stdout.strip(), self.before)
                self.assertEqual(self.git('rev-parse', 'refs/heads/dev').stdout.strip(), self.before)

    def test_deleting_protected_branch_is_refused_in_all_storage_states(self) -> None:
        packed_refs = self.repo / '.git' / 'packed-refs'
        loose_dev = self.repo / '.git' / 'refs' / 'heads' / 'dev'

        def prepare(state: str) -> str:
            self.env.pop('CLAUDECODE', None)
            if packed_refs.exists():
                packed_refs.unlink()
            loose_dev.write_text(self.before + '\n')
            if state == 'loose-only':
                expected = self.before
            elif state == 'packed-only':
                self.assertEqual(self.git('pack-refs', '--all').returncode, 0)
                self.assertFalse(loose_dev.exists())
                expected = self.before
            elif state == 'both-same-sha':
                self.assertEqual(self.git('pack-refs', '--all').returncode, 0)
                loose_dev.write_text(self.before + '\n')
                expected = self.before
            elif state == 'both-diff-sha':
                self.assertEqual(self.git('pack-refs', '--all').returncode, 0)
                loose_dev.write_text(self.tip + '\n')
                expected = self.tip
            else:
                raise AssertionError(state)
            self.env['CLAUDECODE'] = '1'
            return expected

        for state in ('loose-only', 'packed-only', 'both-same-sha', 'both-diff-sha'):
            for mode in ('branch -D', 'update-ref -d', 'update-ref -d <old>'):
                with self.subTest(state=state, mode=mode):
                    expected = prepare(state)
                    if mode == 'branch -D':
                        result = self.git('branch', '-D', 'dev')
                    elif mode == 'update-ref -d':
                        result = self.git('update-ref', '-d', 'refs/heads/dev')
                    else:
                        result = self.git('update-ref', '-d', 'refs/heads/dev', expected)
                    self.assertNotEqual(result.returncode, 0, f'{mode} unexpectedly succeeded in {state}')
                    self.assertIn('refusing to update refs/heads/dev', result.stderr)
                    self.assertEqual(self.git('rev-parse', '--verify', 'refs/heads/dev').stdout.strip(), expected)

    def test_creating_and_moving_protected_branch_are_still_refused(self) -> None:
        # Moving dev to a different SHA is refused.
        for cmd in (('branch', '-f', 'dev', self.tip), ('update-ref', 'refs/heads/dev', self.tip)):
            with self.subTest(cmd=cmd):
                result = self.git(*cmd)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('refusing to update refs/heads/dev', result.stderr)
                self.assertEqual(self.git('rev-parse', 'refs/heads/dev').stdout.strip(), self.before)

        # Same-SHA no-op passes without moving dev.
        noop = self.git('update-ref', 'refs/heads/dev', self.before)
        self.assertEqual(noop.returncode, 0, noop.stderr)
        self.assertEqual(self.git('rev-parse', 'refs/heads/dev').stdout.strip(), self.before)

        # Remove dev outside an agent session, then verify creation is refused (even with a decoy nested ref).
        self.env.pop('CLAUDECODE')
        self.assertEqual(self.git('branch', '-D', 'dev').returncode, 0)
        self.assertEqual(self.git('update-ref', 'refs/heads/refs/heads/dev', self.before).returncode, 0)
        self.env['CLAUDECODE'] = '1'
        for cmd in (
            ('checkout', '-b', 'dev', self.before),
            ('branch', 'dev', self.before),
            ('update-ref', 'refs/heads/dev', self.before),
        ):
            with self.subTest(create=cmd):
                result = self.git(*cmd)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('refusing to update refs/heads/dev', result.stderr)
                self.assertNotEqual(self.git('show-ref', '--verify', '-q', 'refs/heads/dev').returncode, 0)
