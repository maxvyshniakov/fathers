"""The configured reviewer: model choice, real CLI adapters and the fallback, without lane state or GitHub writes.

Review receipts and their lifecycle belong to review.py. This module needs a Project config, the frozen checkout
and a request; it owns how Codex and Claude receive that request and how their answer is interpreted.

One pipeline serves a single review and a panel (fathers#151): `Conditions` says whether a model may review now
(Mastack's `role` answer when present, fathers#150, else the review role's own conditions), `_candidates` orders the
review role's choices whose conditions hold (fathers#155), and `_execute` runs every member's next candidate at
once, each on the same prompt, until each has an answer or no candidate left. A single review is a panel of one
whose candidates are its primary choice and the next one, its fallback.
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
import tempfile
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
# The share of the weekly Codex limit a Codex choice needs when the models file names none (the 2026-11-13
# incident: a review on credits); a Codex review never runs without a limit read.
DEFAULT_MIN_LEFT = 15


@dataclass(frozen=True)
class Request:
    standards: str
    subject: str
    base: str
    tip: str
    # The implementer's model family (anthropic, openai) for `not_family`; None when not known.
    family: str | None = None
    # `review --reviewer` (fathers#155): the reviewers this review must run, whatever the Codex limit; () chooses.
    reviewers: tuple[str, ...] = ()


@dataclass(frozen=True)
class Answer:
    evidence: dict[str, Any]
    error: str | None = None


@dataclass(frozen=True)
class Model:
    """One review choice: the model's alias in the models file, its id, CLI (codex or claude) and effort, the share
    of the weekly Codex limit it needs (`when.limit.codex_weekly`), its family and `when.not_family`."""
    alias: str
    id: str
    cli: str
    effort: str
    min_left: int | None = None
    family: str | None = None
    not_family: bool = False


DEFAULTS = (Model("sol", "gpt-6.1-sol", "codex", "medium", DEFAULT_MIN_LEFT, "openai"),
            Model("opus", "claude-opus-5-5", "claude", "medium", None, "anthropic"))


@dataclass(frozen=True)
class Role:
    """Mastack's `role review` answer: its choice (id and effort as role gives them), the models it skipped with
    the reason (a model it chose at a later choice is not skipped), and the Codex limit it read, if it read one."""
    chosen: Model
    skipped: dict[str, str]
    quota: dict[str, Any] | None


class Conditions:
    """Whether a model may review now, the one evaluation of the single reviewer and every panel member: its CLI,
    then (unless forced) role's skip reason, `not_family` against a known implementer family and the weekly Codex
    limit, read at most once (role's read when it made one)."""

    def __init__(self, project: Config, family: str | None, role: Role | None):
        self.project, self.family, self.role = project, family, role
        self.codex, self.claude = _codex_cli(project), _claude_cli(project)
        self.quota: dict[str, Any] | None = role.quota if role else None

    def limit(self) -> dict[str, Any]:
        if self.quota is None:
            self.quota = _codex_limit(self.codex) if self.codex else {"left": None, "blocked": None}
        return self.quota

    def reason(self, model: Model, forced: bool = False) -> str | None:
        """Why the model may not review now, or None."""
        if model.cli == "codex" and not self.codex:
            return f"no Codex CLI ({self.project.env_names('CODEX')}, CODEX_CLI_PATH, codex on PATH)"
        if model.cli == "claude" and not self.claude:
            return f"no claude CLI on PATH ({self.project.env_names('CLAUDE')})"
        if model.cli not in ("codex", "claude"):
            return f"{model.alias} runs on {model.cli}, which Lanefather cannot start"
        if forced:
            return None
        if self.role is not None and model.alias in self.role.skipped:
            return self.role.skipped[model.alias]
        if model.not_family and self.family and model.family == self.family:
            return f"same family as the implementer ({self.family})"
        if model.min_left is not None:
            limit = self.limit()
            if limit.get("blocked"):
                return str(limit["blocked"])
            left = limit.get("left")
            if not isinstance(left, int) or left < model.min_left:
                return f"Codex has {left}% of the weekly limit left, under {model.min_left}%"
        return None


@dataclass
class Slot:
    """One reviewer of a review: the receipt entry and the candidates still to try, in order."""
    entry: dict[str, Any]
    candidates: list[Model]


class Reviewer:
    """One project's reviewer. run returns complete selection evidence on success or failure."""

    def __init__(self, project: Config):
        self.project = project
        self.models, self.choices = _models(project)

    @property
    def effort(self) -> str:
        """The effort a running receipt names before the reviewer is chosen: the first choice's."""
        return self.choices[0].effort

    def run(self, label: str, checkout: str, request: Request, output: Path, log: Path) -> Answer:
        panel = request.reviewers or self.project.review_panel
        evidence: dict[str, Any] = {"effort": self.effort}
        if request.reviewers:
            evidence["forced"] = {"by": "flag", "reviewers": list(request.reviewers)}
        try:
            forced = bool(request.reviewers)
            role = None if forced else _role(self.project, _codex_cli(self.project), request.family, evidence)
            conditions = Conditions(self.project, request.family, role)
            models = dict(self.models)
            if role is not None:
                models[role.chosen.alias] = role.chosen
            evidence["codex"] = conditions.codex
            # A forced panel takes no stand-in, so it needs no choice evaluated (and no limit read).
            ordered, skipped = ([], []) if forced else _candidates(self.choices, conditions, role)
            slots = (self._panel(panel, models, ordered, conditions, forced) if panel
                     else [Slot({"member": None, "reason": "; ".join(skipped) or None}, ordered[:2])])
            for slot in slots:
                if slot.entry["member"] and (slot.entry.get("reason") or slot.entry.get("error")):
                    print(f"lane: review {label}: panel member {slot.entry['member']}: "
                          f"{slot.entry.get('reason') or slot.entry.get('error')}", flush=True)
            evidence["codex_left"] = (conditions.quota or {}).get("left")
            answer = self._execute(label, checkout, request, slots, output, log, evidence)
            if not panel:
                entry = slots[0].entry
                evidence.update({k: entry.get(k) for k in ("reviewer", "model", "effort")},
                                fallback=entry.get("fallback") or entry.get("reason"))
            else:
                evidence["panel"] = [s.entry for s in slots]
            return answer
        except Exception as error:
            return Answer(evidence, str(error))

    def _panel(self, panel: tuple[str, ...], models: dict[str, Model], ordered: list[Model],
               conditions: Conditions, forced: bool) -> list[Slot]:
        """Each member, or what reviews in its place. A configured member whose condition fails (a used-up Codex
        week, the implementer's own family) is replaced by the first choice that holds, or skipped when that is on
        the panel already (fathers#151); a forced member (`--reviewer`) runs whatever the limit, or fails."""
        slots: list[Slot] = []
        stand_in = ordered[0] if ordered else None
        for alias in panel:
            model = models.get(alias)
            why = (conditions.reason(model, forced) if model is not None
                   else f"{alias} is not a model of the models file ({_models_path(self.project) or 'the defaults'})")
            entry: dict[str, Any] = {"member": alias, "reviewer": alias, "status": "pending", "reason": None}
            if why is None and model is not None:
                slots.append(Slot(entry, [model]))
                continue
            taken = set(panel) | {s.entry["reviewer"] for s in slots}
            if forced:
                entry.update(status="failed", error=why, numbers=[])
            elif stand_in is not None and stand_in.alias not in taken:
                entry.update(reviewer=stand_in.alias, reason=f"{why}: {stand_in.alias} reviews in its place")
                slots.append(Slot(entry, [stand_in]))
                continue
            else:
                entry.update(status="skipped", numbers=[],
                             reason=(f"{why}; its fallback {stand_in.alias} is already on the panel" if stand_in
                                     else f"{why}; no other choice can review"))
            if model is not None:
                entry.update(model=model.id, effort=model.effort)
            slots.append(Slot(entry, []))
        return slots

    def _execute(self, label: str, checkout: str, request: Request, slots: list[Slot], output: Path, log: Path,
                 evidence: dict[str, Any]) -> Answer:
        """Run each slot's next candidate, all at once on the same prompt file, until every slot has an answer or
        none left; answers stay in a private directory until every member finished, then move beside the receipt
        (no member can read another's). Numbers the findings across the slots."""
        project = self.project
        pending = [s for s in slots if s.candidates]
        if not pending:
            return Answer(evidence, "no reviewer can review: " + "; ".join(
                f"{s.entry['member'] or 'review'}: {s.entry.get('error') or s.entry.get('reason')}" for s in slots))
        prompt = _review_prompt(project, request, _inline_diff(checkout, request))
        evidence["prompt_sha256"] = hashlib.sha256(prompt.encode()).hexdigest()
        private = Path(tempfile.mkdtemp(prefix=f"lanefather-review-{label}-"))
        texts: dict[int, str] = {}
        try:
            prompt_path = private / "prompt.md"
            prompt_path.write_text(prompt)
            attempt = 0
            while pending:
                jobs: list[tuple[list[str], Path, Path]] = []
                for slot in pending:
                    model = slot.candidates.pop(0)
                    attempt += 1
                    stem = private / f"{attempt}-{model.alias}"
                    answer_path, job_log = stem.with_suffix(".md"), stem.with_suffix(".log")
                    job_log.write_text("")
                    slot.entry.update(reviewer=model.alias, model=model.id, effort=model.effort, cli=model.cli,
                                      answer=str(answer_path), job_log=str(job_log))
                    why = slot.entry.get("fallback") or slot.entry.get("reason")
                    left = evidence.get("codex_left")
                    print(f"lane: review {label}: {model.id} {model.effort}"
                          + (f" ({left}% of the weekly Codex limit left)" if model.cli == "codex" and left is not None
                             else f" ({why})" if why else "")
                          + f" on {request.base[:10]}...{request.tip[:10]}", flush=True)
                    jobs.append((_codex_argv(self.codex(), model, checkout, answer_path), job_log, job_log)
                                if model.cli == "codex"
                                else (_claude_argv(self.claude(), model), answer_path, job_log))
                results = _run_together(jobs, checkout, prompt_path)
                retry: list[Slot] = []
                for slot, (status, seconds) in zip(pending, results, strict=True):
                    entry = slot.entry
                    answer_path, job_log = Path(entry.pop("answer")), Path(entry.pop("job_log"))
                    text = answer_path.read_text(errors="replace") if answer_path.exists() else ""
                    with open(log, "a") as combined:
                        combined.write(f"== {entry['reviewer']} ({entry['model']})\n"
                                       + job_log.read_text(errors="replace"))
                    entry["seconds"] = round(entry.get("seconds", 0) + seconds, 1)
                    if status == 0 and text.strip():
                        entry["status"] = "finished"
                        texts[id(slot)] = text
                        continue
                    name = entry["reviewer"].capitalize()
                    failure = (f"the {name} run failed (exit {status}): {_last_lines(job_log)}" if status
                               else f"{name} returned an empty review")
                    print(f"lane: review {label}: {entry['reviewer']} failed: {failure}", flush=True)
                    if slot.candidates:
                        entry["fallback"] = failure
                        retry.append(slot)
                    else:
                        entry.update(status="failed", error=failure, numbers=[])
                pending = retry
        finally:
            findings: list[dict[str, Any]] = []
            sections: list[str] = []
            for slot in slots:
                entry = slot.entry
                text = texts.get(id(slot))
                if text is None:
                    continue
                numbers: list[int] = []
                entry["numbers"] = numbers
                for finding in _parse_findings(text):
                    finding["n"] = len(findings) + 1
                    if entry.get("member"):
                        finding["reviewer"] = entry["reviewer"]
                    findings.append(finding)
                    numbers.append(finding["n"])
                if entry.get("member"):
                    final = output.with_name(f"{output.stem}.{entry['reviewer']}.md")
                    final.write_text(text)
                    entry["output"] = str(final)
                    sections.append(f"# {entry['reviewer']} ({entry['model']} {entry['effort']})\n\n{text.rstrip()}\n")
                else:
                    sections.append(text)
            output.write_text("\n".join(sections))
            shutil.rmtree(private, ignore_errors=True)
        members = [s.entry for s in slots if s.entry.get("member")]
        answered = [s.entry for s in slots if s.entry.get("status") == "finished"]
        if members:
            efforts = sorted({str(e["effort"]) for e in answered})
            evidence.update(reviewer="+".join(str(e["reviewer"]) for e in answered) or None,
                            model=", ".join(str(e["model"]) for e in answered) or None,
                            effort="/".join(efforts) or evidence["effort"],
                            fallback="; ".join(f"{e['member']}: {e.get('error') or e['reason']}" for e in members
                                               if e.get("status") != "finished" or e.get("reason")) or None)
        if not answered:
            entry = slots[0].entry
            return Answer(evidence, entry.get("error") if not members else "no panel member answered: " + "; ".join(
                f"{e['member']}: {e.get('error') or e.get('reason')}" for e in members))
        evidence["findings"] = findings
        return Answer(evidence)

    def codex(self) -> str:
        return _codex_cli(self.project) or "codex"

    def claude(self) -> str:
        return _claude_cli(self.project) or "claude"


def _candidates(choices: list[Model], conditions: Conditions, role: Role | None) -> tuple[list[Model], list[str]]:
    """The review role's choices whose conditions hold, in order and one per model: role's choice first when role
    answered, then the choices after it; and why each choice before the first was skipped (fathers#155)."""
    ordered: list[Model] = []
    skipped: list[str] = []
    start = 0
    if role is not None:
        ordered.append(role.chosen)
        skipped = list(role.skipped.values())
        start = next((i + 1 for i, c in enumerate(choices) if c.alias == role.chosen.alias), len(choices))
    for choice in choices[start:]:
        if any(c.alias == choice.alias for c in ordered):
            continue
        why = conditions.reason(choice)
        if why is None:
            ordered.append(choice)
        elif not ordered:
            skipped.append(why)
    return ordered, skipped


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


def _models(project: Config) -> tuple[dict[str, Model], list[Model]]:
    """The models file: every model by alias (with the effort and limit of its first review choice), and the review
    role's choices in their order, a priority list (fathers#155); the defaults, Sol then Opus, when no file is
    found or it has no readable review role."""
    sol, opus = DEFAULTS
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
                                     DEFAULT_MIN_LEFT if entry["cli"] == "codex" else None,
                                     family if isinstance(family, str) else None)
        choices: list[Model] = []
        for choice in data["roles"]["review"]["choices"]:
            base = found[choice["model"]]
            when: dict[str, Any] = choice.get("when") or {}
            limits: dict[str, Any] = when.get("limit") or {}
            limit = limits.get("codex_weekly")
            # A Codex choice without a limit still needs DEFAULT_MIN_LEFT: no review runs on credits unread.
            min_left = (int(limit) if isinstance(limit, (int, float))
                        else DEFAULT_MIN_LEFT if base.cli == "codex" else None)
            choices.append(Model(base.alias, base.id, base.cli, str(choice.get("effort", "medium")), min_left,
                                 base.family, bool(when.get("not_family"))))
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


