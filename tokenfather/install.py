#!/usr/bin/env python3
"""Install tokenfather: the package into ~/.local/share/tokenfather and the `tokenfather` command into ~/.local/bin.

  python3 install.py [--python /path/to/python3] [--bin DIR] [--share DIR]
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys

sys.dont_write_bytecode = True

MIN_PYTHON = (3, 11)
KEEP_RELEASES = 3
SOURCE = Path(__file__).resolve().parent
COMMON = SOURCE.parent / "common" / "fathers_common"
LAUNCHER = """#!{python}
# tokenfather's command (install.py): the release share/current points at, on the interpreter it was installed for.
import sys
sys.dont_write_bytecode = True
sys.path.insert(0, {current!r})
from tokenfather.cli import main
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
    return os.path.abspath(shutil.which(chosen) or chosen)


def commit() -> str:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    head = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=SOURCE,
                          capture_output=True, text=True, env=environment)
    if head.returncode:
        raise InstallError("install from a committed Git checkout, not an unversioned source directory")
    return head.stdout.strip()


def version() -> str:
    for line in (SOURCE / "tokenfather" / "__init__.py").read_text().splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip('"')
    raise InstallError("tokenfather/__init__.py names no __version__")


def install(share: Path, bin_dir: Path, python: str) -> Path:
    built = commit()
    release = share / "releases" / f"{version()}-{built[:12]}"
    pending = share / "releases" / f".pending-{os.getpid()}"
    shutil.rmtree(pending, ignore_errors=True)
    shutil.copytree(SOURCE / "tokenfather", pending / "tokenfather",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(COMMON, pending / COMMON.name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    if (SOURCE / "CHANGELOG.md").is_file():
        shutil.copy2(SOURCE / "CHANGELOG.md", pending / "tokenfather" / "CHANGELOG.md")
    (pending / "tokenfather" / "COMMIT").write_text(built + "\n")
    if release.exists():
        shutil.rmtree(pending, ignore_errors=True)
    else:
        pending.rename(release)
    link = share / f".current-{os.getpid()}"
    link.unlink(missing_ok=True)
    link.symlink_to(release)
    link.replace(share / "current")
    bin_dir.mkdir(parents=True, exist_ok=True)
    command = bin_dir / "tokenfather"
    tmp = bin_dir / f".tokenfather.{os.getpid()}.tmp"
    tmp.write_text(LAUNCHER.format(python=python, current=str(share / "current")))
    tmp.chmod(0o755)
    tmp.replace(command)
    return release


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install tokenfather.")
    parser.add_argument("--python", help="interpreter for the installed command (Python 3.11+)")
    parser.add_argument("--bin", type=Path, default=Path.home() / ".local" / "bin")
    parser.add_argument("--share", type=Path, default=Path.home() / ".local" / "share" / "tokenfather")
    args = parser.parse_args(argv)
    try:
        release = install(args.share.expanduser(), args.bin.expanduser(), interpreter(args.python))
    except InstallError as error:
        print(f"install: {error}", file=sys.stderr)
        return 2
    print(f"Installed tokenfather to {release}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
