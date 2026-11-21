"""Durable atomic file and symlink replacement (fathers#117).

Each writer creates a unique temporary file beside the target (named `.<name>.<pid>.<hex>.tmp` so record globs skip it),
writes every byte, fsyncs the file, replaces the target in one rename, and fsyncs the parent directory. Any failure
removes the temporary file and leaves the target untouched.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path
import secrets

UNSYNCABLE = frozenset({errno.EINVAL, errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)})


def sync_directory(directory: Path) -> None:
    """Sync `directory` so a rename inside it is durable; ignore filesystems that cannot fsync directories."""
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    except OSError as error:
        if error.errno not in UNSYNCABLE:
            raise
    finally:
        os.close(fd)


def atomic_write(path: Path | str, data: str | bytes, mode: int | None = None) -> None:
    """Atomically replace `path` with `data` (`str` as UTF-8, or `bytes`).

    `mode` sets the file's permissions explicitly (independent of umask); `None` leaves permissions to the process
    umask (`0o666 & ~umask`), matching an ordinary new file.
    """
    target = Path(path)
    raw = data.encode("utf-8") if isinstance(data, str) else data
    target.parent.mkdir(parents=True, exist_ok=True)
    pending = target.with_name(f".{target.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666 if mode is None else mode)
    try:
        try:
            if mode is not None:
                os.fchmod(fd, mode)
            view = memoryview(raw)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(pending, target)
    except BaseException:
        pending.unlink(missing_ok=True)
        raise
    sync_directory(target.parent)


def atomic_symlink(link_path: Path | str, target: Path | str, *, relative_to: Path | None = None) -> None:
    """Atomically point symlink `link_path` at `target` (or `target.relative_to(relative_to)`)."""
    link = Path(link_path)
    link.parent.mkdir(parents=True, exist_ok=True)
    destination = Path(target).relative_to(relative_to) if relative_to is not None else Path(target)
    pending = link.with_name(f".{link.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        os.symlink(destination, pending)
        os.replace(pending, link)
    except BaseException:
        pending.unlink(missing_ok=True)
        raise
    sync_directory(link.parent)
