"""GitHub CLI (`gh`) adapter (fathers#74): one subprocess seam for issue view, comment, create and label."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
import re
import subprocess
from typing import Any, cast

from . import config
from .gitops import git, main_checkout


def gh_command(project: config.Config | None = None) -> str:
    """The configured `gh` command (`LANEFATHER_GH`), defaulting to `gh`."""
    return (project or config.DEFAULT).env("GH") or "gh"


def origin_repo(common: str) -> str | None:
    """`owner/name` from the `origin` remote's GitHub URL, else None."""
    origin = git(main_checkout(common), "remote", "get-url", "origin", check=False)
    found = re.search(r"github\.com[:/]([^/]+/[^/.]+)", origin)
    return found.group(1) if found else None


def repo_args(common: str) -> list[str]:
    """`-R owner/name` for `gh`, from the origin remote; `[]` when origin is not on GitHub."""
    repo = origin_repo(common)
    return ["-R", repo] if repo else []


def repository(common: str, rule: config.Tickets | None = None) -> str | None:
    """`owner/name` for `gh`: the configured `[tickets] repo`, else `origin`'s GitHub repository."""
    active_rule = rule or config.DEFAULT.tickets
    if active_rule.repo:
        return active_rule.repo
    return origin_repo(common)


def run_gh(
    args: Sequence[str],
    *,
    project: config.Config | None = None,
    repo: str | None = None,
    common: str | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
    stdin_devnull: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run `gh` with the given arguments, repository target, environment and timeout."""
    argv = [gh_command(project), *args]
    if repo:
        argv.extend(["--repo", repo])
    elif common:
        argv.extend(repo_args(common))
    return subprocess.run(
        argv,
        stdin=subprocess.DEVNULL if stdin_devnull else None,
        capture_output=True,
        text=True,
        env=dict(env) if env is not None else None,
        timeout=timeout,
    )


def issue_view(
    number: str,
    *,
    json_fields: str,
    common: str | None = None,
    repo: str | None = None,
    project: config.Config | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """View an issue's JSON fields via `gh issue view`; returns `(data, stderr)`."""
    result = run_gh(
        ["issue", "view", number, "--json", json_fields],
        project=project,
        repo=repo,
        common=common,
        env=env,
        timeout=timeout,
    )
    if result.returncode != 0:
        return None, result.stderr.strip()
    try:
        parsed = json.loads(result.stdout)
    except ValueError:
        return None, result.stderr.strip()
    return (cast(dict[str, Any], parsed), "") if isinstance(parsed, dict) else (None, result.stderr.strip())


def issue_create(
    title: str,
    body: str,
    *,
    common: str | None = None,
    repo: str | None = None,
    project: config.Config | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
) -> tuple[str | None, str | None]:
    """Create an issue via `gh issue create`; returns `(issue_url, error)`."""
    result = run_gh(
        ["issue", "create", "--title", title, "--body", body],
        project=project,
        repo=repo,
        common=common,
        env=env,
        timeout=timeout,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip().splitlines()[-1], None
    return None, ((result.stderr or result.stdout).strip() or f"gh exit {result.returncode}")[:300]


def _run_status(
    args: Sequence[str],
    *,
    repo: str,
    project: config.Config | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
    stdin_devnull: bool = True,
) -> str | None:
    try:
        result = run_gh(
            args,
            project=project,
            repo=repo,
            env=env,
            timeout=timeout,
            stdin_devnull=stdin_devnull,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return f"gh {' '.join(args[:2])}: {error}"
    if result.returncode:
        return f"gh {' '.join(args[:2])} exited {result.returncode}: {(result.stderr or result.stdout).strip()}"
    return None


def issue_comment(
    repo: str,
    number: str,
    body: str,
    *,
    project: config.Config | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
) -> str | None:
    """Comment on an issue; None on success, else error string."""
    return _run_status(
        ["issue", "comment", number, "--body", body],
        project=project,
        repo=repo,
        env=env,
        timeout=timeout,
    )


def issue_edit(
    repo: str,
    number: str,
    *args: str,
    project: config.Config | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
) -> str | None:
    """Edit an issue (e.g. `--add-label` / `--remove-label`); None on success, else error string."""
    return _run_status(["issue", "edit", number, *args], project=project, repo=repo, env=env, timeout=timeout)


def label_create(
    repo: str,
    name: str,
    *,
    color: str,
    description: str,
    project: config.Config | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
) -> str | None:
    """Create a repository label; None on success, else error string."""
    return _run_status(
        ["label", "create", name, "--color", color, "--description", description],
        project=project,
        repo=repo,
        env=env,
        timeout=timeout,
    )
