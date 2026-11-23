#!/usr/bin/env python3
"""Install lanefather: the package into ~/.local/share/lanefather and the `lanefather` command into ~/.local/bin.

  python3 install.py [--python /path/to/python3] [--bin DIR] [--share DIR]
                     [--no-switch | --rollback | --uninstall]

The command runs on one named interpreter, the one running this installer (or --python, 3.11 or newer for tomllib),
so an agent inside a project's virtual environment still runs lanefather on the Python it was installed with.

Each install is a new directory, share/releases/<version>-<commit>, and share/current points at it; the command reads
`current` when it starts. A running lanefather keeps the release it started from (a background review re-runs that
release). Pruning keeps the three newest releases by version and build order, every release pinned by a live lane in
a registered state directory, and any release in use by a running process. `current` never moves backwards during a
normal install; use `--no-switch` to stage an older release without moving `current` or `--rollback` to point
`current` at the previous release. `--uninstall` removes the command, `current`, and unpinned releases while keeping
any release pinned by a live lane or in use. `lanefather --version` names the commit.
"""
from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any, cast

# Inspect and copy source without creating uncommitted interpreter cache files in it.
sys.dont_write_bytecode = True
SOURCE = Path(__file__).resolve().parent
COMMON = SOURCE.parent / "common" / "fathers_common"
if str(SOURCE.parent / "common") not in sys.path:
    sys.path.insert(0, str(SOURCE.parent / "common"))

from fathers_common.installer import (  # noqa: E402
    InstallError,
    clean_git_env,
    committed_head,
    copy_python_tree,
    install_lock,
    resolve_interpreter,
    stage_release,
    write_launcher,
)
from fathers_common.installer import (  # noqa: E402
    switch_symlink as _switch_symlink,
)
from lanefather import config, registry  # noqa: E402
from lanefather.errors import LaneError  # noqa: E402
from lanefather.runtime import MANIFEST, installed_identity, payload_digest, shipping_files  # noqa: E402
from lanefather.state import read_json  # noqa: E402

MIN_PYTHON = (3, 11)
KEEP_RELEASES = 3
HEX_COMMIT = re.compile(r"^[0-9a-f]{7,40}$")
PENDING_DIR = re.compile(r"^\.pending-(\d+)(?:-.*)?$")
VERSION_PREFIX = re.compile(r"^(\d+(?:\.\d+)*)(?:-|$)")
LAUNCHER = """#!{python}
# lanefather's command (install.py): the release share/current points at, on the interpreter it was installed for.
import sys
# An installed release stays immutable, including while a new CLI starts from it.
sys.dont_write_bytecode = True
sys.path.insert(0, {current!r})
from lanefather.cli import main
sys.exit(main())
"""


def interpreter(python: str | None) -> str:
    return resolve_interpreter(python, MIN_PYTHON, tool="lanefather", allow_venv=True, resolve_which=True)


def commit() -> str:
    return committed_head(
        SOURCE,
        ("lanefather", "hooks", "install.py", "CHANGELOG.md", str(COMMON)),
        unversioned_message="install from a committed Git checkout, not an unversioned source directory",
        dirty_message="uncommitted shipping inputs: commit and verify them before installing; "
                      "the installed release was not changed",
    )


def commit_meta(rev: str) -> tuple[int, int]:
    """Committer timestamp and history depth for `rev` in SOURCE; (0, 0) when unavailable."""
    environment = clean_git_env()
    shown = subprocess.run(["git", "show", "-s", "--format=%ct", rev], cwd=SOURCE,
                           capture_output=True, text=True, env=environment)
    commit_time = int(shown.stdout.strip()) if shown.returncode == 0 and shown.stdout.strip().isdigit() else 0
    counted = subprocess.run(["git", "rev-list", "--count", rev], cwd=SOURCE,
                             capture_output=True, text=True, env=environment)
    commit_count = int(counted.stdout.strip()) if counted.returncode == 0 and counted.stdout.strip().isdigit() else 0
    return commit_time, commit_count


def version() -> str:
    for line in (SOURCE / "lanefather" / "__init__.py").read_text().splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip('"')
    raise InstallError("lanefather/__init__.py names no __version__")


def write_atomic(path: Path, text: str, mode: int) -> None:
    write_launcher(path, text, mode=mode)


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def parse_version(name: str) -> tuple[int, ...]:
    match = VERSION_PREFIX.match(name)
    return tuple(int(part) for part in match[1].split(".")) if match else ()