def _role(project: Config, codex: str | None, family: str | None, evidence: dict[str, Any]) -> Role | None:
    """`role review [--implementer-family F] --json` (fathers#150): its choice, its skip reasons and the limit it
    read, also in evidence["role"]; None without the command, or when it gives no choice (evidence["role_error"]
    says why), so the review role read here decides. The role reads the Codex limit with the CLI this review runs."""
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
    alias = str(chosen.get("model"))
    raw: list[Any] = chosen.get("skipped") or []
    skips = [cast(dict[str, Any], s) for s in raw if isinstance(s, dict)]
    skipped = {str(s.get("model")): str(s.get("reason")) for s in skips if s.get("model") != alias}
    quotas: Any = chosen.get("quotas") or {}
    weekly: Any = cast(dict[str, Any], quotas).get("codex_weekly") if isinstance(quotas, dict) else None
    quota: dict[str, Any] | None = None
    if isinstance(weekly, dict):
        read = cast(dict[str, Any], weekly)
        quota = {"left": read.get("left"),
                 "blocked": (f"Codex reports {read['reached']}" if read.get("reached")
                             else f"the Codex limit could not be read: {read['error']}" if read.get("error")
                             else None if isinstance(read.get("left"), int) else "the Codex limit could not be read")}
    reasons = [str(s.get("reason")) for s in skips]
    evidence["role"] = {"command": command, "family": family, "chose": alias, "skipped": reasons,
                        "caution": chosen.get("caution"), "note": chosen.get("note")}
    if skips or chosen.get("caution"):
        print(f"lane: review: role review chose {alias}"
              + (f"; skipped {'; '.join(reasons)}" if skips else "")
              + (f"; {chosen['caution']}" if chosen.get("caution") else ""), flush=True)
    model = Model(alias, str(chosen["id"]), str(chosen.get("cli")), str(chosen.get("effort") or "medium"),
                  None, chosen.get("family") if isinstance(chosen.get("family"), str) else None)
    return Role(model, skipped, quota)


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
