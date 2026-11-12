"""The configured reviewer: model choice, real CLI adapters and one fallback, without lane state or GitHub writes.

Review receipts and their lifecycle belong to review.py. This module needs a Project config, the frozen checkout
and a request; it owns how Codex and Claude receive that request and how their answer is interpreted.
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import tomllib
from typing import Any, cast

from .config import Config
from .gitops import git

WEEK_MINUTES = 10080
LIMIT_TIMEOUT = 20
REVIEW_TIMEOUT = 30 * 60
OPUS_TOOLS = "Read,Grep,Glob,Bash(git diff:*),Bash(git log:*),Bash(git show:*)"
MAX_INLINE_DIFF = 400_000
MAX_FINDINGS = 8
SEVERITIES = ("P1", "P2", "P3")
# package-misuse: a use of a dependency package against its documented API; the friction journal counts these.
CATEGORIES = ("bug", "standards", "docs", "package-misuse")
DEFAULT_MODELS = {"sol": "gpt-6.1-sol", "sol_effort": "medium", "sol_min_left": 15,
                  "opus": "claude-opus-5-5", "opus_effort": "medium"}


@dataclass(frozen=True)
class Request:
    standards: str
    subject: str
    base: str
    tip: str


@dataclass(frozen=True)
class Answer:
    evidence: dict[str, Any]
    error: str | None = None


class Reviewer:
    """One project's reviewer. run returns complete selection evidence on success or failure."""

    def __init__(self, project: Config):
        self.project = project
        self.models = _review_models(project)

    def run(self, label: str, checkout: str, request: Request, output: Path, log: Path) -> Answer:
        models = self.models
        sol, opus = models["sol"], models["opus"]
        evidence: dict[str, Any] = {"effort": models["sol_effort"]}
        try:
            codex = _codex_cli(self.project)
            limit = _codex_limit(codex) if codex else {"left": None, "blocked": None}
            evidence.update(codex=codex, codex_left=limit["left"])
            fallback = (f"no Codex CLI ({self.project.env_names('CODEX')}, CODEX_CLI_PATH, codex on PATH)"
                        if not codex
                        else limit["blocked"] or (f"Codex has {limit['left']}% of the weekly limit left, under "
                                                  f"{models['sol_min_left']}%"
                                                  if limit["left"] < models["sol_min_left"] else None))
            text = ""
            if fallback is None and codex:
                effort = models["sol_effort"]
                evidence.update(reviewer="sol", model=sol, effort=effort)
                print(f"lane: review {label}: {sol} {effort} ({limit['left']}% of the weekly Codex "
                      f"limit left) on {request.base[:10]}...{request.tip[:10]}", flush=True)
                output.unlink(missing_ok=True)
                status = _run_reviewer([codex, "exec", "-C", checkout, "-s", "read-only", "-m", sol, "-c",
                                        f'model_reasoning_effort="{effort}"', "--ephemeral", "-o", str(output),
                                        "-"], checkout, _review_prompt(self.project, request, None), log, log)
                text = output.read_text(errors="replace") if output.exists() else ""
                if status or not text.strip():
                    fallback = (f"the Sol run failed (exit {status}): {_last_lines(log)}" if status
                                else "Sol returned an empty review")
            evidence["fallback"] = fallback
            if fallback is not None:
                effort = models["opus_effort"]
                evidence.update(reviewer="opus", model=opus, effort=effort)
                print(f"lane: review {label}: {opus} {effort} ({fallback})", flush=True)
                claude = _claude_cli(self.project)
                if not claude:
                    return Answer(evidence, f"no claude CLI on PATH ({self.project.env_names('CLAUDE')})")
                diff = git(checkout, "diff", f"{request.base}...{request.tip}")
                if len(diff) > MAX_INLINE_DIFF:
                    diff = diff[:MAX_INLINE_DIFF] + "\n[diff truncated: read the rest with git diff]"
                output.unlink(missing_ok=True)
                status = _run_reviewer([claude, "-p", "--model", opus, "--effort", effort,
                                        "--allowedTools", OPUS_TOOLS], checkout,
                                       _review_prompt(self.project, request, diff), output, log)
                text = output.read_text(errors="replace") if output.exists() else ""
                if status or not text.strip():
                    return Answer(evidence, f"the Opus run failed (exit {status}): {_last_lines(log)}"
                                  if status else "Opus returned an empty review")
            evidence["findings"] = _parse_findings(text)
            return Answer(evidence)
        except Exception as error:
            return Answer(evidence, str(error))


def _models_path(project: Config) -> Path | None:
    """LANEFATHER_MODELS, else mastack's models.toml where mastack keeps it."""
    configured = project.env("MODELS")
    candidates = [configured] if configured else ["~/.config/mastack/models.toml", "~/mastack/models.toml"]
    for candidate in candidates:
        path = Path(os.path.expanduser(candidate))
        if path.is_file():
            return path
    return None



