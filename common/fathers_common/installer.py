"""Shared release installer primitives for the fathers tools (fathers#117).

Provides interpreter checking, Git checkout/dirty inspection, deterministic source digests, immutable release staging
with byte-for-byte verification, atomic `current` symlink switching, and multi-command wrapper swapping with rollback.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
import contextlib
from contextlib import contextmanager
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, cast

from .atomic import atomic_symlink, atomic_write, sync_directory


class InstallError(Exception):
    """Raised when an installer check, staging step, or command swap refuses to proceed."""


def clean_git_env() -> dict[str, str]:
    """Environment stripped of `GIT_*` bindings so hook invocations still inspect the target checkout."""
    return {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}


@contextmanager
def install_lock(path: Path, wait: float = 15.0, *, label: str = "install") -> Iterator[None]:
    """Acquire an exclusive flock at `path`, retrying for up to `wait` seconds before raising `InstallError`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        deadline = time.monotonic() + max(0.0, wait)
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                pass
            except OSError as error:
                if error.errno not in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES):
                    raise
            if time.monotonic() >= deadline:
                raise InstallError(
                    f"{label} in progress at {path} for {wait:g} s; nothing was changed: retry after it finishes"
                )
            time.sleep(0.05)
        yield


def resolve_interpreter(
    python: str | None,
    min_python: tuple[int, int],
    *,
    tool: str,
    allow_venv: bool = True,
    allow_explicit_venv: bool = True,
    resolve_which: bool = False,
) -> str:
    """Validate and return the interpreter path for installed commands."""
    explicit = bool(python)
    if python:
        chosen = (
            os.path.abspath(shutil.which(python) or os.path.expanduser(python))
            if resolve_which
            else os.path.abspath(os.path.expanduser(python))
        )
    elif not allow_venv and sys.prefix != sys.base_prefix:
        raise InstallError(
            "this Python belongs to a virtual environment; run install.py with a Python outside it, "
            "or name one with --python"
        )
    else:
        chosen = os.path.abspath(shutil.which(sys.executable) or sys.executable) if resolve_which else sys.executable
    try:
        probe = subprocess.run(
            [chosen, "-c", "import sys; print('%d.%d' % sys.version_info[:2], sys.prefix != sys.base_prefix)"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if probe.returncode:
            raise ValueError("probe exited nonzero")
        found, _, inside = probe.stdout.strip().partition(" ")
        version = tuple(int(part) for part in found.split("."))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        if allow_venv:
            min_text = f"{min_python[0]}.{min_python[1]}"
            raise InstallError(f"{python or sys.executable} is not Python {min_text} or newer") from None
        raise InstallError(f"cannot run {chosen} as Python") from None
    if inside == "True" and ((not explicit and not allow_venv) or (explicit and not allow_explicit_venv)):
        raise InstallError(f"{chosen} belongs to a virtual environment; name a Python outside it")
    if version < min_python:
        if allow_venv:
            raise InstallError(f"{python or sys.executable} is not Python {min_python[0]}.{min_python[1]} or newer")
        raise InstallError(f"{chosen} is Python {found}; {tool} needs {min_python[0]}.{min_python[1]} or later")
    return chosen


def committed_head(
    source_dir: Path,
    shipping_paths: Sequence[str | Path],
    *,
    unversioned_message: str = "install from a committed Git checkout",
    dirty_message: str = "uncommitted shipping inputs: commit them before installing; nothing was changed",
) -> str:
    """Verify `source_dir` is a committed Git checkout with no uncommitted changes in `shipping_paths`."""
    environment = clean_git_env()
    head = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=source_dir,
        capture_output=True,
        text=True,
        env=environment,
    )
    if head.returncode:
        raise InstallError(unversioned_message)
    changed = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all", "--", *(str(p) for p in shipping_paths)],
        cwd=source_dir,
        capture_output=True,
        text=True,
        env=environment,
    )
    if changed.returncode:
        raise InstallError("cannot inspect shipping inputs; the installed release was not changed")
    if changed.stdout.strip():
        raise InstallError(dirty_message)
    return head.stdout.strip()


def files_digest(root: Path, files: Sequence[Path]) -> str:
    """Length-prefixed SHA-256 over `(relative_path, file_bytes)` for `files` under `root`."""
    digest = hashlib.sha256()
    for path in files:
        if not path.is_file():
            raise InstallError(f"source file missing: {path}")
        name = str(path.relative_to(root)).encode("utf-8")
        payload = path.read_bytes()
        digest.update(len(name).to_bytes(8, "big") + name)
        digest.update(len(payload).to_bytes(8, "big") + payload)
    return digest.hexdigest()


