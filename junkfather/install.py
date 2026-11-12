#!/usr/bin/env python3
"""Install junkfather: the package into ~/.local/share/junkfather and the `junkfather` command into ~/.local/bin.

  python3 install.py [--python /path/to/python3] [--bin DIR] [--share DIR] [--launchd] [--mastack]

Each install is a new directory, share/releases/<version>-<commit>, and share/current points at it; the command reads
`current` when it starts, so a running supervisor keeps the release it started from. The three newest releases stay.
The command runs on one named interpreter (this one, or --python; 3.9 or newer).

--launchd  writes ~/Library/LaunchAgents/com.junkfather.sweep.plist (`junkfather sweep --plist`: hourly and at load)
           and loads it again (launchctl bootout, then bootstrap).
--mastack  adds junkfather's hook to Mastack's machine settings (`lifecycle` in ~/.config/mastack/hooks.json: the
           SubagentStop, SessionEnd and Stop events, and the PreToolUse observer for Bash commands naming
           junkfather), then runs Mastack's installer (`~/mastack/bin/install --only hooks`, $MASTACK_ROOT moves it),
           which registers them in ~/.claude/settings.json.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

sys.dont_write_bytecode = True
MIN_PYTHON = (3, 9)
KEEP_RELEASES = 3
SOURCE = Path(__file__).resolve().parent
LAUNCHER = """#!{python}
# junkfather's command (install.py): the release share/current points at, on the interpreter it was installed for.
import sys
sys.dont_write_bytecode = True
sys.path.insert(0, {current!r})
from junkfather.cli import main
sys.exit(main())
"""
HOOK = "{command} hook"
EVENTS = ("SubagentStop", "SessionEnd", "Stop")


class InstallError(Exception):
    pass


def interpreter(python: str | None) -> str:
    chosen = python or sys.executable
    result = subprocess.run([chosen, "-c", f"import sys; print(sys.version_info[:2] >= {MIN_PYTHON})"],
                            capture_output=True, text=True)
    if result.returncode or result.stdout.strip() != "True":
        raise InstallError(f"{chosen} is not Python {'.'.join(map(str, MIN_PYTHON))} or newer")
    return os.path.abspath(shutil.which(chosen) or chosen)


def commit() -> str:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    head = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=SOURCE, capture_output=True, text=True,
                          env=environment)
    if head.returncode:
        raise InstallError("install from a committed Git checkout")
    changed = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all", "--", "junkfather",
                              "install.py"], cwd=SOURCE, capture_output=True, text=True, env=environment)
    if changed.returncode or changed.stdout.strip():
        raise InstallError("uncommitted shipping inputs: commit them before installing; nothing was changed")
    return head.stdout.strip()


def version() -> str:
    for line in (SOURCE / "junkfather" / "__init__.py").read_text().splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].split("#")[0].strip().strip('"')
    raise InstallError("junkfather/__init__.py names no __version__")


def write_atomic(path: Path, text: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=".junkfather-", delete=False) as handle:
        handle.write(text)
        os.fchmod(handle.fileno(), mode)
    os.replace(handle.name, path)


def install(share: Path, bin_dir: Path, python: str) -> Path:
    built = commit()
    release = share / "releases" / f"{version()}-{built[:12]}"
    if not release.exists():
        pending = share / "releases" / f".pending-{os.getpid()}"
        shutil.rmtree(pending, ignore_errors=True)
        shutil.copytree(SOURCE / "junkfather", pending / "junkfather",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copy2(SOURCE / "CHANGELOG.md", pending / "CHANGELOG.md")
        (pending / "junkfather" / "COMMIT").write_text(built + "\n")
        pending.rename(release)
    link = share / f".current-{os.getpid()}"
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(release.relative_to(share))
    os.replace(link, share / "current")
    write_atomic(bin_dir / "junkfather", LAUNCHER.format(python=python, current=str(share / "current")), 0o755)
    releases = sorted((p for p in (share / "releases").iterdir() if not p.name.startswith(".")),
                      key=lambda p: p.stat().st_mtime, reverse=True)
    for old in releases[KEEP_RELEASES:]:
        if old.resolve() != release.resolve():
            shutil.rmtree(old, ignore_errors=True)
    return release


def launchd(command: Path) -> Path:
    plist = Path.home() / "Library" / "LaunchAgents" / "com.junkfather.sweep.plist"
    text = subprocess.run([str(command), "sweep", "--plist"], capture_output=True, text=True, check=True).stdout
    write_atomic(plist, text, 0o644)
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/com.junkfather.sweep"], capture_output=True)
    for attempt in range(5):  # bootout finishes asynchronously; an early bootstrap fails with error 5
        loaded = subprocess.run(["launchctl", "bootstrap", domain, str(plist)], capture_output=True, text=True)
        if not loaded.returncode:
            break
        time.sleep(0.5 * (attempt + 1))
    if loaded.returncode:
        raise InstallError(f"launchctl bootstrap: {loaded.stderr.strip()}")
    return plist


def mastack(command: Path) -> None:
    """Add junkfather's lifecycle entries to Mastack's machine settings and let Mastack register them."""
    config = Path(os.environ.get("MASTACK_CONFIG") or Path.home() / ".config" / "mastack").expanduser()
    path = (config / "hooks.json").resolve()  # a symlinked settings file stays a symlink
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        raise InstallError(f"no {path}: install Mastack's hooks first") from None
    except ValueError as error:
        raise InstallError(f"{path} is not JSON: {error}") from None
    hook = HOOK.format(command=shlex.quote(str(command)))
    lifecycle = data.setdefault("lifecycle", {})
    if not isinstance(lifecycle, dict):
        raise InstallError(f"`lifecycle` in {path} is not an object")
    for event in EVENTS:
        entries = lifecycle.setdefault(event, [])
        if hook not in entries:
            entries.append(hook)
    observers = lifecycle.setdefault("PreToolUse", [])
    observer = {"command": hook, "contains": ["junkfather"]}
    if observer not in observers:
        observers.append(observer)
    write_atomic(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n", path.stat().st_mode & 0o7777)
    root = Path(os.environ.get("MASTACK_ROOT") or Path.home() / "mastack").expanduser()
    result = subprocess.run([str(root / "bin" / "install"), "--only", "hooks"], text=True)
    if result.returncode:
        raise InstallError("Mastack's installer failed; its message is above")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--python", help="the interpreter the command runs on (default: this one)")
    parser.add_argument("--bin", type=Path, default=Path.home() / ".local/bin")
    parser.add_argument("--share", type=Path, default=Path.home() / ".local/share/junkfather")
    parser.add_argument("--launchd", action="store_true", help="install and load the hourly sweep agent")
    parser.add_argument("--mastack", action="store_true", help="register the hooks through Mastack")
    args = parser.parse_args(argv)
    try:
        python = interpreter(args.python)
        release = install(args.share, args.bin, python)
        print(f"Installed {args.bin / 'junkfather'} for {python}: {release}")
        if args.launchd:
            print(f"Loaded {launchd(args.bin / 'junkfather')}")
        if args.mastack:
            mastack(args.bin / "junkfather")
    except (InstallError, OSError, subprocess.CalledProcessError) as error:
        print(f"install: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