def _review_models(project: Config) -> dict[str, Any]:
    """The review role's two choices: the Codex one with its weekly-limit condition, and the Claude one."""
    chosen = dict(DEFAULT_MODELS)
    path = _models_path(project)
    if path is None:
        return chosen
    try:
        data = tomllib.loads(path.read_text())
        models: dict[str, Any] = data["models"]
        for choice in data["roles"]["review"]["choices"]:
            model = models[choice["model"]]
            if model.get("cli") == "codex" and "sol_from" not in chosen:
                when: dict[str, Any] = choice.get("when") or {}
                limits: dict[str, Any] = when.get("limit") or {}
                limit = limits.get("codex_weekly")
                chosen.update(sol=model["id"], sol_effort=choice.get("effort", "medium"), sol_from=str(path),
                              **({"sol_min_left": int(limit)} if isinstance(limit, (int, float)) else {}))
            elif model.get("cli") == "claude" and "opus_from" not in chosen:
                chosen.update(opus=model["id"], opus_effort=choice.get("effort", "medium"), opus_from=str(path))
    except (OSError, KeyError, TypeError, ValueError, tomllib.TOMLDecodeError) as error:
        print(f"lane: warning: {path}: no review role ({error}); reviewing with the defaults", file=sys.stderr)
        return dict(DEFAULT_MODELS)
    return chosen



def _configured_codex() -> str | None:
    """CODEX_CLI_PATH from the Codex config ($CODEX_HOME/config.toml), wherever the ChatGPT app put it."""
    try:
        path = Path(os.path.expanduser(os.environ.get("CODEX_HOME") or "~/.codex")) / "config.toml"
        stack: list[dict[str, Any]] = [tomllib.loads(path.read_text())]
    except Exception:
        return None
    while stack:
        table = stack.pop()
        if isinstance(table.get("CODEX_CLI_PATH"), str):
            return table["CODEX_CLI_PATH"]
        stack += [value for value in table.values() if isinstance(value, dict)]
    return None



def _codex_cli(project: Config) -> str | None:
    """The Codex CLI the review runs: LANEFATHER_CODEX; else CODEX_CLI_PATH from the environment or the Codex
    config, as the ChatGPT app sets it; else codex on PATH. A CLI that cannot run the model fails its run, and Opus
    reviews."""
    explicit = project.env("CODEX")
    if explicit is not None:
        return explicit if explicit and os.access(explicit, os.X_OK) else None
    for candidate in (os.environ.get("CODEX_CLI_PATH"), _configured_codex()):
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    return shutil.which("codex")



