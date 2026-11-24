"""Versioned capabilities and installed payload identity for project launchers."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import sys
from typing import Any, cast

from . import __version__, config, events
from .errors import LaneError
from .project import Project

MANIFEST = 'INSTALL.json'
CAPABILITIES = ('project.readiness', 'project.update_queue', 'project-hook', 'canonical-reference-transaction',
                'promote.step-leases', 'tickets.lifecycle', 'slots.priority', 'dashboard.events',
                'dashboard.status', 'dashboard.settings', 'host.min_disk_gb', 'lane.drop',
                'post_merge.cleared', 'lane.wait', 'review.fix_of', 'lane.load_advisory', 'project.policy',
                'checks.min_slots.default')
# The dashboard contract versions Godfather reads (its SPEC.md, "Capabilities contract").
DASHBOARD = 1


def shipping_files(root: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for directory in ('lanefather', 'hooks', 'fathers_common'):
        for path in sorted((root / directory).rglob('*')):
            if not path.is_file() or '__pycache__' in path.parts or path.suffix == '.pyc':
                continue
            if path.parent == root / 'lanefather' and path.name in ('COMMIT', MANIFEST):
                continue
            files[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def payload_digest(files: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(files, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def installed_identity(root: Path) -> dict[str, Any]:
    package = root / 'lanefather'
    manifest_path = package / MANIFEST
    if not manifest_path.exists() and not (package / 'COMMIT').exists():
        return {'installed': False, 'verified': False, 'commit': None, 'sha256': None}
    try:
        data: Any = json.loads(manifest_path.read_text())
        if not isinstance(data, dict):
            raise ValueError('manifest must be an object')
        manifest = cast(dict[str, Any], data)
        if type(manifest.get('v')) is not int or manifest['v'] != 1:
            raise ValueError('unknown manifest version')
        commit = manifest.get('commit')
        if not isinstance(commit, str) or not re.fullmatch('[0-9a-f]{40}', commit):
            raise ValueError('invalid source commit')
        if (package / 'COMMIT').read_text().strip() != commit:
            raise ValueError('source commit differs')
        actual = shipping_files(root)
        if manifest.get('files') != actual or manifest.get('sha256') != payload_digest(actual):
            raise ValueError('file digest differs')
        return {'installed': True, 'verified': True, 'commit': commit, 'sha256': payload_digest(actual)}
    except (OSError, ValueError) as error:
        raise LaneError(f'installed payload cannot be verified: {error}; reinstall committed source') from None


def dashboard() -> dict[str, Any]:
    """What a dashboard reads from Lanefather: its EVENTS tag, the event log, the status snapshot, settings."""
    from . import settings, status  # the snapshot module reads every other module; install.py imports only this one
    return {'v': DASHBOARD, 'tag': events.TAG,
            'events': {'path': str(events.path().absolute()), 'versions': [events.SCHEMA]},
            'snapshot': {'argv': ['status', '--json'], 'versions': [status.VERSION], 'poll': status.POLL},
            'settings': {'versions': [settings.VERSION]}}


def minimum_gb(project: config.Config | None = None) -> float:
    """The free-disk floor start applies in the project the command runs in (fathers#157), else to a project
    without lanes on this machine."""
    from . import admission  # admission reads the slot and state modules install.py does not import
    return admission.floor_here(project).gb


def policy_summary(project: config.Config | None = None) -> dict[str, Any]:
    """Effective project policy values reported by `capabilities --json` (fathers#131, fathers#247)."""
    cfg = project or config.DEFAULT
    return {
        'promoted': list(cfg.promoted),
        'worktrees_root': cfg.worktrees_root,
        'lane_targets': list(cfg.lane_targets),
        'target_label': cfg.target_label,
        'cache_partitions': cfg.cache_partitions or None,
        'cache_depfiles': list(cfg.cache_depfiles),
        'toolchain': bool(cfg.toolchain and cfg.toolchain_pin),
        'toolchain_name': cfg.toolchain_name,
        'min_slots': dict(cfg.min_slots),
    }


def capabilities(args: argparse.Namespace, project: config.Config | None = None) -> int:
    report = {'v': 1, 'tool': 'lanefather', 'version': __version__, 'state_schema': 1,
              'slotfather_query_versions': [1, 2], 'capabilities': CAPABILITIES, 'dashboard': dashboard(),
              # The free disk under which start refuses a lane here, in decimal GB (fathers#46, #157).
              'host': {'min_disk_gb': round(minimum_gb(project), 1)},
              'policy': policy_summary(project),
              'source': installed_identity(Path(__file__).resolve().parent.parent)}
    print(json.dumps(report, sort_keys=True) if args.json else json.dumps(report, indent=2, sort_keys=True))
    return 0


def project_hook(args: argparse.Namespace, cfg: config.Config) -> int:
    project = Project(cfg)
    script = dict(project.config.project_hooks).get(args.name)
    if not script or project.config.path is None:
        raise LaneError(f"no configured project hook {args.name!r}; name it in [hooks.project]")
    root = project.config.path.parent
    path = (root / script).resolve()
    if not path.is_file():
        raise LaneError(f"configured project hook {args.name!r} has no script at {path}")
    previous_argv, previous_cwd, previous_path = sys.argv, Path.cwd(), sys.path[:]
    arguments = args.arguments[1:] if args.arguments[:1] == ['--'] else args.arguments
    # The free-disk minimum under each name of the shared setting, beside DISK_FREE_GB (fathers#46); Lanefather
    # sets it for the hook and reads it from nowhere.
    minimum = {name: os.environ.get(name) for name in project.config.names('MIN_DISK_GB')}
    try:
        os.environ.update(dict.fromkeys(minimum, f"{minimum_gb(project.config):g}"))
        sys.argv = [str(path), *arguments]
        os.chdir(root)
        sys.path.insert(0, str(path.parent))
        runpy.run_path(str(path), run_name='__main__', init_globals={'LANEFATHER_PROJECT': project})
        return 0
    except SystemExit as error:
        if error.code is None or isinstance(error.code, int):
            return error.code or 0
        print(str(error.code), file=sys.stderr)
        return 1
    finally:
        for name, value in minimum.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        sys.argv = previous_argv
        sys.path[:] = previous_path
        os.chdir(previous_cwd)
