#!/usr/bin/env python3
"""Install lanefather: the package into ~/.local/share/lanefather and the `lanefather` command into ~/.local/bin.

  python3 install.py [--python /path/to/python3] [--bin DIR] [--share DIR]

The command runs on one named interpreter, the one running this installer (or --python, 3.11 or newer for tomllib),
so an agent inside a project's virtual environment still runs lanefather on the Python it was installed with.

Each install is a new directory, share/releases/<version>-<commit>, and share/current points at it; the command reads
`current` when it starts. A running lanefather keeps the release it started from (a background review re-runs that
release), so the installer keeps the three newest releases. `lanefather --version` names the commit.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

MIN_PYTHON = (3, 11)
KEEP_RELEASES = 3
SOURCE = Path(__file__).resolve().parent
LAUNCHER = """#!{python}
# lanefather's command (install.py): the release share/current points at, on the interpreter it was installed for.
import sys
sys.path.insert(0, {current!r})
from lanefather.cli import main
sys.exit(main())
"""


class InstallError(Exception):
    pass


def interpreter(python: str | None) -> str:
    chosen = python or sys.executable
    result = subprocess.run([chosen, "-c", "import sys; print(sys.version_info[:2] >= (3, 11))"],
                            capture_output=True, text=True)
    if result.returncode or result.stdout.strip() != "True":
        raise InstallError(f"{chosen} is not Python {'.'.join(map(str, MIN_PYTHON))} or newer")
    # The path as found, not resolved: /opt/homebrew/bin/python3 and ~/.local/bin/python3 are links that survive a
    # Python upgrade, while the versioned file they point at goes with it (review of #648, finding 6).
    return os.path.abspath(shutil.which(chosen) or chosen)


def commit() -> str:
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=SOURCE, capture_output=True, text=True).stdout.strip()
    dirty = subprocess.run(["git", "status", "--porcelain", "--", "lanefather"], cwd=SOURCE, capture_output=True,
                           text=True).stdout.strip()
    return (head or "unknown") + ("-dirty" if dirty else "")


def version() -> str:
    for line in (SOURCE / "lanefather" / "__init__.py").read_text().splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip('"')
    raise InstallError("lanefather/__init__.py names no __version__")


def write_atomic(path: Path, text: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=".lanefather-", delete=False) as handle:
        handle.write(text)
        handle.flush()
        os.fchmod(handle.fileno(), mode)
        os.fsync(handle.fileno())
    os.replace(handle.name, path)


def install(share: Path, bin_dir: Path, python: str) -> Path:
    built = commit()
    release = share / "releases" / f"{version()}-{built[:12]}"
    pending = share / "releases" / f".pending-{os.getpid()}"
    shutil.rmtree(pending, ignore_errors=True)
    shutil.copytree(SOURCE / "lanefather", pending / "lanefather",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (pending / "lanefather" / "COMMIT").write_text(built + "\n")
    if release.exists():
        shutil.rmtree(release)
    pending.rename(release)
    # Swap share/current in one rename, so a starting command sees the old release or the new one.
    link = share / f".current-{os.getpid()}"
    link.unlink(missing_ok=True)
    link.symlink_to(release.relative_to(share))
    os.replace(link, share / "current")
    write_atomic(bin_dir / "lanefather", LAUNCHER.format(python=python, current=str(share / "current")), 0o755)
    releases = sorted((p for p in (share / "releases").iterdir() if not p.name.startswith(".")),
                      key=lambda p: p.stat().st_mtime, reverse=True)
    for old in releases[KEEP_RELEASES:]:
        shutil.rmtree(old, ignore_errors=True)
    return release


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--python", help="the interpreter the command runs on (default: this one)")
    parser.add_argument("--bin", type=Path, default=Path.home() / ".local/bin", help="where the command goes")
    parser.add_argument("--share", type=Path, default=Path.home() / ".local/share/lanefather",
                        help="where the releases go")
    args = parser.parse_args(argv)
    try:
        python = interpreter(args.python)
        release = install(args.share, args.bin, python)
    except (InstallError, OSError) as error:
        print(f"install: {error}", file=sys.stderr)
        return 1
    print(f"Installed {args.bin / 'lanefather'} for {python}: {release}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
