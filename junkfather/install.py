#!/usr/bin/env python3
"""Install junkfather: the package into ~/.local/share/junkfather and the `junkfather` command into ~/.local/bin.

  python3 install.py [--python /path/to/python3] [--bin DIR] [--share DIR] [--launchd] [--mastack]

Each install is a new directory, share/releases/<version>-<commit>, and share/current points at it; the command reads
`current` when it starts, so a running supervisor keeps the release it started from. The three newest releases stay.
The command runs on one named interpreter (this one, or --python; 3.9 or newer).

--launchd  writes ~/Library/LaunchAgents/com.junkfather.sweep.plist (`junkfather sweep --plist`: hourly and at load)
           and loads it again (launchctl bootout, then bootstrap).
--mastack  adds junkfather's hook to Mastack's machine settings (`lifecycle` in ~/.config/mastack/hooks.json: the
           SubagentStop, SessionEnd and Stop events; the PreToolUse observer for Bash commands naming junkfather;
           and the PreToolUse rewriter for background Bash calls, with the permission rule its added line needs,
           `Bash(<bin>/junkfather background:*)`, replacing the Bash hook an earlier build registered directly),
           then runs Mastack's installer (`~/mastack/bin/install --only hooks`, $MASTACK_ROOT moves it), which
           registers them in ~/.claude/settings.json. On macOS it implies --launchd: the sweep is what ends an entry
           whose session vanished without a SessionEnd.
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
import time
from typing import Any, Dict, List, cast

sys.dont_write_bytecode = True
SOURCE = Path(__file__).resolve().parent
COMMON = SOURCE.parent / "common" / "fathers_common"
if str(SOURCE.parent / "common") not in sys.path:
    sys.path.insert(0, str(SOURCE.parent / "common"))

from fathers_common.atomic import atomic_write  # noqa: E402
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

__all__ = ["InstallError", "allow_rule", "install", "interpreter", "launchd", "main", "mastack", "write_atomic"]

MIN_PYTHON = (3, 9)
KEEP_RELEASES = 3
LAUNCHER = """#!{python}
# junkfather's command (install.py): the release share/current points at, on the interpreter it was installed for.
import sys
sys.dont_write_bytecode = True
sys.path.insert(0, {current!r})
from junkfather.cli import main
sys.exit(main())
"""
HOOK = "{command} hook"
# The Bash hook an earlier build of this lane registered in ~/.claude/settings.json itself; Mastack removes it.
DIRECT_HOOK = """sh -c '[ -x "$0" ] || exit 0; exec "$0" hook' {command}"""
EVENTS = ("SubagentStop", "SessionEnd", "Stop")


def interpreter(python: str | None) -> str:
    return resolve_interpreter(python, MIN_PYTHON, tool="junkfather", allow_venv=True, resolve_which=True)


def commit() -> str:
    return committed_head(SOURCE, ("junkfather", "install.py", str(COMMON)))


def version() -> str:
    for line in (SOURCE / "junkfather" / "__init__.py").read_text().splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].split("#")[0].strip().strip('"')
    raise InstallError("junkfather/__init__.py names no __version__")


def write_atomic(path: Path, text: str, mode: int) -> None:
    atomic_write(path, text, mode=mode)


def install(share: Path, bin_dir: Path, python: str) -> Path:
    built = commit()
    release_name = f"{version()}-{built[:12]}"

    def populate(pending: Path) -> None:
        copy_python_tree(SOURCE / "junkfather", pending / "junkfather")
        copy_python_tree(COMMON, pending / COMMON.name)
        shutil.copy2(SOURCE / "CHANGELOG.md", pending / "CHANGELOG.md")
        (pending / "junkfather" / "COMMIT").write_text(built + "\n")

    with install_lock(share / ".install.lock"):
        release = stage_release(share / "releases", release_name, populate)
        switch_symlink(share / "current", release, relative_to=share)
        write_launcher(bin_dir / "junkfather", LAUNCHER.format(python=python, current=str(share / "current")), 0o755)
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
    attempt = 0
    while True:  # bootout finishes asynchronously; an early bootstrap fails with error 5
        loaded = subprocess.run(["launchctl", "bootstrap", domain, str(plist)], capture_output=True, text=True)
        if not loaded.returncode:
            break
        time.sleep(0.5 * (attempt + 1))
        attempt += 1
        if attempt == 5:
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
    found = cast(Dict[str, Any], data).setdefault("lifecycle", {}) if isinstance(data, dict) else None
    if not isinstance(found, dict):
        raise InstallError(f"`lifecycle` in {path} is not an object")
    lifecycle = cast(Dict[str, List[Any]], found)
    for event in EVENTS:
        entries = lifecycle.setdefault(event, [])
        if hook not in entries:
            entries.append(hook)
    observers = lifecycle.setdefault("PreToolUse", [])
    observer = {"command": hook, "contains": ["junkfather"]}
    if observer not in observers:
        observers.append(observer)
    rewriter = {"command": hook, "background": True, "rewrite": True, "allow": [allow_rule(command)],
                "replaces": [DIRECT_HOOK.format(command=shlex.quote(str(command)))]}
    if rewriter not in observers:
        observers[:] = [e for e in observers if not (isinstance(e, dict) and cast(Dict[str, Any], e).get("command")
                                                    == hook and cast(Dict[str, Any], e).get("rewrite"))] + [rewriter]
    write_atomic(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n", path.stat().st_mode & 0o7777)
    root = Path(os.environ.get("MASTACK_ROOT") or Path.home() / "mastack").expanduser()
    result = subprocess.run([str(root / "bin" / "install"), "--only", "hooks"], text=True)
    if result.returncode:
        raise InstallError("Mastack's installer failed; its message is above")


def allow_rule(command: Path) -> str:
    """The permission rule that lets the line the hook puts before a background command run without a prompt: it
    starts with the command as Mastack runs it (background.adopting_line). It covers that line only: Claude Code
    judges each line of the call on its own."""
    return f"Bash({shlex.quote(str(command))} background:*)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--python", help="the interpreter the command runs on (default: this one)")
    parser.add_argument("--bin", type=Path, default=Path.home() / ".local/bin")
    parser.add_argument("--share", type=Path, default=Path.home() / ".local/share/junkfather")
    parser.add_argument("--launchd", action="store_true", help="install and load the hourly sweep agent")
    parser.add_argument("--mastack", action="store_true", help="register the hooks through Mastack")
    args = parser.parse_args(argv)
    args.bin = args.bin.expanduser().resolve()  # the hook and the permission rule name the command absolutely
    args.share = args.share.expanduser().resolve()
    try:
        python = interpreter(args.python)
        release = install(args.share, args.bin, python)
        print(f"Installed {args.bin / 'junkfather'} for {python}: {release}")
        if args.launchd or (args.mastack and sys.platform == "darwin"):
            print(f"Loaded {launchd(args.bin / 'junkfather')}")
        if args.mastack:
            mastack(args.bin / "junkfather")
            print(f"Asked Mastack for the hooks and the permission rule {allow_rule(args.bin / 'junkfather')}")
    except (InstallError, OSError, subprocess.CalledProcessError) as error:
        print(f"install: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