def git_source_identity(
    root: Path,
    files: Sequence[Path],
    status_paths: Sequence[str | Path],
    *,
    supplied_file: Path | None = None,
) -> dict[str, Any]:
    """Compute `{v, commit, dirty, source_sha256}` for `files`, or verify a streamed `supplied_file`."""
    digest = files_digest(root, files)
    if supplied_file is not None and supplied_file.is_file():
        data = cast(dict[str, Any], json.loads(supplied_file.read_text(encoding="utf-8")))
        if data.get("source_sha256") != digest:
            raise InstallError("streamed source does not match its recorded digest")
        return data
    environment = clean_git_env()
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        env=environment,
    )
    commit = result.stdout.strip() if result.returncode == 0 else None
    changed = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "status",
            "--porcelain",
            "--untracked-files=normal",
            "--",
            *(str(p) for p in status_paths),
        ],
        capture_output=True,
        text=True,
        env=environment,
    )
    return {
        "v": 1,
        "commit": commit,
        "dirty": bool(changed.stdout.strip()) or commit is None,
        "source_sha256": digest,
    }


def copy_python_tree(source: Path, destination: Path) -> None:
    """Copy a Python package or directory into a staged release, dereferencing symlinks and skipping bytecode."""
    shutil.copytree(source, destination, symlinks=False, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def release_bytes(directory: Path) -> dict[str, bytes]:
    """Map relative file paths inside `directory` to their bytes, ignoring bytecode caches."""
    return {
        str(path.relative_to(directory)): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    }


def stage_release(
    releases_dir: Path,
    release_name: str,
    populate: Callable[[Path], None],
    *,
    verify_existing: Callable[[Path, Path], None] | None = None,
    world_readable: bool = False,
) -> Path:
    """Build a complete immutable release in a private directory and publish or verify `releases_dir / release_name`."""
    releases_dir.mkdir(parents=True, exist_ok=True)
    if world_readable:
        releases_dir.parent.chmod(0o755)
        releases_dir.chmod(0o755)
    release = releases_dir / release_name
    pending = Path(tempfile.mkdtemp(prefix=f".pending-{os.getpid()}-", dir=releases_dir))
    try:
        populate(pending)
        if world_readable:
            for item in pending.rglob("*"):
                item.chmod(0o755 if item.is_dir() else 0o644)
            pending.chmod(0o755)
        if release.exists():
            if verify_existing is not None:
                verify_existing(release, pending)
            elif release_bytes(release) != release_bytes(pending):
                raise InstallError(f"immutable installed release differs from source: {release}")
        else:
            os.replace(pending, release)
            sync_directory(releases_dir)
        return release
    finally:
        if pending.exists():
            shutil.rmtree(pending, ignore_errors=True)


def switch_symlink(link_path: Path, target: Path, *, relative_to: Path | None = None) -> None:
    """Atomically switch `link_path` to point at `target`."""
    atomic_symlink(link_path, target, relative_to=relative_to)


def write_launcher(target: Path, payload: str | bytes, mode: int = 0o755) -> None:
    """Atomically write an executable launcher script at `target`."""
    atomic_write(target, payload, mode=mode)


def _snapshot_command(path: Path) -> tuple[str, bytes | str | None, int]:
    if path.is_symlink():
        return ("symlink", os.readlink(path), 0)
    if path.is_file():
        return ("file", path.read_bytes(), path.stat().st_mode & 0o777)
    return ("absent", None, 0)


def _restore_command(path: Path, snapshot: tuple[str, bytes | str | None, int]) -> None:
    kind, data, mode = snapshot
    if kind == "absent":
        path.unlink(missing_ok=True)
    elif kind == "symlink" and isinstance(data, str):
        atomic_symlink(path, data)
    elif kind == "file" and isinstance(data, bytes):
        atomic_write(path, data, mode=mode)


def swap_commands(bin_dir: Path, writers: Sequence[tuple[str, Callable[[Path], None]]]) -> None:
    """Atomically replace a set of command wrappers in `bin_dir`, restoring the previous set if any fails."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    snapshots = [(name, _snapshot_command(bin_dir / name)) for name, _ in writers]
    swapped: list[tuple[str, tuple[str, bytes | str | None, int]]] = []
    for (name, write_fn), (_, snap) in zip(writers, snapshots):
        target = bin_dir / name
        try:
            write_fn(target)
            swapped.append((name, snap))
        except (InstallError, OSError) as error:
            for prev_name, prev_snap in reversed(swapped):
                with contextlib.suppress(OSError):
                    _restore_command(bin_dir / prev_name, prev_snap)
            with contextlib.suppress(OSError):
                _restore_command(target, snap)
            raise InstallError(f"wrapper swap failed for {name}; restored previous commands: {error}") from None