def _codex_limit(codex: str) -> dict[str, Any]:
    """The live weekly Codex limit, from `codex app-server` (initialize, initialized, account/rateLimits/read):
    {"left": percent or None, "blocked": why Sol may not run or None}."""
    try:
        child = subprocess.Popen([codex, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, text=True)
    except OSError as error:
        return {"left": None, "blocked": f"codex app-server did not start: {error}"}
    answer: dict[str, Any] = {}

    def read() -> None:
        assert child.stdout is not None
        with child.stdout as stream:
            for line in stream:
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if isinstance(message, dict) and message.get("id") == 2:  # pyright: ignore[reportUnknownMemberType]
                    answer.update(message)  # pyright: ignore[reportUnknownArgumentType]
                    return
    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        assert child.stdin is not None
        for message in ({"id": 1, "method": "initialize",
                         "params": {"clientInfo": {"name": "lanefather-review", "version": "1"}}},
                        {"method": "initialized"}, {"id": 2, "method": "account/rateLimits/read"}):
            child.stdin.write(json.dumps(message) + "\n")
        child.stdin.flush()
        reader.join(LIMIT_TIMEOUT)
    except OSError:
        pass
    finally:
        child.kill()
        child.wait()
        if child.stdin is not None:
            with contextlib.suppress(OSError):
                child.stdin.close()
        reader.join(timeout=1)
    if not answer:
        return {"left": None, "blocked": f"codex app-server gave no rate limits within {LIMIT_TIMEOUT} s"}
    if answer.get("error"):
        return {"left": None, "blocked": f"codex app-server: {answer['error'].get('message', answer['error'])}"}
    result: dict[str, Any] = answer.get("result") or {}
    limits: dict[str, Any] = result.get("rateLimits") or {}
    windows = [cast(dict[str, Any], w) for w in (limits.get("primary"), limits.get("secondary"))
               if isinstance(w, dict)]
    weekly = next((w for w in windows if w.get("windowDurationMins") == WEEK_MINUTES), windows[0] if windows else {})
    used = weekly.get("usedPercent")
    left = int(100 - used) if isinstance(used, (int, float)) else None
    blocked = (f"Codex reports {limits['rateLimitReachedType']}" if limits.get("rateLimitReachedType")
               else "Codex spend control is reached" if limits.get("spendControlReached")
               else "the Codex limit could not be read" if left is None else None)
    return {"left": left, "blocked": blocked, "rate_limits": limits}



def _claude_cli(project: Config) -> str | None:
    return project.env("CLAUDE") or shutil.which("claude")



def _review_prompt(cfg: Config, request: Request, diff: str | None) -> str:
    """The reviewer gets the diff, the ticket and the coding standards; never the lane's report."""
    standards, subject, base, tip = request.standards, request.subject, request.base, request.tip
    lines = [
        f"You review one change to the {cfg.project} repository before it reaches the owner's machine. Report",
        "correctness bugs and breaks of the coding standards below; no style nits and no praise.",
        "",
        f"## Coding standards ({cfg.coding_standards})",
        "",
        standards,
        "",
        "## What the change is for",
        "",
        subject,
        "",
        "## The change",
        "",
        f"The change is `git diff {base}...{tip}` in this checkout (HEAD is {tip}). Read it and the code around it;",
        "do not modify files.",
    ]
    if diff is not None:
        lines += ["", "```diff", diff.rstrip("\n"), "```"]
    lines += [
        "",
        "## Answer",
        "",
        f"List at most {MAX_FINDINGS} findings, most severe first: P1 a bug, P2 a risk, P3 minor. For each give",
        "file:line, the concrete failure scenario and the fix, and a category: bug (wrong behavior), standards (a",
        "break of the coding standards), docs (a stale or wrong doc or changelog) or package-misuse (a use of a",
        "dependency package against its documented API: a selector or DOM query into its internals such as",
        "data-slot or data-dg-* parts, a restyle its lint forbids, an invented or wrong prop or export, or a",
        "workaround for something the package already provides; name the package). End the answer with one",
        "fenced json block that holds",
        "the same findings in the same order:",
        "",
        "```json",
        '{"findings": [{"severity": "P1", "category": "bug", "file": "path/in/repo", "line": 123,',
        '               "title": "one line", "detail": "the failure scenario and the fix",',
        '               "package": "only for package-misuse: the npm name"}]}',
        "```",
        "",
        'With nothing to report, end with {"findings": []}.',
    ]
    return "\n".join(lines) + "\n"



def _parse_findings(text: str) -> list[dict[str, Any]]:
    """The findings of the last json block that has them. An answer without one is a single finding to read, so
    an unreadable review never passes as clean."""
    for block in reversed(re.findall(r"```json[ \t]*\n(.*?)```", text, re.S)):
        try:
            data: Any = json.loads(block)
        except ValueError:
            continue
        items: Any = cast(dict[str, Any], data).get("findings") if isinstance(data, dict) else None
        if isinstance(items, list) and not all(isinstance(i, dict) for i in items):  # pyright: ignore[reportUnknownVariableType]
            return _unreadable_answer(text)
        if isinstance(items, list):
            found: list[dict[str, Any]] = []
            for item in cast(list[Any], items):
                entry = cast(dict[str, Any], item)
                severity = str(entry.get("severity", "")).upper()
                category = str(entry.get("category") or "").strip().lower()
                package = entry.get("package") if category == "package-misuse" else None
                line = entry.get("line")
                found.append({"n": len(found) + 1, "severity": severity if severity in SEVERITIES else "P2",
                              "category": category if category in CATEGORIES else None,
                              # An npm name is at most 214 characters.
                              "package": (package.strip()[:214] if isinstance(package, str) and package.strip()
                                          else None),
                              "file": entry.get("file") if isinstance(entry.get("file"), str) else None,
                              "line": line if isinstance(line, int) else None,
                              "title": str(entry.get("title") or "").strip()[:300],
                              "detail": str(entry.get("detail") or "").strip()[:4000], "mark": None})
            return found
    if re.search(r"\bno findings\b", text, re.I) and len(text) < 400:
        return []
    return _unreadable_answer(text)



def _unreadable_answer(text: str) -> list[dict[str, Any]]:
    return [{"n": 1, "severity": "P2", "category": None, "package": None, "file": None, "line": None, "mark": None,
             "title": "the review answer has no readable findings block: read it and mark it",
             "detail": text.strip()[:4000]}]



def _run_reviewer(argv: list[str], cwd: str, prompt: str, stdout: Path, log: Path) -> int:
    """Run one reviewer with the prompt on stdin; its exit, 124 on the timeout. Stopping this process stops it."""
    with open(stdout, "w") as out, open(log, "a") as err:
        child = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE, stdout=out,
                                 stderr=err if stdout != log else subprocess.STDOUT, text=True)
        try:
            child.communicate(prompt, timeout=REVIEW_TIMEOUT)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
            return 124
        except BaseException:
            child.terminate()
            try:
                child.wait(timeout=30)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
            raise
    return child.returncode



def _last_lines(path: Path, count: int = 3) -> str:
    try:
        lines = [line for line in path.read_text(errors="replace").splitlines() if line.strip()]
    except OSError:
        return ""
    return " / ".join(lines[-count:])[:500]



