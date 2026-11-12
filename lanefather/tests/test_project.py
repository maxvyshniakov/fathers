"""Configured project operations, observed through real queue/state files and independent projects."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lanefather import config
from lanefather.errors import LaneError
from lanefather.project import Project, QueueUpdate, Readiness


class ProjectTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix='lanefather-project-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(('LANEFATHER_', 'ALPHA_', 'BETA_'))}
        environment.update(ALPHA_CAP='4', ALPHA_LOAD15='0', ALPHA_DISK_FREE_GB='500',
                           BETA_CAP='2', BETA_LOAD15='0', BETA_DISK_FREE_GB='500')
        variables = patch.dict(os.environ, environment, clear=True)
        variables.start()
        self.addCleanup(variables.stop)
        self.addCleanup(config.use, None)

    def project(self, name: str) -> Project:
        directory = self.root / name
        path = directory / 'queue' / 'queue.toml'
        path.parent.mkdir(parents=True)
        path.write_text(f'[[lane]]\nid = "{name}"\nticket = "none"\nclaims = ["src/**"]\n')
        return Project(config.Config(project=name, state_dir=str(directory / 'state'),
                                     legacy_env_prefix=f'{name.upper()}_'))

    def test_readiness_uses_each_projects_configuration_without_creating_state_or_changing_ambient_config(self) -> None:
        alpha, beta = self.project('alpha'), self.project('beta')
        config.use(beta.config)
        first, second = alpha.readiness(), beta.readiness()
        assert first.ready is not None and second.ready is not None
        self.assertEqual(first.ready['id'], 'alpha')
        self.assertEqual(second.ready['id'], 'beta')
        self.assertEqual((first.cap, second.cap), (4, 2))
        self.assertEqual(first.records, ())
        self.assertEqual(first.planned_or_active, ())
        self.assertFalse(first.state_dir.exists())
        self.assertFalse(second.state_dir.exists())
        self.assertIs(config.current(), beta.config)

    def test_readiness_names_planned_and_active_records_without_an_ambiguous_alias(self) -> None:
        project = self.project('alpha')
        project.paths.state_dir.mkdir()
        for status in ('planned', 'active', 'done'):
            (project.paths.state_dir / f'{status}.json').write_text(json.dumps({'id': status, 'status': status, 'claims': []}))
        snapshot = project.readiness()
        self.assertEqual({'planned', 'active'}, {record['id'] for record in snapshot.planned_or_active})
        self.assertFalse(hasattr(snapshot, 'active'))

    def test_configured_paths_are_available_before_a_queue_or_state_exists(self) -> None:
        directory = self.root / 'new-project'
        project = Project(config.Config(state_dir=str(directory / 'lanes')))
        self.assertEqual(project.paths.state_dir, directory / 'lanes')
        self.assertEqual(project.paths.queue_path, directory / 'queue' / 'queue.toml')
        self.assertFalse(directory.exists())
        with self.assertRaisesRegex(LaneError, "no queue at"):
            project.readiness()
        self.assertFalse(directory.exists())

    def test_cache_queries_use_explicit_projects_without_replacing_ambient_configuration(self) -> None:
        from dataclasses import replace

        from lanefather import worktrees
        alpha, beta = self.project('alpha'), self.project('beta')
        alpha = Project(replace(alpha.config, cache_partitions='cargo-v2'))
        beta = Project(replace(beta.config, cache_partitions='cargo-v2'))
        first, second = self.root / 'cache-alpha', self.root / 'cache-beta'
        with patch.dict(os.environ, ALPHA_BUILD_CACHE=str(first), BETA_BUILD_CACHE=str(second)):
            config.use(beta.config)
            self.assertEqual(worktrees.cache_root(alpha.config), first)
            self.assertEqual(worktrees.cache_root(beta.config), second)
            partition = first / 'cargo-v2' / ('a' * 64)
            (partition / 'debug').mkdir(parents=True)
            (partition / 'debug/core.d').write_text('/checkout/src/lib.rs: source\n')
            self.assertEqual(worktrees.shared_partitions('/checkout', project=alpha.config), [partition])
            self.assertEqual(worktrees.shared_partitions('/checkout', project=beta.config), [])
            self.assertFalse(second.exists())
            self.assertIs(config.current(), beta.config)

    def test_queue_policy_returns_a_proposal_while_the_project_owns_the_atomic_write_and_ledger(self) -> None:
        project = self.project('alpha')
        message = project.update_queue(lambda snapshot: QueueUpdate(
            '[[lane]]\nid = "first"\nticket = 648\nclaims = ["lib/**"]\n\n' + snapshot.text, 'queued first'))
        self.assertEqual(message, 'queued first')
        after = project.readiness()
        self.assertEqual([entry['id'] for entry in after.entries], ['first', 'alpha'])
        self.assertTrue((after.state_dir / 'ledger.log').read_text().endswith(' queued first\n'))
        self.assertEqual(list(after.queue_path.parent.glob('*.pending*')), [])

    def test_invalid_proposals_and_policy_exceptions_leave_the_queue_and_ledger_unchanged(self) -> None:
        project = self.project('alpha')
        before = project.readiness()
        for text in ['not toml', '[[lane]]\nid="broken"\n', before.text + before.text]:
            with self.assertRaises(LaneError):
                project.update_queue(lambda snapshot, proposal=text: QueueUpdate(proposal, 'must not be logged'))
            self.assertEqual(before.queue_path.read_text(), before.text)
        def failed_policy(snapshot: Readiness) -> None:
            raise LookupError('registry unavailable')
        with self.assertRaisesRegex(LookupError, 'registry unavailable'):
            project.update_queue(failed_policy)
        self.assertEqual(before.queue_path.read_text(), before.text)
        self.assertFalse((before.state_dir / 'ledger.log').exists())

    def test_parallel_policy_updates_keep_both_entries(self) -> None:
        project = self.project('alpha')
        def append(ident: str) -> str | None:
            return project.update_queue(lambda snapshot: QueueUpdate(snapshot.text
                + f'\n[[lane]]\nid="{ident}"\nticket="none"\nclaims=["{ident}/**"]\n', f'queued {ident}'))
        with ThreadPoolExecutor(max_workers=2) as workers:
            messages = list(workers.map(append, ['first', 'second']))
        self.assertEqual(set(messages), {'queued first', 'queued second'})
        self.assertEqual({entry['id'] for entry in project.readiness().entries}, {'alpha', 'first', 'second'})
        self.assertEqual(len((project.readiness().state_dir / 'ledger.log').read_text().splitlines()), 2)

    def test_unreadable_ownership_records_refuse_readiness_and_mutation(self) -> None:
        project = self.project('alpha')
        snapshot = project.readiness()
        snapshot.state_dir.mkdir()
        record = snapshot.state_dir / 'active.json'
        record.write_text('{broken')
        before = snapshot.queue_path.read_bytes()
        with self.assertRaisesRegex(LaneError, 'unreadable'):
            project.readiness()
        with self.assertRaisesRegex(LaneError, 'unreadable'):
            project.update_queue(lambda snapshot: QueueUpdate(''))
        self.assertEqual(snapshot.queue_path.read_bytes(), before)