def release_commit(release: Path) -> str | None:
    manifest = read_json(release / "lanefather" / MANIFEST)
    if manifest is not None and isinstance(manifest.get("commit"), str):
        value = manifest["commit"].strip().lower()
        if HEX_COMMIT.fullmatch(value):
            return value
    commit_file = release / "lanefather" / "COMMIT"
    if commit_file.is_file():
        try:
            value = commit_file.read_text().strip().lower()
        except OSError:
            value = ""
        if HEX_COMMIT.fullmatch(value):
            return value
    if "-" in release.name:
        suffix = release.name.rsplit("-", 1)[1].lower()
        if HEX_COMMIT.fullmatch(suffix):
            return suffix
    return None


def release_sort_key(release: Path) -> tuple[tuple[int, ...], int, int, str]:
    """Order releases by semantic version, commit time, commit depth and name (never mtime)."""
    ver = parse_version(release.name)
    manifest = read_json(release / "lanefather" / MANIFEST) or {}
    raw_time = manifest.get("commit_time")
    raw_count = manifest.get("commit_count")
    commit_time = raw_time if type(raw_time) is int else 0
    commit_count = raw_count if type(raw_count) is int else 0
    if commit_time == 0 and commit_count == 0:
        rev = release_commit(release)
        if rev is not None:
            commit_time, commit_count = commit_meta(rev)
    return ver, commit_time, commit_count, release.name


def discover_state_dirs() -> list[Path]:
    """Registered state directories (via registry.py), LANEFATHER_STATE, and the default state dir."""
    candidates: list[Path] = []
    for entry in registry.read():
        raw = entry.get("state_dir")
        if isinstance(raw, str) and raw:
            candidates.append(Path(os.path.expanduser(raw)))
    env_state = os.environ.get("LANEFATHER_STATE")
    if env_state:
        candidates.append(Path(os.path.expanduser(env_state)))
    if not os.environ.get("LANEFATHER_REGISTRY"):
        candidates.append(Path(os.path.expanduser(config.Config().state_dir)))
    seen: set[Path] = set()
    found: list[Path] = []
    for candidate in candidates:
        if not candidate.is_dir():
            continue
        resolved = candidate.resolve()
        if resolved not in seen:
            seen.add(resolved)
            found.append(resolved)
    return found


def _collect_pin_value(value: Any, paths: set[Path], names: set[str], commits: set[str]) -> None:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return
        if "/" in text or os.sep in text:
            expanded = Path(os.path.expanduser(text))
            paths.add(expanded.resolve())
            if expanded.name:
                names.add(expanded.name)
        else:
            names.add(text)
            lowered = text.lower()
            if HEX_COMMIT.fullmatch(lowered):
                commits.add(lowered)
    elif isinstance(value, dict):
        mapping = cast(dict[str, Any], value)
        for key in ("path", "dir", "name", "release"):
            if key in mapping:
                _collect_pin_value(mapping[key], paths, names, commits)
        for key in ("commit", "source_commit", "release_commit"):
            raw = mapping.get(key)
            if isinstance(raw, str):
                lowered = raw.strip().lower()
                if HEX_COMMIT.fullmatch(lowered):
                    commits.add(lowered)


def pinned_releases(share: Path) -> set[Path]:
    """Every release in `share/releases` pinned by a live (not done) lane in any registered state directory."""
    releases_dir = share / "releases"
    if not releases_dir.is_dir():
        return set()
    candidates = [p for p in releases_dir.iterdir() if p.is_dir() and not p.name.startswith(".")]
    if not candidates:
        return set()
    paths: set[Path] = set()
    names: set[str] = set()
    commits: set[str] = set()
    for state_dir in discover_state_dirs():
        for record_file in sorted(state_dir.glob("*.json")):
            if record_file.name.count(".") != 1:
                continue
            record = read_json(record_file)
            if record is None or "id" not in record or record.get("status") == "done":
                continue
            for key in ("release", "lanefather_release", "installed_release", "release_path", "release_dir",
                        "lanefather"):
                if key in record:
                    _collect_pin_value(record[key], paths, names, commits)
            for key in ("lanefather_commit", "source_commit", "release_commit"):
                raw = record.get(key)
                if isinstance(raw, str):
                    lowered = raw.strip().lower()
                    if HEX_COMMIT.fullmatch(lowered):
                        commits.add(lowered)
    pinned: set[Path] = set()
    for rel in candidates:
        resolved = rel.resolve()
        if resolved in paths or rel.name in names:
            pinned.add(resolved)
            continue
        rel_commit = release_commit(rel)
        if rel_commit and any(rel_commit.startswith(c) or c.startswith(rel_commit) for c in commits):
            pinned.add(resolved)
    return pinned


