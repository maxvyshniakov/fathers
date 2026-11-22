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

    def project(self, name: str) -> Project:
        directory = self.root / name
        path = directory / 'queue' / 'queue.toml'
        path.parent.mkdir(parents=True)
        path.write_text(f'[[lane]]\nid = "{name}"\nticket = "none"\nclaims = ["src/**"]\n')
        return Project(config.Config(project=name, state_dir=str(directory / 'state'),
                                     legacy_env_prefix=f'{name.upper()}_'))

    def test_readiness_uses_each_projects_configuration_without_creating_state_or_changing_ambient_config(self) -> None:
        alpha, beta = self.project('alpha'), self.project('beta')
        first, second = alpha.readiness(), beta.readiness()
        assert first.ready is not None and second.ready is not None
        self.assertEqual(first.ready['id'], 'alpha')
        self.assertEqual(second.ready['id'], 'beta')
        self.assertEqual((first.cap, second.cap), (4, 2))
        self.assertEqual(first.records, ())
        self.assertEqual(first.planned_or_active, ())
        self.assertFalse(first.state_dir.exists())
        self.assertFalse(second.state_dir.exists())

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
            self.assertEqual(worktrees.cache_root(alpha.config), first)
            self.assertEqual(worktrees.cache_root(beta.config), second)
            partition = first / 'cargo-v2' / ('a' * 64)
            (partition / 'debug').mkdir(parents=True)
            (partition / 'debug/core.d').write_text('/checkout/src/lib.rs: source\n')
            self.assertEqual(worktrees.shared_partitions('/checkout', project=alpha.config), [partition])
            self.assertEqual(worktrees.shared_partitions('/checkout', project=beta.config), [])
            self.assertFalse(second.exists())

    def test_multi_store_and_multi_project_isolation_in_one_process(self) -> None:
        import subprocess
        from typing import Any

        from lanefather import plan, results, state

        # Construct two independent git repositories and projects in the same process with distinct verify,
        # plan_module, gate_only, min_slots, and directory claim topologies.
        repo_a, repo_b = self.root / 'repo-a', self.root / 'repo-b'
        (repo_a / 'pkg_a').mkdir(parents=True)
        (repo_a / 'pkg_a' / 'file.txt').write_text('x\n')
        repo_b.mkdir(parents=True)
        (repo_b / 'pkg_a').write_text('file-at-head\n')
        for repo in (repo_a, repo_b):
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            subprocess.run(['git', '-C', str(repo), 'config', 'user.email', 't@example.com'], check=True)
            subprocess.run(['git', '-C', str(repo), 'config', 'user.name', 'T'], check=True)
            subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
            subprocess.run(['git', '-C', str(repo), 'commit', '-qm', 'init'], check=True)

        (repo_a / '.lanefather.toml').write_text('project = "alpha"\n')
        (repo_b / '.lanefather.toml').write_text('project = "beta"\n')

        dir_a, dir_b = self.root / 'alpha', self.root / 'beta'
        (dir_a / 'queue').mkdir(parents=True)
        (dir_b / 'queue').mkdir(parents=True)
        # In repo_a, `pkg_a` is a directory, so `claims = ["pkg_a"]` overlaps active lane `pkg_a/sub.txt`.
        # In repo_b, `pkg_a` is NOT a directory, so `claims = ["pkg_a"]` is a literal file and does NOT overlap `pkg_a/sub.txt`.
        (dir_a / 'queue' / 'queue.toml').write_text('[[lane]]\nid = "next-a"\nticket = "none"\nclaims = ["pkg_a"]\n')
        (dir_b / 'queue' / 'queue.toml').write_text('[[lane]]\nid = "next-b"\nticket = "none"\nclaims = ["pkg_a"]\n')

        cfg_a = config.Config(
            project='alpha',
            path=repo_a / '.lanefather.toml',
            state_dir=str(dir_a / 'state'),
            legacy_env_prefix='ALPHA_',
            verify='verify-alpha.sh',
            plan_module='tools/plan_a.py',
            gate_only=('alpha-only-*',),
            min_slots=(('alpha-*', 3),),
        )
        cfg_b = config.Config(
            project='beta',
            path=repo_b / '.lanefather.toml',
            state_dir=str(dir_b / 'state'),
            legacy_env_prefix='BETA_',
            verify='verify-beta.sh',
            plan_module='tools/plan_b.py',
            gate_only=('beta-only-*',),
            min_slots=(('beta-*', 4),),
        )
        store_a = state.Store(cfg_a)
        store_b = state.Store(cfg_b)
        store_a.save({'id': 'act-a', 'status': 'active', 'ticket': '1', 'claims': ['pkg_a/sub.txt'], 'after': []})
        store_b.save({'id': 'act-b', 'status': 'active', 'ticket': '2', 'claims': ['pkg_a/sub.txt'], 'after': []})

        # 1. Project.readiness() evaluates claims against each project's own repository root (directory vs file).
        readiness_a = Project(cfg_a).readiness()
        readiness_b = Project(cfg_b).readiness()
        self.assertIsNone(readiness_a.ready, 'pkg_a is a directory in repo_a, so it overlaps pkg_a/sub.txt')
        self.assertIsNotNone(readiness_b.ready, 'pkg_a is not a directory in repo_b, so it does not overlap pkg_a/sub.txt')

        # 2. Results recorded in store_a never bleed into store_b.
        tree_sha = 'a' * 40
        results.record(store_a, tree_sha, {'alpha-check': 'passed'}, source='check')
        self.assertTrue(results.lookup(store_a, tree_sha, ['alpha-check'])['verified'])
        self.assertFalse(results.lookup(store_b, tree_sha, ['alpha-check'])['verified'])

        # 3. plan.smoke_reuse and required_min_slots use each Store/Config's own gate_only and min_slots.
        smoke_rows: list[dict[str, Any]] = [
            {'id': 'alpha-only-1', 'suite': 's', 'paths': []},
            {'id': 'beta-only-1', 'suite': 's', 'paths': []},
        ]
        _, kept_a, _, _ = plan.smoke_reuse(store_a, 'smoke', smoke_rows, tree_sha, None)
        _, kept_b, _, _ = plan.smoke_reuse(store_b, 'smoke', smoke_rows, tree_sha, None)
        self.assertEqual([r['id'] for r in kept_a or []], ['beta-only-1'])
        self.assertEqual([r['id'] for r in kept_b or []], ['alpha-only-1'])
        sample_plan: dict[str, Any] = {
            'selection': [
                {'id': 'alpha-1', 'suite': 's', 'paths': []},
                {'id': 'beta-1', 'suite': 's', 'paths': []},
            ]
        }
        self.assertEqual(plan.required_min_slots(sample_plan, {'s': ['alpha-1']}, ['s'], project=cfg_a), (3, ['alpha-1']))
        self.assertEqual(plan.required_min_slots(sample_plan, {'s': ['alpha-1']}, ['s'], project=cfg_b), (1, []))
        self.assertEqual(plan.required_min_slots(sample_plan, {'s': ['beta-1']}, ['s'], project=cfg_b), (4, ['beta-1']))

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
