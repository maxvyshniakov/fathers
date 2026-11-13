"""The project registry: every project whose configuration a command on this machine loaded, so `lanefather status`
reports all of them from anywhere.

  ~/.local/state/lanefather/projects.json   {"v": 1, "projects": [{"state_dir", "common_dir", "config", "repository",
                                                                    "project", "seen"}]}

`LANEFATHER_REGISTRY` names another file. One entry per state directory and repository (its Git common directory):
projects that keep the default state_dir share the directory, and lane ids repeat across projects. A command in a
lane's worktree registers the main checkout's configuration. A command refreshes its project's entry at most every
ten minutes, under an `flock` on `projects.lock`, with an atomic rename; the read-only commands (capabilities, status,
settings, show) never write it.
An entry whose configuration file is missing and that no command has seen for 7 days is dropped. A project whose state
directory is in a temporary directory (a test's) is not registered unless `LANEFATHER_REGISTRY` names the file.
"""
from __future__ import annotations

import contextlib
import datetime
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any, cast

from . import config, events, journal, state
from .gitops import main_checkout

VERSION = 1
REFRESH = 600
FORGET = 7 * 86400
READ_ONLY = ("capabilities", "status", "settings", "show", "stats", "changelog")


def path() -> Path:
    configured = os.environ.get("LANEFATHER_REGISTRY")
    return Path(os.path.expanduser(configured)) if configured else events.machine_dir() / "projects.json"


def read(registry: Path | None = None) -> list[dict[str, Any]]:
    """The registered projects; a missing or unreadable registry has none."""
    data = state.read_json(registry or path())
    if data is None or data.get("v") != VERSION or not isinstance(data.get("projects"), list):
        return []
    entries = cast(list[Any], data["projects"])
    return [cast(dict[str, Any], entry) for entry in entries
            if isinstance(entry, dict) and isinstance(cast(dict[str, Any], entry).get("state_dir"), str)]


def repository(root: Path) -> str | None:
    """owner/name of the GitHub origin of the checkout at `root`, without a network call."""
    try:
        origin = subprocess.run(["git", "-C", str(root), "remote", "get-url", "origin"], capture_output=True,
                                text=True, timeout=2).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.fullmatch(r"(?:git@github\.com:|https://github\.com/|ssh://git@github\.com/)([\w.-]+/[\w.-]+?)"
                         r"(?:\.git)?/?", origin)
    return match[1] if match else None


def age(entry: dict[str, Any], now: datetime.datetime) -> float:
    """Seconds since a command last saw the entry's project; an unreadable time counts as long ago."""
    try:
        return (now - journal.parse_time(str(entry.get("seen")))).total_seconds()
    except ValueError:
        return float("inf")


def forgotten(entry: dict[str, Any], now: datetime.datetime) -> bool:
    """A project whose configuration file (or, without one, whose repository) is gone and that no command has seen
    for 7 days."""
    configured, common = entry.get("config"), entry.get("common_dir")
    missing = (not Path(configured).is_file() if isinstance(configured, str)
               else isinstance(common, str) and not Path(common).is_dir())
    return missing and age(entry, now) > FORGET


def checkout(project: config.Config) -> tuple[str | None, Path | None]:
    """The Git common directory of the project's repository and its configuration in the main checkout: a command
    run in a lane's worktree registers the project, not the worktree."""
    where = project.path.parent if project.path is not None else Path.cwd()
    try:
        found = subprocess.run(["git", "-C", str(where), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                               capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return None, project.path
    if found.returncode:
        return None, project.path
    common = found.stdout.strip()
    main = Path(main_checkout(common))
    if project.path is not None and (main / project.path.name).is_file():
        return common, main / project.path.name
    return common, project.path


def note(project: config.Config) -> None:
    """Record or refresh `project` as a command loads it. Never fails or stalls the command: a command outside a
    Git repository registers nothing, and a registry lock held for more than a second skips the refresh."""
    with contextlib.suppress(Exception):
        directory = state.Store(project).directory.expanduser().absolute()
        registry = path()
        if events.temporary(directory) and not os.environ.get("LANEFATHER_REGISTRY"):
            return
        now = datetime.datetime.now(datetime.UTC)
        known = read(registry)
        stale = any(forgotten(entry, now) for entry in known)
        # The common case without a git call: a fresh entry for this state directory and configuration file.
        if project.path is not None and not stale and any(
                entry["state_dir"] == str(directory) and entry.get("config") == str(project.path)
                and age(entry, now) < REFRESH for entry in known):
            return
        common, configured_path = checkout(project)
        if common is None:
            return
        configured = str(configured_path) if configured_path is not None else None
        key = (str(directory), common)
        name = journal.project_name(project)
        found = next((entry for entry in known if identity(entry) == key), None)
        if (found is not None and found.get("config") == configured and found.get("project") == name
                and age(found, now) < REFRESH and not stale):
            return
        registry.parent.mkdir(parents=True, exist_ok=True)
        with open(registry.with_name("projects.lock"), "a") as lock:
            if not events.hold(lock, events.LOCK_WAIT):
                return
            entries = [entry for entry in read(registry) if identity(entry) != key and not forgotten(entry, now)]
            entries.append({"state_dir": key[0], "common_dir": common, "config": configured, "project": name,
                            "repository": repository(Path(main_checkout(common))),
                            "seen": now.strftime(journal.LEDGER_TIME)})
            entries.sort(key=lambda entry: (entry["state_dir"], str(entry.get("common_dir"))))
            # 0600 as the temporary file it was written through before fathers#129.
            state.atomic_write(registry, json.dumps({"v": VERSION, "projects": entries}, indent=2) + "\n", mode=0o600)


def identity(entry: dict[str, Any]) -> tuple[str, str | None]:
    """A project is its state directory and its repository: projects that leave state_dir at the default share
    the directory, and lane ids repeat across projects."""
    common = entry.get("common_dir")
    return str(entry["state_dir"]), common if isinstance(common, str) else None
