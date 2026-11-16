"""The configured reviewer: model choice, real CLI adapters and one fallback, without lane state or GitHub writes.

Review receipts and their lifecycle belong to review.py. This module needs a Project config, the frozen checkout
and a request; it owns how Codex and Claude receive that request and how their answer is interpreted.
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
import tomllib
from typing import Any, cast

from .config import Config
from .gitops import git

WEEK_MINUTES = 10080
LIMIT_TIMEOUT = 20
# `role` reads the limit itself, within its own 20 s.
ROLE_TIMEOUT = LIMIT_TIMEOUT + 15
REVIEW_TIMEOUT = 30 * 60
OPUS_TOOLS = "Read,Grep,Glob,Bash(git diff:*),Bash(git log:*),Bash(git show:*)"
MAX_INLINE_DIFF = 400_000
MAX_FINDINGS = 8
SEVERITIES = ("P1", "P2", "P3")
# package-misuse: a use of a dependency package against its documented API; the friction journal counts these.
# design: a change bolted on beside the structure it lands in, or a copy of behaviour the repository already has.
CATEGORIES = ("bug", "standards", "docs", "package-misuse", "design")
DEFAULT_MODELS = {"sol": "gpt-6.1-sol", "sol_effort": "medium", "sol_min_left": 15,
                  "opus": "claude-opus-5-5", "opus_effort": "medium"}


@dataclass(frozen=True)
class Request:
    standards: str
    subject: str
    base: str
    tip: str
    # The implementer's model family (anthropic, openai) for the role's `not_family`; None when not known.
    family: str | None = None
    # `review --reviewer` (fathers#155): the reviewers this review must run, whatever the Codex limit; () chooses.
    reviewers: tuple[str, ...] = ()


@dataclass(frozen=True)
class Answer:
    evidence: dict[str, Any]
    error: str | None = None


@dataclass(frozen=True)
class Model:
    """One reviewer the models file names: its alias there, model id, CLI (codex or claude) and effort, and the
    share of the weekly Codex limit its review choice needs (`when.limit.codex_weekly`), None without one."""
    alias: str
    id: str
    cli: str
    effort: str
    min_left: int | None = None
    family: str | None = None
    # `when.not_family`: the choice holds only for an implementer of another family (fathers#155).
    not_family: bool = False


class Reviewer:
    """One project's reviewer. run returns complete selection evidence on success or failure."""

    def __init__(self, project: Config):
        self.project = project
        self.models = _review_models(project)

    def run(self, label: str, checkout: str, request: Request, output: Path, log: Path) -> Answer:
        if request.reviewers:
            return self._panel(label, checkout, request, output, log, request.reviewers, "flag")
        if self.project.review_panel:
            return self._panel(label, checkout, request, output, log, self.project.review_panel, "panel")
        models = self.models
        evidence: dict[str, Any] = {"effort": models["sol_effort"]}
        try:
            codex = _codex_cli(self.project)
            first, fallback, second = self._choose(label, codex, request.family, evidence)
            text = ""
            if first is not None and codex:
                evidence.update(reviewer=first.alias, model=first.id, effort=first.effort)
                left = evidence.get("codex_left")
                print(f"lane: review {label}: {first.id} {first.effort} ("
                      + (f"{left}% of the weekly Codex limit left" if left is not None else "the Codex limit")
                      + f") on {request.base[:10]}...{request.tip[:10]}", flush=True)
                output.unlink(missing_ok=True)
                status = _run_reviewer(_codex_argv(codex, first, checkout, output), checkout,
                                       _review_prompt(self.project, request, None), log, log)
                text = output.read_text(errors="replace") if output.exists() else ""
                name = first.alias.capitalize()
                if status or not text.strip():
                    fallback = (f"the {name} run failed (exit {status}): {_last_lines(log)}" if status
                                else f"{name} returned an empty review")
            evidence["fallback"] = fallback
            if fallback is not None:
                evidence.update(reviewer=second.alias, model=second.id, effort=second.effort)
                print(f"lane: review {label}: {second.id} {second.effort} ({fallback})", flush=True)
                claude = _claude_cli(self.project)
                if not claude:
                    return Answer(evidence, f"no claude CLI on PATH ({self.project.env_names('CLAUDE')})")
                output.unlink(missing_ok=True)
                status = _run_reviewer(_claude_argv(claude, second), checkout,
                                       _review_prompt(self.project, request, _inline_diff(checkout, request)),
                                       output, log)
                text = output.read_text(errors="replace") if output.exists() else ""
                name = second.alias.capitalize()
                if status or not text.strip():
                    return Answer(evidence, f"the {name} run failed (exit {status}): {_last_lines(log)}"
                                  if status else f"{name} returned an empty review")
            evidence["findings"] = _parse_findings(text)
            return Answer(evidence)
        except Exception as error:
            return Answer(evidence, str(error))

    def _choose(self, label: str, codex: str | None, family: str | None,
                evidence: dict[str, Any]) -> tuple[Model | None, str | None, Model]:
        """(the Codex reviewer to try first or None, why it does not run or None, the Claude reviewer): Mastack's
        `role review` when the models file has it beside it (fathers#150), else the review role read here."""
        models = self.models
        claude_model = Model("opus", models["opus"], "claude", models["opus_effort"])
        evidence["codex"] = codex
        answer = _role_answer(self.project, codex, family, evidence)
        if answer is not None:
            chosen = Model(str(answer["model"]), str(answer["id"]), str(answer["cli"]),
                           str(answer.get("effort") or "medium"))
            skipped = evidence["role"]["skipped"]
            if skipped or evidence["role"].get("caution"):
                print(f"lane: review {label}: role review chose {chosen.alias}"
                      + (f"; skipped {'; '.join(skipped)}" if skipped else "")
                      + (f"; {evidence['role']['caution']}" if evidence["role"].get("caution") else ""), flush=True)
            if chosen.cli == "codex":
                if not codex:
                    return None, f"no Codex CLI ({self.project.env_names('CODEX')}, CODEX_CLI_PATH, codex on PATH)", \
                        claude_model
                return chosen, None, claude_model
            if chosen.cli == "claude":
                return None, "; ".join(skipped) or f"role review chose {chosen.alias}", chosen
            return None, f"role review chose {chosen.id} on the unknown CLI {chosen.cli}", claude_model
        # Without role: the review role's choices in order, the first whose conditions hold runs (fathers#155).
        _, choices = _models(self.project)
        skipped: list[str] = []
        limit: dict[str, Any] | None = None
        for index, model in enumerate(choices):
            if model.not_family and family and model.family == family:
                skipped.append(f"{model.alias}: same family as the implementer ({family})")
                continue
            if model.cli == "claude":
                return None, "; ".join(skipped) or f"the review role lists {model.alias} first", model
            if model.cli != "codex":
                skipped.append(f"{model.alias} runs on {model.cli}, which Lanefather cannot start")
                continue
            if not codex:
                skipped.append(f"no Codex CLI ({self.project.env_names('CODEX')}, CODEX_CLI_PATH, codex on PATH)")
                continue
            if model.min_left is not None:
                if limit is None:
                    limit = _codex_limit(codex)
                    evidence.update(codex_left=limit["left"])
                why = limit["blocked"] or (f"Codex has {limit['left']}% of the weekly limit left, under "
                                           f"{model.min_left}%" if limit["left"] < model.min_left else None)
                if why:
                    skipped.append(why)
                    continue
            after = next((m for m in choices[index + 1:] if m.cli == "claude"), claude_model)
            return model, None, after
        evidence.setdefault("codex_left", None)
        return None, "; ".join(skipped), claude_model

    def _panel(self, label: str, checkout: str, request: Request, output: Path, log: Path, panel: tuple[str, ...],
               by: str) -> Answer:
        """Every reviewer of the panel at once on the same checkout with the same prompt, each in its own process
        that never sees another's answer (fathers#151). The panel is `[review] panel` (by "panel"): a member whose
        condition fails (a used-up Codex week) is replaced by the role's fallback, or skipped when that fallback is
        already on the panel. Or `review --reviewer` (by "flag", fathers#155): each runs whatever the limit."""
        project = self.project
        forced = by == "flag"
        evidence: dict[str, Any] = {"effort": self.models["sol_effort"],
                                    "forced": {"by": by, "reviewers": list(panel)}}
        try:
            models, choices = _models(project)
            fallback_alias = next((m.alias for m in choices if m.cli == "claude"), None)
            codex, claude = _codex_cli(project), _claude_cli(project)
            evidence["codex"] = codex
            answer = None if forced else _role_answer(project, codex, request.family, evidence)
            if answer is not None:
                fallback_alias = str(answer["model"])
            quota: dict[str, Any] = {}
            quotas: dict[str, Any] = (answer or {}).get("quotas") or {}
            if isinstance(quotas.get("codex_weekly"), dict):
                read: dict[str, Any] = quotas["codex_weekly"]
                quota = {"left": read.get("left"),
                         "blocked": (f"Codex reports {read['reached']}" if read.get("reached")
                                     else f"the Codex limit could not be read: {read['error']}" if read.get("error")
                                     else None if isinstance(read.get("left"), int)
                                     else "the Codex limit could not be read")}

            def unusable(model: Model | None, alias: str) -> str | None:
                if model is None:
                    return f"{alias} is not a model of the models file ({_models_path(project) or 'the defaults'})"
                if model.cli == "codex" and not codex:
                    return f"no Codex CLI ({project.env_names('CODEX')}, CODEX_CLI_PATH, codex on PATH)"
                if model.cli == "claude" and not claude:
                    return f"no claude CLI on PATH ({project.env_names('CLAUDE')})"
                if model.cli not in ("codex", "claude"):
                    return f"{alias} runs on {model.cli}, which Lanefather cannot start"
                if model.min_left is not None and not forced:
                    if not quota:
                        quota.update(_codex_limit(codex) if codex else {"left": None, "blocked": None})
                    if quota.get("blocked"):
                        return str(quota["blocked"])
                    left = quota.get("left")
                    if not isinstance(left, int) or left < model.min_left:
                        return f"Codex has {left}% of the weekly limit left, under {model.min_left}%"
                return None

            entries: list[dict[str, Any]] = []
            runs: list[tuple[dict[str, Any], Model]] = []
            for alias in panel:
                model = models.get(alias)
                why = unusable(model, alias)
                if why is None and model is not None:
                    entry = {"member": alias, "reviewer": alias, "status": "running", "reason": None}
                    runs.append((entry, model))
                else:
                    if forced:  # the asked-for reviewer or none: never a stand-in
                        fallback_alias = None
                    stand_in = models.get(fallback_alias) if fallback_alias else None
                    taken = set(panel) | {e["reviewer"] for e in entries}
                    if (fallback_alias and fallback_alias not in taken and stand_in is not None
                            and unusable(stand_in, fallback_alias) is None):
                        entry = {"member": alias, "reviewer": fallback_alias, "status": "running",
                                 "reason": f"{why}: {fallback_alias} reviews in its place"}
                        runs.append((entry, stand_in))
                    else:
                        entry = {"member": alias, "reviewer": alias, "status": "skipped",
                                 "reason": why if not fallback_alias or fallback_alias == alias
                                 else f"{why}; its fallback {fallback_alias} is already on the panel"
                                 if fallback_alias in taken else f"{why}; its fallback {fallback_alias} cannot run",
                                 **({"model": model.id, "effort": model.effort} if model else {})}
                entries.append(entry)
            evidence.update(codex_left=quota.get("left"), panel=entries)
            for entry in entries:
                if entry["reason"]:
                    print(f"lane: review {label}: panel member {entry['member']}: {entry['reason']}", flush=True)
            if not runs:
                return Answer(evidence, "no panel member can review: "
                              + "; ".join(f"{e['member']}: {e['reason']}" for e in entries))
            prompt = _review_prompt(project, request, _inline_diff(checkout, request))
            prompt_path = output.with_name(f"{output.stem}.prompt.md")
            prompt_path.write_text(prompt)
            evidence["prompt_sha256"] = hashlib.sha256(prompt.encode()).hexdigest()
            jobs: list[tuple[list[str], Path, Path]] = []
            for entry, model in runs:
                answer_path = output.with_name(f"{output.stem}.{entry['reviewer']}.md")
                member_log = output.with_name(f"{output.stem}.{entry['reviewer']}.log")
                answer_path.unlink(missing_ok=True)
                member_log.write_text("")
                entry.update(model=model.id, effort=model.effort, cli=model.cli, output=str(answer_path),
                             log=str(member_log))
                print(f"lane: review {label}: panel {entry['reviewer']}: {model.id} {model.effort} on "
                      f"{request.base[:10]}...{request.tip[:10]}", flush=True)
                if model.cli == "codex" and codex:
                    jobs.append((_codex_argv(codex, model, checkout, answer_path), member_log, member_log))
                else:
                    jobs.append((_claude_argv(claude or "claude", model), answer_path, member_log))
            results = _run_together(jobs, checkout, prompt_path)
            findings: list[dict[str, Any]] = []
            sections: list[str] = []
            for (entry, _model), (status, seconds) in zip(runs, results, strict=True):
                answer_path, member_log = Path(entry["output"]), Path(entry["log"])
                text = answer_path.read_text(errors="replace") if answer_path.exists() else ""
                name = entry["reviewer"].capitalize()
                entry["seconds"] = round(seconds, 1)
                if status or not text.strip():
                    entry.update(status="failed", numbers=[],
                                 error=(f"the {name} run failed (exit {status}): {_last_lines(member_log)}"
                                        if status else f"{name} returned an empty review"))
                    print(f"lane: review {label}: panel {entry['reviewer']} failed: {entry['error']}", flush=True)
                    continue
                entry.update(status="finished", numbers=[])
                for finding in _parse_findings(text):
                    finding.update(n=len(findings) + 1, reviewer=entry["reviewer"])
                    findings.append(finding)
                    entry["numbers"].append(finding["n"])
                sections.append(f"# {entry['reviewer']} ({entry['model']} {entry['effort']})\n\n{text.rstrip()}\n")
            output.write_text("\n".join(sections))
            answered = [e for e in entries if e["status"] == "finished"]
            efforts = sorted({str(e["effort"]) for e in answered})
            evidence.update(reviewer="+".join(e["reviewer"] for e in answered) or None,
                            model=", ".join(str(e["model"]) for e in answered) or None,
                            effort="/".join(efforts) or evidence["effort"],
                            fallback="; ".join(f"{e['member']}: {e.get('error') or e['reason']}" for e in entries
                                               if e["status"] != "finished" or e["reason"]) or None)
            if not answered:
                return Answer(evidence, "no panel member answered: "
                              + "; ".join(f"{e['member']}: {e.get('error') or e['reason']}" for e in entries))
            evidence["findings"] = findings
            return Answer(evidence)
        except Exception as error:
            return Answer(evidence, str(error))


