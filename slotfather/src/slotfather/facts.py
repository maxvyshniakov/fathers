"""Facts a request records about what it checks (fathers#97): the git HEAD and tree of its directory, so that
repeated checks of one tree can be found in the history."""
from __future__ import annotations

import os
import re
import subprocess
from typing import Any

from . import config as _config

GIT_TIMEOUT = 1.0  # seconds for each git call; a slow or hung git costs the request nothing more
SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def git(cwd: str) -> dict[str, Any] | None:
    """{"head", "tree", "dirty"} of the repository `cwd` is in, or None outside one, in one without commits, where
    git is missing or slow, or with SLOTFATHER_GIT_FACTS=0. `tree` is HEAD's tree; `dirty` says tracked files differ
    from it (staged or not; untracked files are not looked at), so only a clean entry checked HEAD's tree. Takes no
    index lock (GIT_OPTIONAL_LOCKS=0) and never raises."""
    if not cwd or (_config.setting("GIT_FACTS") or "1") == "0":
        return None
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
    try:
        found = subprocess.run(["git", "-C", cwd, "rev-parse", "-q", "HEAD", "HEAD^{tree}"], capture_output=True,
                               text=True, timeout=GIT_TIMEOUT, env=env, stdin=subprocess.DEVNULL)
        lines = found.stdout.split()
        if found.returncode or len(lines) != 2 or not all(SHA.fullmatch(line) for line in lines):
            return None
        # status refreshes stale stat data in memory (diff-index would call a touched file changed) and, with
        # GIT_OPTIONAL_LOCKS=0, writes nothing
        changed = subprocess.run(["git", "-C", cwd, "status", "--porcelain", "--untracked-files=no",
                                  "--ignore-submodules=dirty"], capture_output=True, timeout=GIT_TIMEOUT, env=env,
                                 stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError):
        return None
    if changed.returncode:
        return None
    return {"head": lines[0], "tree": lines[1], "dirty": bool(changed.stdout.strip())}
