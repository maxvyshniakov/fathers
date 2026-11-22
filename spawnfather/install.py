#!/usr/bin/env python3
"""Install Spawnfather: the package into ~/.local/share/spawnfather and `spawnfather` (plus a forwarding `junkfather`
shim) into ~/.local/bin.

  python3 install.py [--python /path/to/python3] [--bin DIR] [--share DIR] [--launchd] [--mastack] [--claude-hooks]

Each install is a new directory, share/releases/<version>-<commit>, and share/current points at it; the command reads
`current` when it starts, so a running supervisor keeps the release it started from. The three newest releases stay,
and any prior `~/.local/share/junkfather` releases stay untouched so live older supervisors keep running.
The command runs on one named interpreter (this one, or --python; 3.11 or newer).

Before mutating anything, `install.py` checks state and settings migration paths (`~/.local/state/junkfather` ->
`~/.local/state/spawnfather` and `~/.config/junkfather` -> `~/.config/spawnfather`, or their XDG / env equivalents) and
refuses if both old and new directories already hold independent stores. Otherwise it moves the store under its flock
and leaves `old -> new` as a symbolic link so live old helpers share the migrated store.

--launchd       retires `com.junkfather.sweep` (bootout and unlink), writes
                `~/Library/LaunchAgents/com.spawnfather.sweep.plist` (`spawnfather sweep --plist`: hourly and at load),
                and loads it (launchctl bootout, then bootstrap).
--mastack       updates Spawnfather's lifecycle hooks in `~/.config/mastack/hooks.json` (`SubagentStop`, `SessionEnd`,
                `Stop`, and the `PreToolUse` observer, migrating any existing background rewriter to `spawnfather`
                without enabling a disabled rewriter unless `--enable-background-rewriter` is passed) and runs
                Mastack's installer (`~/mastack/bin/install --only hooks`, `$MASTACK_ROOT` moves it). On macOS it
                implies `--launchd`.
--claude-hooks  updates `~/.claude/settings.json` (`$CLAUDE_CONFIG_DIR` moves it) directly, rewriting old `junkfather`
                lifecycle/direct-hook entries to `spawnfather` while preserving symlinks, unrelated rules, and the
                disabled background rewriter.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, List, Mapping, Optional, Tuple, cast

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
    swap_commands,
    switch_symlink,
    write_launcher,
)

__all__ = [
    "LABEL",
    "LEGACY_LABEL",
    "InstallError",
    "allow_rule",
    "check_migration_conflicts",
    "claude_hooks",
    "install",
    "install_claude_hooks",
    "install_launchd",
    "install_mastack_hooks",
    "interpreter",
    "launchd",
    "main",
    "mastack",
    "migrate_config_dir",
    "migrate_state_dir",
    "resolve_config_paths",
    "resolve_state_paths",
    "write_atomic",
]

MIN_PYTHON = (3, 11)
KEEP_RELEASES = 3
LABEL = "com.spawnfather.sweep"
LEGACY_LABEL = "com.junkfather.sweep"
TOOL_NAMES = ("spawnfather", "junkfather")

LAUNCHER = """#!{python}
# spawnfather's command (install.py): the release share/current points at, on the interpreter it was installed for.
import sys
sys.dont_write_bytecode = True
sys.path.insert(0, {current!r})
from spawnfather.cli import main
sys.exit(main())
"""

LEGACY_SHIM = """#!{python}
# Legacy forwarding shim (fathers#170): forwards exact argv and exit code to spawnfather without touching stdout.
import os
from pathlib import Path
import sys
sys.dont_write_bytecode = True
sys.stderr.write("junkfather: deprecated; use spawnfather\\n")
sys.stderr.flush()
target = Path(__file__).resolve().with_name("spawnfather")
if not target.is_file():
    target = Path({spawnfather!r})
