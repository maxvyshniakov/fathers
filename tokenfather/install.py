#!/usr/bin/env python3
"""Install tokenfather: the package into ~/.local/share/tokenfather and the `tokenfather` command into ~/.local/bin.

  python3 install.py [--python /path/to/python3] [--bin DIR] [--share DIR]

Each install is a new immutable directory, share/releases/<version>-<commit>, staged privately, verified byte for byte
against an existing release of the same name, and smoke-checked before share/current switches to it; the command reads
`current` when it starts. The three newest releases stay. Installs come from a committed checkout whose shipping inputs
(the package, install.py, CHANGELOG.md and common/fathers_common) have no uncommitted changes, one at a time under
share/.install.lock. Built on `fathers_common.installer` (fathers#117, fathers#208).
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
SOURCE = Path(__file__).resolve().parent
COMMON = SOURCE.parent / "common" / "fathers_common"
if str(SOURCE.parent / "common") not in sys.path:
    sys.path.insert(0, str(SOURCE.parent / "common"))

from fathers_common.installer import (  # noqa: E402
    InstallError,
    committed_head,
    copy_python_tree,
    install_lock,
    resolve_interpreter,
    stage_release,
    switch_symlink,
    write_launcher,
)

__all__ = ["InstallError", "commit", "install", "interpreter", "main", "smoke_check", "version"]

MIN_PYTHON = (3, 11)
KEEP_RELEASES = 3
SHIPPING = ("tokenfather", "install.py", "CHANGELOG.md", str(COMMON))
LAUNCHER = """#!{python}
# tokenfather's command (install.py): the release share/current points at, on the interpreter it was installed for.
import sys
sys.dont_write_bytecode = True
sys.path.insert(0, {current!r})
from tokenfather.cli import main
sys.exit(main())
"""


def interpreter(python: str | None) -> str:
    return resolve_interpreter(python, MIN_PYTHON, tool="tokenfather", allow_venv=True, resolve_which=True)


def commit() -> str:
    return committed_head(
        SOURCE,
        SHIPPING,
        unversioned_message="install from a committed Git checkout, not an unversioned source directory",
    )


def version() -> str:
    for line in (SOURCE / "tokenfather" / "__init__.py").read_text(encoding="utf-8").splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].split("#")[0].strip().strip('"')
    raise InstallError("tokenfather/__init__.py names no __version__")


def smoke_check(release: Path, python: str) -> None:
    """The staged release answers `--version` and `changelog --json` on `python` before anything points at it."""
    with tempfile.TemporaryDirectory(prefix="tokenfather-smoke-") as scratch:
        environment = {key: value for key, value in os.environ.items() if not key.startswith("TOKENFATHER_")}
        environment.update(
            PYTHONPATH=str(release),
            PYTHONDONTWRITEBYTECODE="1",
            TOKENFATHER_STATE=str(Path(scratch) / "state"),
            TOKENFATHER_EVENTS=str(Path(scratch) / "events.jsonl"),
        )
        for args in (["--version"], ["changelog", "--json"]):
            try:
                result = subprocess.run([python, "-m", "tokenfather", *args], cwd=scratch, env=environment,
                                        capture_output=True, text=True, timeout=20)
            except (OSError, subprocess.TimeoutExpired) as error:
                raise InstallError(f"smoke check failed for {release.name} ({' '.join(args)}): {error}") from None
            if result.returncode or not result.stdout.strip():
                detail = (result.stderr or result.stdout).strip().splitlines()
                reason = detail[-1] if detail else f"exit {result.returncode}"
                raise InstallError(f"smoke check failed for {release.name} ({' '.join(args)}): {reason}")


def install(share: Path, bin_dir: Path, python: str) -> Path:
    built = commit()
    release_name = f"{version()}-{built[:12]}"

    def populate(pending: Path) -> None:
        copy_python_tree(SOURCE / "tokenfather", pending / "tokenfather")
        copy_python_tree(COMMON, pending / COMMON.name)
        shutil.copyfile(SOURCE / "CHANGELOG.md", pending / "tokenfather" / "CHANGELOG.md")
        (pending / "tokenfather" / "COMMIT").write_text(built + "\n", encoding="utf-8")

    with install_lock(share / ".install.lock", label="tokenfather install"):
        release = stage_release(share / "releases", release_name, populate)
        smoke_check(release, python)
        current = share / "current"
        previous = os.readlink(current) if current.is_symlink() else None
        switch_symlink(current, release, relative_to=share)
        try:
            write_launcher(bin_dir / "tokenfather", LAUNCHER.format(python=python, current=str(current)), 0o755)
        except BaseException:
            if previous is not None:
                switch_symlink(current, Path(previous))
            else:
                current.unlink(missing_ok=True)
            raise
        releases = sorted((p for p in (share / "releases").iterdir() if not p.name.startswith(".")),
                          key=lambda p: p.stat().st_mtime, reverse=True)
        for old in releases[KEEP_RELEASES:]:
            if old.resolve() != release.resolve():
                shutil.rmtree(old, ignore_errors=True)
    return release


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install tokenfather.")
    parser.add_argument("--python", help="interpreter for the installed command (Python 3.11+)")
    parser.add_argument("--bin", type=Path, default=Path.home() / ".local" / "bin")
    parser.add_argument("--share", type=Path, default=Path.home() / ".local" / "share" / "tokenfather")
    args = parser.parse_args(argv)
    try:
        python = interpreter(args.python)
        release = install(args.share.expanduser().resolve(), args.bin.expanduser().resolve(), python)
    except (InstallError, OSError) as error:
        print(f"install: {error}", file=sys.stderr)
        return 2
    print(f"Installed tokenfather to {release}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