def _codex_argv(codex: str, model: Model, checkout: str, output: Path) -> list[str]:
    return [codex, "exec", "-C", checkout, "-s", "read-only", "-m", model.id, "-c",
            f'model_reasoning_effort="{model.effort}"', "--ephemeral", "-o", str(output), "-"]


def _claude_argv(claude: str, model: Model) -> list[str]:
    return [claude, "-p", "--model", model.id, "--effort", model.effort, "--allowedTools", OPUS_TOOLS]


def _inline_diff(checkout: str, request: Request) -> str:
    """The diff itself, for a reviewer without a shell beyond git (Claude) and for every panel member alike."""
    diff = git(checkout, "diff", f"{request.base}...{request.tip}")
    if len(diff) > MAX_INLINE_DIFF:
        diff = diff[:MAX_INLINE_DIFF] + "\n[diff truncated: read the rest with git diff]"
    return diff


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
    """The review role's first Codex choice with its weekly-limit condition and its first Claude choice: the
    efforts a running receipt names before the reviewer is chosen."""
    chosen = dict(DEFAULT_MODELS)
    _, choices = _models(project)
    sol = next((m for m in choices if m.cli == "codex"), None)
    opus = next((m for m in choices if m.cli == "claude"), None)
    if sol is not None:
        chosen.update(sol=sol.id, sol_effort=sol.effort,
                      **({"sol_min_left": sol.min_left} if sol.min_left is not None else {}))
    if opus is not None:
        chosen.update(opus=opus.id, opus_effort=opus.effort)
    return chosen