def _lock_held(path: Path) -> bool:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError as error:
        return error.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES)
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def _pid_file_alive(path: Path) -> bool:
    try:
        text = path.read_text().strip()
    except OSError:
        return False
    first = text.split(None, 1)[0] if text else ""
    if not first.isdigit():
        return False
    return pid_alive(int(first))


def release_in_use(release: Path) -> bool:
    """Whether a running process holds a lock or live pid file inside `release`."""
    dirs = [release]
    try:
        for child in release.iterdir():
            if child.is_dir() and not child.is_symlink() and child.name != "__pycache__":
                dirs.append(child)
    except OSError:
        return True
    for directory in dirs:
        try:
            entries = list(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not entry.is_file() or entry.is_symlink():
                continue
            if (entry.name == ".lock" or entry.name.endswith(".lock")) and _lock_held(entry):
                return True
            if (entry.name == ".pid" or entry.name.endswith(".pid")) and _pid_file_alive(entry):
                return True
    return False


def clean_pending(share: Path) -> list[Path]:
    """Remove stale `releases/.pending-<pid>` directories whose owning pid is no longer alive."""
    releases_dir = share / "releases"
    if not releases_dir.is_dir():
        return []
    cleaned: list[Path] = []
    for entry in list(releases_dir.iterdir()):
        match = PENDING_DIR.fullmatch(entry.name)
        if not match:
            continue
        pid = int(match[1])
        if pid == os.getpid() or pid_alive(pid):
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink(missing_ok=True)
        cleaned.append(entry)
    return cleaned


def current_release(share: Path) -> Path | None:
    link = share / "current"
    if not link.is_symlink() and not link.exists():
        return None
    try:
        resolved = link.resolve()
    except OSError:
        return None
    return resolved if resolved.is_dir() else None


def switch_symlink(link_path: Path, target: Path, share: Path) -> None:
    """Atomically point `link_path` at `target` (relative to `share`)."""
    _switch_symlink(link_path, target, relative_to=share)


def prune_releases(share: Path, keep_extra: set[Path] | None = None) -> list[Path]:
    """Prune old releases beyond KEEP_RELEASES by version/build order, keeping current, pinned and in-use releases."""
    releases_dir = share / "releases"
    if not releases_dir.is_dir():
        return []
    candidates = sorted(
        (p for p in releases_dir.iterdir() if p.is_dir() and not p.name.startswith(".")),
        key=release_sort_key,
        reverse=True,
    )
    keep: set[Path] = {p.resolve() for p in candidates[:KEEP_RELEASES]}
    keep |= pinned_releases(share)
    cur = current_release(share)
    if cur is not None:
        keep.add(cur)
    if keep_extra:
        keep |= {p.resolve() for p in keep_extra}
    removed: list[Path] = []
    for old in candidates:
        if old.resolve() in keep or release_in_use(old):
            continue
        shutil.rmtree(old, ignore_errors=True)
        removed.append(old)
    return removed


def install(share: Path, bin_dir: Path, python: str | None, *, switch: bool = True) -> Path:
    built = commit()
    ct, count = commit_meta(built)
    ver = version()
    release_name = f"{ver}-{built[:12]}"
    manifest_digest: list[str] = []

    def populate(pending: Path) -> None:
        copy_python_tree(SOURCE / "lanefather", pending / "lanefather")
        copy_python_tree(COMMON, pending / COMMON.name)
        shutil.copytree(SOURCE / "hooks", pending / "hooks")
        if (SOURCE / "CHANGELOG.md").is_file():
            shutil.copy2(SOURCE / "CHANGELOG.md", pending / "lanefather" / "CHANGELOG.md")
        (pending / "lanefather" / "COMMIT").write_text(built + "\n")
        files = shipping_files(pending)
        digest = payload_digest(files)
        manifest_digest[:] = [digest]
        manifest = {"v": 1, "commit": built, "commit_time": ct, "commit_count": count,
                    "files": files, "sha256": digest}
        (pending / "lanefather" / MANIFEST).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    def verify_existing(existing: Path, _pending: Path) -> None:
        try:
            identity = installed_identity(existing)
            if identity["commit"] != built or identity["sha256"] != manifest_digest[0]:
                raise InstallError("existing release differs from committed shipping inputs")
        except LaneError as error:
            raise InstallError(str(error)) from None

    with install_lock(share / ".install.lock"):
        release = share / "releases" / release_name
        cur = current_release(share)
        if switch and cur is not None and cur != release.resolve():
            candidate_key = (parse_version(release_name), ct, count, release_name)
            if candidate_key < release_sort_key(cur):
                raise InstallError(f"current release {cur.name} is newer than {release_name}; "
                                   "use --no-switch to install without switching or --rollback to move current back")
        clean_pending(share)
        release = stage_release(share / "releases", release_name, populate, verify_existing=verify_existing)
        if switch:
            if cur is not None and cur != release.resolve():
                switch_symlink(share / "previous", cur, share.resolve())
            switch_symlink(share / "current", release, share)
            if python is None:
                raise InstallError("an interpreter is required to write the command")
            write_atomic(bin_dir / "lanefather", LAUNCHER.format(python=python, current=str(share / "current")), 0o755)
        prune_releases(share, keep_extra={release})
    return release


def rollback(share: Path) -> Path:
    """Point `share/current` at the previous release."""
    releases_dir = share / "releases"
    if not releases_dir.is_dir():
        raise InstallError("no previous release to roll back to")
    cur = current_release(share)
    releases_root = releases_dir.resolve()
    target: Path | None = None
    previous_link = share / "previous"
    if previous_link.is_symlink() or previous_link.exists():
        try:
            prev_resolved = previous_link.resolve()
        except OSError:
            prev_resolved = None
        if (prev_resolved is not None and prev_resolved.is_dir()
                and prev_resolved.parent == releases_root and prev_resolved != cur):
            target = releases_dir / prev_resolved.name
    if target is None:
        candidates = sorted(
            (p for p in releases_dir.iterdir() if p.is_dir() and not p.name.startswith(".")),
            key=release_sort_key,
            reverse=True,
        )
        if cur is not None:
            cur_key = release_sort_key(cur)
            older = [p for p in candidates if p.resolve() != cur and release_sort_key(p) < cur_key]
            others = [p for p in candidates if p.resolve() != cur]
            target = older[0] if older else (others[0] if others else None)
        elif candidates:
            target = candidates[0]
    if target is None:
        raise InstallError("no previous release to roll back to")
    if cur is not None and cur.parent == releases_root:
        switch_symlink(share / "previous", releases_dir / cur.name, share)
    switch_symlink(share / "current", target, share)
    return target


def uninstall(share: Path, bin_dir: Path) -> tuple[list[Path], list[Path]]:
    """Remove the command, `current`, and unpinned/unused releases; keep releases pinned by live lanes or in use."""
    command = bin_dir / "lanefather"
    if command.is_dir() and not command.is_symlink():
        raise InstallError(f"{command} is a directory; refusing to remove it")
    command.unlink(missing_ok=True)
    for name in ("current", "previous"):
        link = share / name
        if link.is_symlink() or link.is_file():
            link.unlink(missing_ok=True)
    clean_pending(share)
    releases_dir = share / "releases"
    removed: list[Path] = []
    kept: list[Path] = []
    if releases_dir.is_dir():
        pinned = pinned_releases(share)
        for rel in sorted(releases_dir.iterdir()):
            if not rel.is_dir() or rel.name.startswith("."):
                continue
            if rel.resolve() in pinned or release_in_use(rel):
                kept.append(rel)
            else:
                shutil.rmtree(rel, ignore_errors=True)
                removed.append(rel)
        if not any(releases_dir.iterdir()):
            releases_dir.rmdir()
    if share.is_dir():
        (share / ".install.lock").unlink(missing_ok=True)
        if not any(share.iterdir()):
            share.rmdir()
    return removed, kept


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--python", help="the interpreter the command runs on (default: this one)")
    parser.add_argument("--bin", type=Path, default=Path.home() / ".local/bin", help="where the command goes")
    parser.add_argument("--share", type=Path, default=Path.home() / ".local/share/lanefather",
                        help="where the releases go")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--no-switch", action="store_true",
                       help="install under releases/ without moving current or the command")
    modes.add_argument("--rollback", action="store_true",
                       help="point current at the previous release")
    modes.add_argument("--uninstall", action="store_true",
                       help="remove the command, current, and releases no live lane pins")
    args = parser.parse_args(argv)
    try:
        if args.rollback:
            target = rollback(args.share)
            print(f"Rolled back {args.share / 'current'} to {target}")
            return 0
        if args.uninstall:
            removed, kept = uninstall(args.share, args.bin)
            note = f"; kept {len(kept)} pinned/in-use release(s)" if kept else ""
            print(f"Uninstalled {args.bin / 'lanefather'} (removed {len(removed)} release(s){note})")
            return 0
        python = None if args.no_switch else interpreter(args.python)
        release = install(args.share, args.bin, python, switch=not args.no_switch)
    except (InstallError, OSError) as error:
        print(f"install: {error}", file=sys.stderr)
        return 1
    if args.no_switch:
        print(f"Installed release without switching current: {release}")
    else:
        print(f"Installed {args.bin / 'lanefather'} for {python}: {release}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
