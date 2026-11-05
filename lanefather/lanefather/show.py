"""Read-only, versioned lane summaries for external dashboards."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any

from . import config, state
from .errors import LaneError
from .gitops import main_checkout


def git_read(root: str, *args: str) -> str:
    try:
        result = subprocess.run(['git', '-C', root, *args], capture_output=True, text=True, timeout=2)
        return result.stdout.strip() if result.returncode == 0 else ''
    except (OSError, subprocess.TimeoutExpired):
        return ''


def summary(lane_id: str, directory: Path) -> dict[str, Any]:
    """Read records without creating a state directory, taking locks or running project hooks."""
    if not state.ID.fullmatch(lane_id):
        raise LaneError(f'lane id {lane_id!r}: use letters, digits, _ or -')
    record = state.read_json(directory / f'{lane_id}.json')
    if record is None:
        raise LaneError(f'no lane {lane_id}')
    record = state.migrate(record)
    if record.get('id') != lane_id:
        raise LaneError(f'lane id mismatch for {lane_id}')
    root = main_checkout(str(record.get('common_dir', '')))
    origin = git_read(root, 'remote', 'get-url', 'origin') if root else ''
    match = re.fullmatch(r'(?:git@github\.com:|https://github\.com/|ssh://git@github\.com/)'
                         r'([\w.-]+/[\w.-]+?)(?:\.git)?/?', origin)
    repository = f'https://github.com/{match[1]}' if match else None
    number = str(record.get('ticket', '')).lstrip('#')
    ticket = {'number': number, 'url': f'{repository}/issues/{number}' if repository and number.isdigit() else None}
    receipt = state.read_json(directory / f'{lane_id}.receipt.json')
    tree = git_read(root, 'rev-parse', '--verify', str(record.get('branch', '')) + '^{tree}') if root else ''
    checks = {'status': receipt.get('status', 'unknown') if receipt else 'not run',
              'current': receipt.get('tree') == tree if receipt and tree else None}
    review = state.read_json(directory / f'{lane_id}.review.json') or {}
    findings = review.get('findings', [])
    config_path = Path(root) / config.CONFIG_NAME
    cfg = config.load(config_path) if config_path.is_file() else config.Config()
    return {'version': 1, 'id': lane_id, 'project': cfg.project if cfg.project != 'this project' else Path(root).name,
            'repository': repository, 'ticket': ticket, 'status': record.get('status'),
            'branch': record.get('branch'), 'base': record.get('base'), 'worktree': record.get('worktree'),
            'claims': record.get('claims', []), 'after': record.get('after', []), 'checks': checks,
            'review': {'status': review.get('status', 'not started'), 'findings': len(findings),
                       'unresolved': sum(1 for finding in findings if not finding.get('mark'))}}


def show(args: Any) -> int:
    cfg = config.current()
    common = ''
    if args.project_dir:
        common = git_read(args.project_dir, 'rev-parse', '--path-format=absolute', '--git-common-dir')
        if not common:
            raise LaneError('project directory is not a Git repository')
        project_root = git_read(args.project_dir, 'rev-parse', '--show-toplevel')
        path = Path(project_root) / config.CONFIG_NAME
        if not path.is_file():
            path = Path(main_checkout(common)) / config.CONFIG_NAME
        if not path.is_file():
            raise LaneError('project has no lanefather configuration')
        cfg = config.load(path)
    directory = Path(os.path.expanduser(args.state_dir or cfg.env('STATE') or cfg.state_dir))
    result = summary(args.id, directory)
    if common:
        record = state.read_json(directory / f'{args.id}.json') or {}
        if Path(str(record.get('common_dir', ''))).resolve() != Path(common).resolve():
            raise LaneError('lane belongs to another repository')
    if args.json:
        print(json.dumps(result))
    else:
        print(f"Lane {result['id']} · {result['status']}")
        for key in ('project', 'ticket', 'branch', 'base', 'checks', 'review', 'claims', 'after', 'worktree'):
            print(f'{key}: {result[key]}')
    return 0