def _models(project: Config) -> tuple[dict[str, Model], list[Model]]:
    """The models file: every model by alias (with the effort and limit of its first review choice), and the review
    role's choices in their order, a priority list (fathers#155); the defaults, Sol then Opus, when no file is
    found or it has no readable review role."""
    models: dict[str, Any] = dict(DEFAULT_MODELS)
    sol = Model("sol", str(models["sol"]), "codex", str(models["sol_effort"]), int(models["sol_min_left"]), "openai")
    opus = Model("opus", str(models["opus"]), "claude", str(models["opus_effort"]), None, "anthropic")
    path = _models_path(project)
    if path is None:
        return {"sol": sol, "opus": opus}, [sol, opus]
    try:
        data = tomllib.loads(path.read_text())
        table: dict[str, Any] = data["models"]
        found: dict[str, Model] = {}
        for alias, model in table.items():
            entry = cast(dict[str, Any], model) if isinstance(model, dict) else {}
            if isinstance(entry.get("id"), str) and isinstance(entry.get("cli"), str):
                family = entry.get("family")
                found[alias] = Model(alias, entry["id"], entry["cli"], "medium",
                                     family=family if isinstance(family, str) else None)
        choices: list[Model] = []
        for choice in data["roles"]["review"]["choices"]:
            base = found[choice["model"]]
            when: dict[str, Any] = choice.get("when") or {}
            limits: dict[str, Any] = when.get("limit") or {}
            limit = limits.get("codex_weekly")
            choices.append(Model(base.alias, base.id, base.cli, str(choice.get("effort", "medium")),
                                 int(limit) if isinstance(limit, (int, float)) else None, base.family,
                                 bool(when.get("not_family"))))
        if not choices:
            raise ValueError("the review role lists no choices")
        for model in reversed(choices):
            found[model.alias] = model
        return found, choices
    except (OSError, KeyError, TypeError, ValueError, tomllib.TOMLDecodeError) as error:
        print(f"lane: warning: {path}: no review role ({error}); reviewing with the defaults", file=sys.stderr)
        return {"sol": sol, "opus": opus}, [sol, opus]



