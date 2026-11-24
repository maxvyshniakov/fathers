"""Read-only, versioned lane summaries for external dashboards."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
from typing import Any

from . import admission, config, receipts, state
from .errors import LaneError, LaneNotFound
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
    path = directory / f'{lane_id}.json'
    record = state.read_json(path)
    if record is None:
        if path.exists():
            raise LaneError(state.cut_short(path))
        raise LaneNotFound(f'no lane {lane_id}')
    record = state.migrate(record)
    if record.get('id') != lane_id:
        raise LaneError(f'lane id mismatch for {lane_id}')
    root = main_checkout(str(record.get('common_dir', '')))
    config_path = Path(root) / config.CONFIG_NAME if root else None
    cfg = config.load(config_path) if config_path and config_path.is_file() else config.Config()
    store = state.Store(cfg, directory=directory)
    # fathers#237: a lane whose branch and worktree are gone says so.
    return dict(receipts.lane_view(store, record).for_show(), gone=admission.gone(record))


def show(
    args: Any,
    project: config.Config | None = None,
    config_error: LaneError | None = None,
) -> int:
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
    else:
        if not args.state_dir and config_error is not None:
            raise config_error
        cfg = project or config.DEFAULT
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
        if result['gone']:
            print(f'gone: {admission.GONE}; {cfg.command} drop {result["id"]} frees it')
    return 0