os.execv(str(target), [str(target), *sys.argv[1:]])
"""

HOOK = "{command} hook"
DIRECT_HOOK_BODY = '[ -x "$0" ] || exit 0; exec "$0" hook'
DIRECT_HOOK = f"sh -c '{DIRECT_HOOK_BODY}' {{command}}"
EVENTS = ("SubagentStop", "SessionEnd", "Stop")
ALLOW_RE = re.compile(r"\ABash\((.+)\s+background:\*\)\Z")


def _home(env: Optional[Mapping[str, str]] = None) -> Path:
    source = os.environ if env is None else env
    configured = source.get("HOME")
    return Path(configured).expanduser() if configured else Path.home()


def interpreter(python: str | None) -> str:
    return resolve_interpreter(python, MIN_PYTHON, tool="spawnfather", allow_venv=True, resolve_which=True)


def commit() -> str:
    return committed_head(SOURCE, ("spawnfather", "install.py", str(COMMON)))


def version() -> str:
    for line in (SOURCE / "spawnfather" / "__init__.py").read_text(encoding="utf-8").splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].split("#")[0].strip().strip('"')
    raise InstallError("spawnfather/__init__.py names no __version__")


def write_atomic(path: Path, text: str, mode: int) -> None:
    atomic_write(path, text, mode=mode)


# State & config migration

def resolve_state_paths(env: Optional[Mapping[str, str]] = None) -> Tuple[Optional[Path], Path]:
    """Return `(old_state, new_state)` for migration. When only `SPAWNFATHER_STATE` is set, `old_state` is None so an
    isolated state override never pulls default `~/.local/state/junkfather` into a custom directory."""
    source = os.environ if env is None else env
    base = Path(source.get("XDG_STATE_HOME") or _home(source) / ".local" / "state").expanduser()
    new_override = source.get("SPAWNFATHER_STATE")
    old_override = source.get("JUNKFATHER_STATE")
    if new_override and old_override:
        return Path(old_override).expanduser(), Path(new_override).expanduser()
    if new_override:
        return None, Path(new_override).expanduser()
    if old_override:
        return Path(old_override).expanduser(), base / "spawnfather"
    return base / "junkfather", base / "spawnfather"


def resolve_config_paths(env: Optional[Mapping[str, str]] = None) -> Tuple[Optional[Path], Path]:
    """Return `(old_config, new_config)` for `~/.config/{junkfather,spawnfather}` (or `$XDG_CONFIG_HOME`)."""
    source = os.environ if env is None else env
    home = _home(source)
    if source.get("SPAWNFATHER_STATE") and not source.get("JUNKFATHER_STATE") and not source.get("XDG_CONFIG_HOME"):
        return None, home / ".config" / "spawnfather"
    base = Path(source.get("XDG_CONFIG_HOME") or home / ".config").expanduser()
    return base / "junkfather", base / "spawnfather"


def _dir_nonempty(path: Path) -> bool:
    return any(path.iterdir())


def _same_resolved(left: Path, right: Path) -> bool:
    return left.resolve() == right.resolve()


def _check_state_conflict(old_state: Optional[Path], new_state: Path) -> None:
    if old_state is None or (not old_state.exists() and not old_state.is_symlink()):
        return
    if old_state.is_symlink():
        if not old_state.exists():
            return
        if _same_resolved(old_state, new_state):
            return
        if new_state.exists() and _dir_nonempty(new_state):
            raise InstallError(
                f"cannot migrate state: {old_state} points at {old_state.resolve()} while {new_state} already has files"
            )
        return
    if not old_state.is_dir():
        raise InstallError(f"cannot migrate state: {old_state} is not a directory")
    if new_state.is_symlink():
        if new_state.exists() and _same_resolved(new_state, old_state):
            return
        raise InstallError(f"cannot migrate state: {new_state} is an unexpected symbolic link")
    if new_state.exists():
        if not new_state.is_dir():
            raise InstallError(f"cannot migrate state: {new_state} is not a directory")
        if not _same_resolved(old_state, new_state) and _dir_nonempty(new_state):
            raise InstallError(
                f"cannot migrate state: both {old_state} and {new_state} exist and {new_state} is not empty"
            )


def _identical_single_settings(old_config: Path, new_config: Path) -> bool:
    old_items = list(old_config.iterdir())
    new_items = list(new_config.iterdir())
    if [p.name for p in old_items] != ["settings.json"] or [p.name for p in new_items] != ["settings.json"]:
        return False
    old_file, new_file = old_config / "settings.json", new_config / "settings.json"
    return old_file.is_file() and new_file.is_file() and old_file.read_bytes() == new_file.read_bytes()


def _check_config_conflict(old_config: Optional[Path], new_config: Path) -> None:
    if old_config is None or (not old_config.exists() and not old_config.is_symlink()):
        return
    if old_config.is_symlink():
        if not old_config.exists() or _same_resolved(old_config, new_config):
            return
        if new_config.exists() and _dir_nonempty(new_config):
            raise InstallError(
                f"cannot migrate config: {old_config} points at {old_config.resolve()} while {new_config} is not empty"
            )
        return
    if not old_config.is_dir():
        raise InstallError(f"cannot migrate config: {old_config} is not a directory")
    if new_config.is_symlink():
        if new_config.exists() and _same_resolved(new_config, old_config):
            return
        raise InstallError(f"cannot migrate config: {new_config} is an unexpected symbolic link")
    if new_config.exists():
        if not new_config.is_dir():
            raise InstallError(f"cannot migrate config: {new_config} is not a directory")
        if not _same_resolved(old_config, new_config) and _dir_nonempty(new_config):
            if not _identical_single_settings(old_config, new_config):
                raise InstallError(
                    f"cannot migrate config: both {old_config} and {new_config} exist and {new_config} is not empty"
                )


def check_migration_conflicts(env: Optional[Mapping[str, str]] = None) -> None:
    """Validate state and config migration paths before any install mutation happens."""
    old_state, new_state = resolve_state_paths(env)
    _check_state_conflict(old_state, new_state)
    old_config, new_config = resolve_config_paths(env)
    _check_config_conflict(old_config, new_config)


def _link_old_to_new(old_path: Path, new_path: Path) -> None:
    same_parent = old_path.parent.exists() and new_path.parent.exists() and _same_resolved(
        old_path.parent, new_path.parent
    )
    switch_symlink(
        old_path,
        new_path if same_parent else new_path.resolve(),
        relative_to=old_path.parent if same_parent else None,
    )


def migrate_state_dir(
    old_state: Optional[Path] = None,
    new_state: Optional[Path] = None,
    *,
    env: Optional[Mapping[str, str]] = None,
) -> Path:
    """Move `old_state` (`junkfather`) to `new_state` (`spawnfather`) under the store flock and leave `old_state` as a
    symlink to `new_state` so live older helpers share the migrated store without recreating a split directory."""
    if old_state is None and new_state is None:
        old_state, new_state = resolve_state_paths(env)
    elif new_state is None:
        _, new_state = resolve_state_paths(env)
    _check_state_conflict(old_state, new_state)
    if old_state is None or (not old_state.exists() and not old_state.is_symlink()):
        return new_state
    if old_state.is_symlink():
        if old_state.exists() and _same_resolved(old_state, new_state):
            return new_state
        if not old_state.exists():
            return new_state
        # Symlink to another directory while new_state is absent or empty: move target or repoint? Move target safely.
        target_dir = old_state.resolve()
        _check_state_conflict(target_dir, new_state)
        migrate_state_dir(target_dir, new_state)
        _link_old_to_new(old_state, new_state)
        return new_state
    if new_state.exists() and not new_state.is_symlink() and _same_resolved(old_state, new_state):
        return new_state

    old_state.mkdir(parents=True, exist_ok=True)
    with open(old_state / "lock", "a", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle, fcntl.LOCK_EX)
        try:
            _check_state_conflict(old_state, new_state)
            if old_state.is_symlink() and old_state.exists() and _same_resolved(old_state, new_state):
                return new_state
            new_state.parent.mkdir(parents=True, exist_ok=True)
            if new_state.is_symlink():
                new_state.unlink()
            elif new_state.exists() and not _dir_nonempty(new_state):
                new_state.rmdir()
            try:
                os.replace(old_state, new_state)
            except OSError:
                shutil.move(str(old_state), str(new_state))
            _link_old_to_new(old_state, new_state)
        finally:
            fcntl.flock(lock_handle, fcntl.LOCK_UN)
    return new_state


def migrate_config_dir(
    old_config: Optional[Path] = None,
    new_config: Optional[Path] = None,
    *,
    env: Optional[Mapping[str, str]] = None,
) -> Path:
    """Migrate `~/.config/junkfather` (or `$XDG_CONFIG_HOME/junkfather`) to `spawnfather` idempotently."""
    if old_config is None and new_config is None:
        old_config, new_config = resolve_config_paths(env)
    elif new_config is None:
        _, new_config = resolve_config_paths(env)
    _check_config_conflict(old_config, new_config)
    if old_config is None or (not old_config.exists() and not old_config.is_symlink()):
        return new_config
    if old_config.is_symlink():
        if not old_config.exists() or _same_resolved(old_config, new_config):
            return new_config
        target_dir = old_config.resolve()
        migrate_config_dir(target_dir, new_config)
        _link_old_to_new(old_config, new_config)
        return new_config
    if new_config.exists() and not new_config.is_symlink() and _same_resolved(old_config, new_config):
        return new_config
    new_config.parent.mkdir(parents=True, exist_ok=True)
    if new_config.is_symlink():
        new_config.unlink()
    elif new_config.exists():
        if not _dir_nonempty(new_config):
            new_config.rmdir()
        elif _identical_single_settings(old_config, new_config):
            shutil.rmtree(old_config)
            _link_old_to_new(old_config, new_config)
            return new_config
    try:
        os.replace(old_config, new_config)
    except OSError:
        shutil.move(str(old_config), str(new_config))
    _link_old_to_new(old_config, new_config)
    return new_config


# Release staging & launchers

def install(share: Path, bin_dir: Path, python: str, *, built: Optional[str] = None) -> Path:
    head = built or commit()
    release_name = f"{version()}-{head[:12]}"

    def populate(pending: Path) -> None:
        copy_python_tree(SOURCE / "spawnfather", pending / "spawnfather")
        copy_python_tree(COMMON, pending / COMMON.name)
        shutil.copy2(SOURCE / "CHANGELOG.md", pending / "CHANGELOG.md")
        (pending / "spawnfather" / "COMMIT").write_text(head + "\n", encoding="utf-8")

    launcher_text = LAUNCHER.format(python=python, current=str(share / "current"))
    shim_text = LEGACY_SHIM.format(python=python, spawnfather=str(bin_dir / "spawnfather"))

    with install_lock(share / ".install.lock"):
        release = stage_release(share / "releases", release_name, populate)
        switch_symlink(share / "current", release, relative_to=share)
        swap_commands(
            bin_dir,
            [
                ("spawnfather", lambda target: write_launcher(target, launcher_text, 0o755)),
                ("junkfather", lambda target: write_launcher(target, shim_text, 0o755)),
            ],
        )
        releases = sorted(
            (p for p in (share / "releases").iterdir() if not p.name.startswith(".")),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old in releases[KEEP_RELEASES:]:
            if old.resolve() != release.resolve():
                shutil.rmtree(old, ignore_errors=True)
    return release


# Launchd

def launchd(command: Path) -> Path:
    agents = _home() / "Library" / "LaunchAgents"
    plist = agents / f"{LABEL}.plist"
    old_plist = agents / f"{LEGACY_LABEL}.plist"
    text = subprocess.run([str(command), "sweep", "--plist"], capture_output=True, text=True, check=True).stdout
    write_atomic(plist, text, 0o644)
    domain = f"gui/{os.getuid()}"
    # Retire legacy com.junkfather.sweep before bootstrapping com.spawnfather.sweep so two sweeps never overlap.
    subprocess.run(["launchctl", "bootout", f"{domain}/{LEGACY_LABEL}"], capture_output=True)
    old_plist.unlink(missing_ok=True)
    subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], capture_output=True)
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


install_launchd = launchd


# Mastack & Claude Code hook registration

def _match_hook_command(raw: Any) -> Tuple[Optional[str], Optional[str]]:
    """Classify `raw` if it is an exact Spawnfather/Junkfather hook command: returns `('hook' | 'direct', binary_path)`
    or `(None, None)` for any unrelated command."""
    if not isinstance(raw, str):
        return None, None
    try:
        words = shlex.split(raw)
    except ValueError:
        return None, None
    if len(words) == 2 and words[1] == "hook" and os.path.basename(words[0]) in TOOL_NAMES:
        return "hook", words[0]
    if (
        len(words) == 4
        and words[:3] == ["sh", "-c", DIRECT_HOOK_BODY]
        and os.path.basename(words[3]) in TOOL_NAMES
    ):
        return "direct", words[3]
    return None, None


def _is_our_allow_rule(raw: Any) -> bool:
    if not isinstance(raw, str):
        return False
    matched = ALLOW_RE.match(raw)
    if not matched:
        return False
    try:
        words = shlex.split(matched.group(1))
    except ValueError:
        return False
    return len(words) == 1 and os.path.basename(words[0]) in TOOL_NAMES


def allow_rule(command: Path) -> str:
    """The permission rule that lets the line the hook puts before a background command run without a prompt: it
    starts with the command as Mastack runs it (background.adopting_line). It covers that line only: Claude Code
    judges each line of the call on its own."""
    return f"Bash({shlex.quote(str(command))} background:*)"


def mastack(command: Path, *, enable_rewriter: bool = False) -> bool:
    """Update Spawnfather's lifecycle entries in Mastack's `hooks.json` and run Mastack's installer.
    Preserves the disabled background rewriter (#176): a `"rewrite": true` entry is written only if one was already
    present in `hooks.json` or `enable_rewriter=True`. Returns True when the background rewriter is active."""
    config = Path(os.environ.get("MASTACK_CONFIG") or _home() / ".config" / "mastack").expanduser()
    path = (config / "hooks.json").resolve()  # a symlinked settings file stays a symlink
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        raise InstallError(f"no {path}: install Mastack's hooks first") from None
    except ValueError as error:
        raise InstallError(f"{path} is not JSON: {error}") from None
    if not isinstance(data, dict):
        raise InstallError(f"{path} is not a JSON object")
    root_obj = cast(Dict[str, Any], data)
    found = root_obj.setdefault("lifecycle", {})
    if not isinstance(found, dict):
        raise InstallError(f"`lifecycle` in {path} is not an object")
    lifecycle = cast(Dict[str, Any], found)
    hook = HOOK.format(command=shlex.quote(str(command)))

    for event in EVENTS:
        found_entries = lifecycle.setdefault(event, [])
        if not isinstance(found_entries, list):
            raise InstallError(f"`lifecycle.{event}` in {path} is not a list")
        raw_entries = cast(List[Any], found_entries)
        updated_event: List[Any] = []
        placed = False
        for item in raw_entries:
            kind, _ = _match_hook_command(item)
            if kind is not None:
                if not placed:
                    updated_event.append(hook)
                    placed = True
            else:
                updated_event.append(item)
        if not placed:
            updated_event.append(hook)
        raw_entries[:] = updated_event

    found_observers = lifecycle.setdefault("PreToolUse", [])
    if not isinstance(found_observers, list):
        raise InstallError(f"`lifecycle.PreToolUse` in {path} is not a list")
    observers = cast(List[Any], found_observers)
    observer: Dict[str, Any] = {"command": hook, "contains": ["spawnfather", "junkfather"]}
    had_observer = False
    had_rewriter = False
    old_binaries: List[str] = []
    old_replaces: List[str] = []
    cleaned_pre: List[Any] = []

    for item in observers:
        if isinstance(item, dict):
            entry_dict = cast(Dict[str, Any], item)
            kind, old_bin = _match_hook_command(entry_dict.get("command"))
            if kind is not None:
                if old_bin:
                    old_binaries.append(old_bin)
                if entry_dict.get("rewrite"):
                    had_rewriter = True
                    for rep in cast(List[Any], entry_dict.get("replaces") or []):
                        if isinstance(rep, str):
                            old_replaces.append(rep)
                    continue
                if not had_observer:
                    cleaned_pre.append(observer)
                    had_observer = True
                continue
        cleaned_pre.append(item)

    if not had_observer:
        cleaned_pre.append(observer)

    rewriter_active = had_rewriter or enable_rewriter
    if rewriter_active:
        replaces = list(
            dict.fromkeys(
                [
                    *old_replaces,
                    *(DIRECT_HOOK.format(command=shlex.quote(b)) for b in old_binaries),
                    DIRECT_HOOK.format(command=shlex.quote(str(command.with_name("junkfather")))),
                    DIRECT_HOOK.format(command=shlex.quote(str(command))),
                ]
            )
        )
        rewriter: Dict[str, Any] = {
            "command": hook,
            "background": True,
            "rewrite": True,
            "allow": [allow_rule(command)],
            "replaces": replaces,
        }
        cleaned_pre.append(rewriter)

    observers[:] = cleaned_pre
    write_atomic(path, json.dumps(root_obj, indent=2, ensure_ascii=False) + "\n", path.stat().st_mode & 0o7777)
    mastack_root = Path(os.environ.get("MASTACK_ROOT") or _home() / "mastack").expanduser()
    result = subprocess.run([str(mastack_root / "bin" / "install"), "--only", "hooks"], text=True)
    if result.returncode:
        raise InstallError("Mastack's installer failed; its message is above")
    return rewriter_active


install_mastack_hooks = mastack


def _rewrite_claude_event_list(
    groups: List[Any],
    command: Path,
    *,
    ensure_present: bool,
    matcher: Optional[str] = None,
) -> bool:
    """Rewrite exact old `junkfather`/`spawnfather` command hooks inside a Claude `settings.json` event list.
    Returns True if at least one hook for our tool is present after rewriting."""
    quoted = shlex.quote(str(command))
    hook = HOOK.format(command=quoted)
    direct = DIRECT_HOOK.format(command=quoted)
    found_any = False

    for group in groups:
        if not isinstance(group, dict):
            continue
        group_dict = cast(Dict[str, Any], group)
        # Support both nested `{"matcher": ..., "hooks": [{"type": "command", "command": ...}]}` and flat command dicts
        nested = group_dict.get("hooks")
        if isinstance(nested, list):
            nested_list = cast(List[Any], nested)
            new_nested: List[Any] = []
            seen_in_group: set[str] = set()
            for item in nested_list:
                if isinstance(item, dict):
                    item_dict = cast(Dict[str, Any], item)
                    kind, _ = _match_hook_command(item_dict.get("command"))
                    if kind is not None:
                        replacement = direct if kind == "direct" else hook
                        found_any = True
                        if replacement not in seen_in_group:
                            seen_in_group.add(replacement)
                            new_nested.append({**item_dict, "command": replacement})
                        continue
                new_nested.append(item)
            nested_list[:] = new_nested
        else:
            kind, _ = _match_hook_command(group_dict.get("command"))
            if kind is not None:
                group_dict["command"] = direct if kind == "direct" else hook
                found_any = True

    if ensure_present and not found_any:
        new_group: Dict[str, Any] = {"hooks": [{"type": "command", "command": hook}]}
        if matcher is not None:
            new_group["matcher"] = matcher
        groups.append(new_group)
        found_any = True
    return found_any


def claude_hooks(command: Path, *, enable_rewriter: bool = False) -> Path:
    """Register or migrate Spawnfather's hooks directly in `~/.claude/settings.json` (`$CLAUDE_CONFIG_DIR` moves it).
    Rewrites exact old `junkfather` lifecycle/direct-hook entries and permission rules in place while preserving
    symlinks, unrelated hooks, and the disabled background rewriter (never adding a new `PreToolUse` rewriter or
    background allow rule unless one was already present or `enable_rewriter=True`)."""
    claude_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or _home() / ".claude").expanduser()
    settings_link = claude_dir / "settings.json"
    target = settings_link.resolve() if settings_link.is_symlink() else settings_link
    mode = 0o600
    if target.exists():
        mode = target.stat().st_mode & 0o7777
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except ValueError as error:
            raise InstallError(f"{target} is not JSON: {error}") from None
        if not isinstance(raw, dict):
            raise InstallError(f"{target} is not a JSON object")
        data = cast(Dict[str, Any], raw)
    else:
        data = {}

    hooks_obj = data.setdefault("hooks", {})
    if not isinstance(hooks_obj, dict):
        raise InstallError(f"`hooks` in {target} is not an object")
    hooks = cast(Dict[str, Any], hooks_obj)

    for event in EVENTS:
        event_list = hooks.setdefault(event, [])
        if not isinstance(event_list, list):
            raise InstallError(f"`hooks.{event}` in {target} is not a list")
        _rewrite_claude_event_list(cast(List[Any], event_list), command, ensure_present=True)

    pre_list = hooks.get("PreToolUse")
    if isinstance(pre_list, list):
        _rewrite_claude_event_list(
            cast(List[Any], pre_list),
            command,
            ensure_present=enable_rewriter,
            matcher="Bash",
        )
    elif enable_rewriter:
        created_pre: List[Any] = []
        _rewrite_claude_event_list(created_pre, command, ensure_present=True, matcher="Bash")
        hooks["PreToolUse"] = created_pre

    permissions = data.get("permissions")
    rule = allow_rule(command)
    if isinstance(permissions, dict):
        perm_dict = cast(Dict[str, Any], permissions)
        allow_list = perm_dict.get("allow")
        if isinstance(allow_list, list):
            rewritten_allow: List[Any] = []
            placed_rule = False
            for entry in cast(List[Any], allow_list):
                if _is_our_allow_rule(entry):
                    if not placed_rule:
                        rewritten_allow.append(rule)
                        placed_rule = True
                else:
                    rewritten_allow.append(entry)
            if enable_rewriter and not placed_rule:
                rewritten_allow.append(rule)
            allow_list[:] = rewritten_allow
    elif enable_rewriter:
        data["permissions"] = {"allow": [rule]}

    write_atomic(target, json.dumps(data, indent=2, ensure_ascii=False) + "\n", mode)
    return settings_link


install_claude_hooks = claude_hooks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--python", help="the interpreter the command runs on (default: this one)")
    parser.add_argument("--bin", type=Path, default=_home() / ".local/bin")
    parser.add_argument("--share", type=Path, default=_home() / ".local/share/spawnfather")
    parser.add_argument("--launchd", action="store_true", help="install and load the hourly sweep agent")
    parser.add_argument("--mastack", action="store_true", help="register the hooks through Mastack")
    parser.add_argument(
        "--claude-hooks",
        action="store_true",
        help="register or migrate hooks in ~/.claude/settings.json",
    )
    parser.add_argument(
        "--enable-background-rewriter",
        action="store_true",
        help="enable the PreToolUse background Bash rewriter when registering hooks",
    )
    args = parser.parse_args(argv)
    args.bin = args.bin.expanduser().resolve()  # the hook and the permission rule name the command absolutely
    args.share = args.share.expanduser().resolve()
    try:
        python = interpreter(args.python)
        check_migration_conflicts()
        release = install(args.share, args.bin, python)
        migrate_state_dir()
        migrate_config_dir()
        spawnfather_bin = args.bin / "spawnfather"
        print(f"Installed {spawnfather_bin} (and legacy {args.bin / 'junkfather'} shim) for {python}: {release}")
        if args.launchd or (args.mastack and sys.platform == "darwin"):
            print(f"Loaded {launchd(spawnfather_bin)}")
        if args.mastack:
            rewriter_on = mastack(spawnfather_bin, enable_rewriter=args.enable_background_rewriter)
            if rewriter_on:
                print(f"Asked Mastack for the hooks and the permission rule {allow_rule(spawnfather_bin)}")
            else:
                print("Asked Mastack for the lifecycle hooks (background rewriter left disabled)")
        if args.claude_hooks:
            updated = claude_hooks(spawnfather_bin, enable_rewriter=args.enable_background_rewriter)
            print(f"Updated Claude Code hooks in {updated}")
    except (InstallError, OSError, subprocess.CalledProcessError) as error:
        print(f"install: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
