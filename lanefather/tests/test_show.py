"""Read-only lane context for resource dashboards."""
from __future__ import annotations

import json
import unittest

from support import RUST_STUB, LaneCase


class ShowTests(LaneCase):
    def test_show_exports_project_ticket_checks_and_review_without_mutation(self):
        worktree = self.created('context', 'lib/**')
        self.git(self.repo, 'remote', 'add', 'origin', 'git@github.com:example/project.with-dots.git')
        self.commit(worktree, {'lib/new.txt': 'context\n'})
        self.lane('check', 'context')
        metadata = json.loads(self.verify_log.with_name(self.verify_log.name + '.meta').read_text().splitlines()[0])
        self.assertEqual(str(self.state.resolve()), metadata['lanefather.state'])
        self.assertEqual('context', metadata['lane'])
        before = {str(p): p.read_bytes() for p in self.state.rglob('*') if p.is_file()}
        shown = json.loads(self.lane('show', 'context', '--json', '--state-dir', str(self.state)))
        self.assertEqual(1, shown['version'])
        self.assertEqual('context', shown['id'])
        self.assertEqual('active', shown['status'])
        by_project = json.loads(self.lane('show', 'context', '--json', '--project-dir', str(worktree)))
        self.assertEqual(shown, by_project)
        self.assertEqual('lane/context', shown['branch'])
        self.assertEqual('integrate/q', shown['base'])
        self.assertEqual(['lib/**'], shown['claims'])
        self.assertEqual('https://github.com/example/project.with-dots/issues/404', shown['ticket']['url'])
        self.assertEqual({'status': 'green', 'current': None, 'tree_match': True}, shown['checks'])
        self.assertEqual('not started', shown['review']['status'])
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.state.rglob('*') if p.is_file()})
        self.commit(worktree, {'lib/new.txt': 'changed\n'})
        stale = json.loads(self.lane('show', 'context', '--json'))
        self.assertFalse(stale['checks']['current'])
        self.assertIn('context', self.lane('show', 'context'))

    def test_matching_tree_cannot_prove_current_toolchain_and_does_not_run_resolver(self):
        worktree = self.created('toolchain', 'lib/**')
        marker = self.tmp / 'resolver-ran'
        resolver = f'from pathlib import Path\nPath({str(marker)!r}).touch()\n' + RUST_STUB
        self.write(self.repo, 'tools/rust-toolchain.py', resolver)
        self.commit(worktree, {'lib/new.txt': 'context\n', 'tools/rust-toolchain.py': resolver,
                               'rust-toolchain.toml': '[toolchain]\nchannel="1.98.1"\n'})
        self.lane('check', 'toolchain')
        receipt = json.loads((self.state / 'toolchain.receipt.json').read_text())
        self.assertIsNotNone(receipt['toolchain'])
        self.assertTrue(marker.exists())
        marker.unlink()
        self.env['FIXTURE_RUST'] = '9.9.9'
        before = {str(p): p.read_bytes() for p in self.state.rglob('*') if p.is_file()}
        shown = json.loads(self.lane('show', 'toolchain', '--json'))
        self.assertIsNone(shown['checks']['current'])
        self.assertTrue(shown['checks']['tree_match'])
        self.assertFalse(marker.exists(), 'read-only dashboard never executes the project resolver')
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.state.rglob('*') if p.is_file()})

    def test_missing_state_and_invalid_ids_do_not_create_files(self):
        missing = self.tmp / 'missing-state'
        self.assertIn('no lane', self.lane('show', 'missing', '--json', '--state-dir', str(missing), ok=False))
        self.assertFalse(missing.exists())
        self.assertIn('lane id', self.lane('show', '../outside', '--json', ok=False))

    def test_project_lookup_refuses_a_lane_from_another_repository(self):
        self.created('same-id', 'lib/**')
        other = self.tmp / 'another-repository'
        other.mkdir()
        self.git(other, 'init', '-q')
        self.write(other, '.lanefather.toml', 'project = "Other"\n')
        output = self.lane('show', 'same-id', '--json', '--state-dir', str(self.state),
                           '--project-dir', str(other), ok=False)
        self.assertIn('another repository', output)


if __name__ == '__main__':
    unittest.main()