def _role_command(project: Config) -> tuple[str, Path] | None:
    """Mastack's `bin/role` beside the models file (fathers#150), and that file, or None (no Mastack)."""
    path = _models_path(project)
    if path is None:
        return None
    command = path.parent / "bin" / "role"
    return (str(command), path) if command.is_file() and os.access(command, os.X_OK) else None



def _role_answer(project: Config, codex: str | None, family: str | None,
                 evidence: dict[str, Any]) -> dict[str, Any] | None:
    """`role review [--implementer-family F] --json`: its choice, with its skip reasons, caution and note in
    evidence["role"]; None without the command, or when it gives no choice (evidence["role_error"] says why), so
    the review role read here decides as before. The role reads the Codex limit with the CLI this review runs."""
    found = _role_command(project)
    if found is None:
        return None
    command, models = found
    argv = [command, "review", *(["--implementer-family", family] if family else []), "--json"]
    env = dict(os.environ, MASTACK_MODELS=str(models), **({"MASTACK_CODEX": codex} if codex else {}))
    try:
        done = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=ROLE_TIMEOUT,
                              stdin=subprocess.DEVNULL)
        answer: Any = json.loads(done.stdout) if done.returncode in (0, 1) else None
    except subprocess.TimeoutExpired:
        answer, done = None, None
    except (OSError, ValueError) as error:
        evidence["role_error"] = f"{command}: {error}"
        print(f"lane: warning: {evidence['role_error']}; choosing the reviewer from {models}", file=sys.stderr)
        return None
    if not isinstance(answer, dict) or not isinstance(answer.get("id"), str):  # pyright: ignore[reportUnknownMemberType]
        why = (f"no answer within {ROLE_TIMEOUT} s" if done is None
               else f"exit {done.returncode}: {(done.stderr or done.stdout).strip()[:300]}")
        evidence["role_error"] = f"{command} review: {why}"
        print(f"lane: warning: {evidence['role_error']}; choosing the reviewer from {models}", file=sys.stderr)
        return None
    chosen = cast(dict[str, Any], answer)
    skipped: list[Any] = chosen.get("skipped") or []
    evidence["role"] = {"command": command, "family": family, "chose": chosen.get("model"),
                        "skipped": [str(cast(dict[str, Any], s).get("reason")) for s in skipped
                                    if isinstance(s, dict)],
                        "caution": chosen.get("caution"), "note": chosen.get("note")}
    quotas: Any = chosen.get("quotas") or {}
    weekly: Any = cast(dict[str, Any], quotas).get("codex_weekly") if isinstance(quotas, dict) else None
    evidence["codex_left"] = cast(dict[str, Any], weekly).get("left") if isinstance(weekly, dict) else None
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
        "",
        "## Design",
        "",
        "Besides bugs and the standards, answer two questions about the change, each as a finding when the answer",
        "is not a plain yes and no:",
        "",
        "1. Does the change fit the structure it lands in, or is it bolted on beside it, and which refactor would",
        "   make it fit?",
        "2. Does it add a copy of behaviour the repository already has? Name the existing code.",
    ]
    if diff is not None:
        lines += ["", "```diff", diff.rstrip("\n"), "```"]
    lines += [
        "",
        "## Answer",
        "",
        f"List at most {MAX_FINDINGS} findings, most severe first: P1 a bug, P2 a risk, P3 minor. For each give",
        "file:line, the concrete failure scenario and the fix, and a category: bug (wrong behavior), standards (a",
        "break of the coding standards), docs (a stale or wrong doc or changelog), design (an answer to one of",
        "the design questions: name the structure or the existing code) or package-misuse (a use of a",
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



def _run_together(jobs: list[tuple[list[str], Path, Path]], cwd: str, prompt: Path) -> list[tuple[int, float]]:
    """Start every (argv, stdout, log) at once, each reading the same prompt file, and wait for all: each one's
    exit (124 on the timeout, 127 when it cannot start) and its seconds. Stopping this process stops them all."""
    children: list[subprocess.Popen[str] | None] = []
    results: list[tuple[int, float]] = [(124, 0.0)] * len(jobs)
    clock = time.monotonic()

    def wait(index: int, child: subprocess.Popen[str]) -> None:
        try:
            child.wait(timeout=REVIEW_TIMEOUT)
            results[index] = (child.returncode, time.monotonic() - clock)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
            results[index] = (124, time.monotonic() - clock)

    try:
        for index, (argv, stdout, log) in enumerate(jobs):
            try:
                with open(prompt) as stdin, open(stdout, "a") as out, open(log, "a") as err:
                    children.append(subprocess.Popen(argv, cwd=cwd, stdin=stdin, stdout=out,
                                                     stderr=err if stdout != log else subprocess.STDOUT, text=True))
            except OSError as error:
                with open(log, "a") as err:
                    err.write(f"{argv[0]} did not start: {error}\n")
                children.append(None)
                results[index] = (127, 0.0)
        waiters = [threading.Thread(target=wait, args=(i, c), daemon=True) for i, c in enumerate(children) if c]
        for waiter in waiters:
            waiter.start()
        for waiter in waiters:
            waiter.join()
    except BaseException:
        running = [c for c in children if c is not None and c.poll() is None]
        for child in running:
            child.terminate()
        for child in running:
            try:
                child.wait(timeout=30)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        raise
    return results



def _last_lines(path: Path, count: int = 3) -> str:
    try:
        lines = [line for line in path.read_text(errors="replace").splitlines() if line.strip()]
    except OSError:
        return ""
    return " / ".join(lines[-count:])[:500]



