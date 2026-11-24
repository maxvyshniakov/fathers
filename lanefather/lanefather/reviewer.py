"""The configured reviewer: model choice, real CLI adapters and the fallback, without lane state or GitHub writes.

Review receipts and their lifecycle belong to review.py. This module needs a Project config, the frozen checkout
and a request; it owns how Codex, Claude and Antigravity receive that request and how their answer is interpreted.

One pipeline serves a single review and a panel (fathers#151): `Conditions` says whether a model may review now
(Mastack's `role` answer when present, fathers#150, else the review role's own conditions), `_candidates` orders the
review role's choices whose conditions hold (fathers#155), and `_execute` runs every member's next candidate at
once, each on the same prompt, until each has an answer or no candidate left. A single review is a panel of one
whose candidates are its primary choice and the next ones, its fallbacks.
"""
from __future__ import annotations

from collections.abc import Callable
import contextlib
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from typing import Any, cast

from .config import Config
from .errors import LaneError
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
# `agy --print-timeout`: the turn ends with a result a minute before Lanefather stops the run (fathers#201).
AGY_PRINT_TIMEOUT = REVIEW_TIMEOUT - 60

# Provider/CLI option names and known model presets matching Mastack's models.toml (fathers#201).
CLI_OPTIONS: dict[str, str] = {
    "codex": "model_reasoning_effort",
    "claude": "effort",
    "antigravity": "thought_level",
}
PROVIDER_OPTIONS: dict[str, str] = {
    "openai": "model_reasoning_effort",
    "anthropic": "effort",
    "google": "thought_level",
    "antigravity": "thought_level",
}
CLI_FAMILIES: dict[str, str] = {
    "codex": "openai",
    "claude": "anthropic",
    "antigravity": "google",
}


@dataclass(frozen=True)
class Request:
    standards: str
    subject: str
    base: str
    tip: str
    # The implementer's model family (anthropic, openai, google) for `not_family`; None when not known.
    family: str | None = None
    # `review --reviewer` (fathers#155): the reviewers this review must run, whatever the Codex limit; () chooses.
    reviewers: tuple[str, ...] = ()


@dataclass(frozen=True)
class Answer:
    evidence: dict[str, Any]
    error: str | None = None


@dataclass(frozen=True)
class Model:
    """One review choice: the model's alias in the models file, its id, CLI (codex, claude or antigravity) and effort,
    the share of the weekly Codex limit it needs (`when.limit.codex_weekly`), its family, `when.not_family`, the
    choice's `note` for the person reading the review ("same family as the implementer: fresh context only"), its
    provider option name (`effort`, `model_reasoning_effort`, `thought_level`; the receipt's `options` key, which
    for Antigravity is Mastack's `thought_level` while `agy` takes `--effort`), and an optional
    `[cli.antigravity].review` command template."""
    alias: str
    id: str
    cli: str
    effort: str
    min_left: int | None = None
    family: str | None = None
    not_family: bool = False
    note: str | None = None
    option: str | None = None
    review_argv: tuple[str, ...] = ()

    @property
    def provider(self) -> str:
        return self.family or CLI_FAMILIES.get(self.cli, self.cli)

    @property
    def option_name(self) -> str:
        return (self.option
                or CLI_OPTIONS.get(self.cli)
                or (PROVIDER_OPTIONS.get(self.family) if self.family else None)
                or "effort")

    @property
    def options(self) -> dict[str, str]:
        return {self.option_name: self.effort}


DEFAULTS = (Model("sol", "gpt-6.1-sol", "codex", "medium", DEFAULT_MIN_LEFT, "openai",
                  option="model_reasoning_effort"),
            Model("opus", "claude-opus-5-5", "claude", "medium", None, "anthropic",
                  option="effort"))
KNOWN_MODELS: tuple[Model, ...] = (
    *DEFAULTS,
    Model("fable", "claude-fable-5-1", "claude", "medium", None, "anthropic", option="effort"),
    Model("sonnet", "claude-sonnet-5-5", "claude", "medium", None, "anthropic", option="effort"),
    Model("neon", "gemini-4-neon", "antigravity", "medium", None, "google", option="thought_level"),
    Model("argon", "gemini-4-argon", "antigravity", "medium", None, "google", option="thought_level"),
    Model("carbon", "gemini-4-carbon", "antigravity", "medium", None, "google", option="thought_level"),
    Model("flash", "gemini-3.8-flash", "antigravity", "medium", None, "google", option="thought_level"),
)
REVIEWER_ALIASES: dict[str, str] = {
    "antigravity": "neon",
    "google": "neon",
    "agy": "neon",
    "claude": "opus",
    "anthropic": "opus",
    "codex": "sol",
    "openai": "sol",
}


def _normalize_cli(cli: str) -> str:
    cleaned = cli.strip().lower()
    return "antigravity" if cleaned in ("antigravity", "agy", "google") else cleaned


def _resolve_reviewer(spec: str, models: dict[str, Model], choices: list[Model]) -> Model | None:
    """Resolve an explicit reviewer alias, provider/CLI name (`antigravity`, `google`, `agy`), or model id/spec
    (`gemini-4-neon`, `gemini-4-argon:high`, `antigravity:gemini-4-carbon`) to a Model (fathers#201)."""
    raw = spec.strip()
    if not raw:
        return None
    if raw in models:
        return models[raw]
    lower = raw.lower()
    if lower in models:
        return models[lower]

    # For provider/CLI shorthand (`antigravity`, `google`, `agy`), prefer the first matching review choice in the
    # loaded table, else the first matching model in the table, else the built-in default (neon).
    if lower in ("antigravity", "google", "agy"):
        for choice in choices:
            if choice.cli == "antigravity" or choice.family == "google":
                return choice
        for candidate in models.values():
            if candidate.cli == "antigravity" or candidate.family == "google":
                return candidate
    mapped = REVIEWER_ALIASES.get(lower)
    if mapped and mapped in models:
        return models[mapped]

    # Match by model id in the loaded table or KNOWN_MODELS.
    for candidate in (*models.values(), *KNOWN_MODELS):
        if candidate.id == raw or candidate.id.lower() == lower or candidate.alias == lower:
            return candidate

    # Support `<provider-or-model>:<model-or-effort>` or `<provider>/<model>[:<effort>]` specs.
    effort_override: str | None = None
    target = raw
    if ":" in target:
        head, tail = target.rsplit(":", 1)
        if tail.lower() in ("minimal", "low", "medium", "high", "xhigh", "max"):
            target, effort_override = head, tail.lower()
    provider_prefix: str | None = None
    for sep in ("/", ":"):
        if sep in target:
            left, right = target.split(sep, 1)
            if left.lower() in ("antigravity", "google", "agy", "claude", "anthropic", "codex", "openai"):
                provider_prefix, target = left.lower(), right
                break

    base = (_resolve_reviewer(target, models, choices) if target != raw else None)
    if base is not None:
        return Model(base.alias, base.id, base.cli, effort_override or base.effort, base.min_left,
                     base.family, base.not_family, base.note, base.option, base.review_argv)

    if provider_prefix in ("antigravity", "google", "agy") or target.lower().startswith("gemini-"):
        model_id = target if target.lower() not in ("antigravity", "google", "agy") else "gemini-4-neon"
        alias = next((m.alias for m in (*models.values(), *KNOWN_MODELS) if m.id == model_id),
                     "neon" if model_id == "gemini-4-neon" else model_id)
        return Model(alias, model_id, "antigravity", effort_override or "medium", None, "google",
                     option="thought_level")
    if provider_prefix in ("claude", "anthropic") or target.lower().startswith("claude-"):
        model_id = target if target.lower() not in ("claude", "anthropic") else "claude-opus-5-5"
        alias = next((m.alias for m in (*models.values(), *KNOWN_MODELS) if m.id == model_id), model_id)
        return Model(alias, model_id, "claude", effort_override or "medium", None, "anthropic",
                     option="effort")
    if provider_prefix in ("codex", "openai") or target.lower().startswith("gpt-"):
        model_id = target if target.lower() not in ("codex", "openai") else "gpt-6.1-sol"
        alias = next((m.alias for m in (*models.values(), *KNOWN_MODELS) if m.id == model_id), model_id)
        return Model(alias, model_id, "codex", effort_override or "medium", DEFAULT_MIN_LEFT, "openai",
                     option="model_reasoning_effort")
    return None


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
        self.codex = _codex_cli(project)
        self.claude = _claude_cli(project)
        self.antigravity = _antigravity_cli(project)
        self.quota: dict[str, Any] | None = role.quota if role else None

    def limit(self) -> dict[str, Any]:
        if self.quota is None:
            self.quota = _codex_limit(self.codex) if self.codex else {"left": None, "blocked": None}
        return self.quota

    def reason(self, model: Model, forced: bool = False, panel: bool = False) -> str | None:
        """Why the model may not review now, or None. A panel member (`panel`) is never kept off for its family
        (owner decision 2026-11-16, fathers#151: the panel compares reviewers on the same diff): only its CLI and
        the Codex limit count, so role's skip reasons, which mix both, are not read for it."""
        if model.cli == "codex" and not self.codex:
            return f"no Codex CLI ({self.project.env_names('CODEX')}, CODEX_CLI_PATH, codex on PATH)"
        if model.cli == "claude" and not self.claude:
            return f"no claude CLI on PATH ({self.project.env_names('CLAUDE')})"
        if model.cli == "antigravity" and not self.antigravity and not model.review_argv:
            return (f"no Antigravity CLI ({self.project.env_names('ANTIGRAVITY')}, "
                    f"{self.project.env_names('AGY')}, ANTIGRAVITY_CLI_PATH, AGY_CLI_PATH, "
                    "agy or antigravity on PATH, ~/.gemini/bin/agy, T3 Code's bundled antigravity)")
        if model.cli not in ("codex", "claude", "antigravity"):
            return f"{model.alias} runs on {model.cli}, which Lanefather cannot start"
        if forced:
            return None
        if not panel and self.role is not None and model.alias in self.role.skipped:
            return self.role.skipped[model.alias]
        if not panel and model.not_family and self.family and model.family == self.family:
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
    """One reviewer of a review: the receipt entry, the candidates still to try, in order, and for a panel member
    (unless forced) the panel's shared pool of stand-ins it may take one from when its run fails (fathers#201)."""
    entry: dict[str, Any]
    candidates: list[Model]
    pool: list[Model] | None = None


@dataclass(frozen=True)
class Decoded:
    """A reviewer's answer read from its structured output: the review text, the run's own error (Antigravity's
    `status: ERROR`), whether the output had no result at all, and the session and token usage it reported."""
    text: str
    error: str | None = None
    missing: bool = False
    session: str | None = None
    usage: dict[str, Any] | None = None


@dataclass(frozen=True)
class Job:
    """One reviewer process: its command line, the file it reads on stdin, where its stdout and stderr go (the same
    file for a CLI that writes its answer itself), and the decoder that turns its stdout into the answer, if any."""
    argv: list[str]
    stdin: Path
    stdout: Path
    log: Path
    decode: Callable[[str], Decoded] | None = None


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
            ordered, skipped = ([], []) if forced else _candidates(self.choices, conditions, role, bool(panel))
            slots = (self._panel(panel, models, ordered, conditions, forced) if panel
                     else [Slot({"member": None, "reason": "; ".join(skipped) or None}, ordered[:3])])
            for slot in slots:
                if slot.entry["member"] and (slot.entry.get("reason") or slot.entry.get("error")):
                    print(f"lane: review {label}: panel member {slot.entry['member']}: "
                          f"{slot.entry.get('reason') or slot.entry.get('error')}", flush=True)
            evidence["codex_left"] = (conditions.quota or {}).get("left")
            answer = self._execute(label, checkout, request, slots, output, log, evidence)
            if not panel:
                entry = slots[0].entry
                evidence.update(
                    {k: entry.get(k) for k in ("reviewer", "model", "effort", "provider", "cli", "options")},
                    **{k: entry[k] for k in ("session", "usage") if entry.get(k)},
                    fallback=entry.get("fallback") or entry.get("reason"),
                )
            else:
                evidence["panel"] = [s.entry for s in slots]
            return answer
        except Exception as error:
            return Answer(evidence, str(error))

    def _panel(self, panel: tuple[str, ...], models: dict[str, Model], ordered: list[Model],
               conditions: Conditions, forced: bool) -> list[Slot]:
        """Each member, or what reviews in its place. A configured member reviews on its first choice whose
        conditions hold under the panel's family policy (_pick: a member of the implementer's family stays, on the
        table's choice for that case); one with none (a used-up Codex week, no CLI) is replaced by the first
        candidate (role's choice when role answered), or skipped when that is on the panel already (fathers#151). A
        forced member (`--reviewer`) runs whatever the limit, or fails. Each entry's note is its actual reviewer's.
        Unless forced, the members that run share one pool of stand-ins, the candidates not on the panel, in order: a
        member whose run fails takes the next one nobody else reviews with (fathers#201)."""
        slots: list[Slot] = []
        stand_in = ordered[0] if ordered else None
        role_choice = ordered[0] if ordered and conditions.role is not None else None
        for alias in panel:
            known = models.get(alias) or _resolve_reviewer(alias, models, self.choices)
            resolved_alias = known.alias if known is not None else alias
            if known is None:
                model: Model | None = None
                why: str | None = (f"{alias} is not a model of the models file "
                                   f"({_models_path(self.project) or 'the defaults'})")
            elif forced:
                model, why = known, conditions.reason(known, forced=True)
            elif role_choice is not None and role_choice.alias == resolved_alias:
                model, why = role_choice, None
            else:
                model, why = _pick(self.choices, known, conditions, panel=True)
            entry: dict[str, Any] = {"member": alias, "reviewer": resolved_alias, "status": "pending", "reason": None}
            if why is None and model is not None:
                slots.append(Slot(_noted(entry, model, conditions.family), [model]))
                continue
            taken = set(panel) | {s.entry["reviewer"] for s in slots}
            if forced:
                entry.update(status="failed", error=why, numbers=[])
            elif stand_in is not None and stand_in.alias not in taken:
                entry.update(reviewer=stand_in.alias, reason=f"{why}: {stand_in.alias} reviews in its place")
                slots.append(Slot(_noted(entry, stand_in, conditions.family), [stand_in]))
                continue
            else:
                entry.update(status="skipped", numbers=[],
                             reason=(f"{why}; its fallback {stand_in.alias} is already on the panel" if stand_in
                                     else f"{why}; no other choice can review"))
            if known is not None:
                entry.update(model=known.id, effort=known.effort, provider=known.provider, cli=known.cli,
                             options=known.options)
            slots.append(Slot(entry, []))
        if not forced:
            taken = set(panel) | {s.entry["reviewer"] for s in slots}
            pool = [m for m in ordered if m.alias not in taken]
            for slot in slots:
                if slot.candidates:
                    slot.pool = pool
        return slots

    def _execute(self, label: str, checkout: str, request: Request, slots: list[Slot], output: Path, log: Path,
                 evidence: dict[str, Any]) -> Answer:
        """Run each slot's next candidate, all at once on the same prompt file, until every slot has an answer or
        none left; answers stay in a private directory until every member finished, then move beside the receipt
        (no member can read another's). Numbers the findings across the slots."""
        project = self.project
        pending = [s for s in slots if s.candidates]
        if not pending:
            entry0 = slots[0].entry
            if len(slots) == 1 and entry0.get("member") is None:
                for candidate in self.choices[:1]:
                    entry0.setdefault("reviewer", candidate.alias)
                    entry0.setdefault("model", candidate.id)
                    entry0.setdefault("effort", candidate.effort)
                    entry0.setdefault("provider", candidate.provider)
                    entry0.setdefault("cli", candidate.cli)
                    entry0.setdefault("options", candidate.options)
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
                runs: list[tuple[Slot, Job, Path]] = []
                for slot in pending:
                    model = slot.candidates.pop(0)
                    attempt += 1
                    for stale in ("session", "usage"):
                        slot.entry.pop(stale, None)
                    slot.entry.update(reviewer=model.alias, model=model.id, effort=model.effort,
                                      provider=model.provider, cli=model.cli, options=model.options)
                    if slot.entry.get("member"):
                        _noted(slot.entry, model, request.family)
                    why = slot.entry.get("fallback") or slot.entry.get("reason")
                    left = evidence.get("codex_left")
                    print(f"lane: review {label}: {model.id} {model.effort}"
                          + (f" ({left}% of the weekly Codex limit left)" if model.cli == "codex" and left is not None
                             else f" ({why})" if why else "")
                          + f" on {request.base[:10]}...{request.tip[:10]}", flush=True)
                    job, answer_path = self._job(model, checkout, prompt_path, private / f"{attempt}-{model.alias}")
                    runs.append((slot, job, answer_path))
                # An Antigravity run may not change the frozen checkout (fathers#201): plan mode and the sandbox
                # should hold it read-only, and this says so when they do not.
                guarded = any(s.entry["cli"] == "antigravity" for s, _, _ in runs)
                before = _checkout_state(checkout) if guarded else ""
                results = _run_together([job for _, job, _ in runs], checkout)
                changed = _checkout_state(checkout) if guarded else before
                if changed != before and not before:
                    _restore_checkout(checkout)
                retry: list[Slot] = []
                for (slot, job, answer_path), (status, seconds) in zip(runs, results, strict=True):
                    entry = slot.entry
                    raw = job.stdout.read_text(errors="replace") if job.decode and job.stdout.exists() else ""
                    decoded = job.decode(raw) if job.decode else None
                    if decoded is not None:
                        answer_path.write_text(decoded.text)
                        entry.update({k: v for k, v in (("session", decoded.session), ("usage", decoded.usage)) if v})
                    text = answer_path.read_text(errors="replace") if answer_path.exists() else ""
                    with open(log, "a") as combined:
                        combined.write(f"== {entry['reviewer']} ({entry['model']})\n"
                                       + job.log.read_text(errors="replace") + raw)
                    entry["seconds"] = round(entry.get("seconds", 0) + seconds, 1)
                    name = entry["reviewer"].capitalize()
                    if changed != before and entry["cli"] == "antigravity":
                        failure = f"the {name} run changed the review checkout: {changed[:300] or 'files removed'}"
                    elif decoded is not None and decoded.error:
                        exit_text = f" (exit {status})" if status else ""
                        failure = f"the {name} run failed{exit_text}: {decoded.error}"
                    elif status:
                        failure = f"the {name} run failed (exit {status}): {_last_lines(job.log)}"
                    elif not text.strip():
                        failure = f"{name} returned an empty review" + (
                            " (no result in its output)" if decoded is not None and decoded.missing else "")
                    else:
                        entry["status"] = "finished"
                        texts[id(slot)] = text
                        continue
                    print(f"lane: review {label}: {entry['reviewer']} failed: {failure}", flush=True)
                    spare = None if slot.candidates else _spare(slot, slots)
                    if slot.candidates:
                        entry["fallback"] = failure + (f": {spare.alias} reviews in its place" if spare else "")
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
            providers = [str(e["provider"]) for e in answered if e.get("provider")]
            clis = [str(e["cli"]) for e in answered if e.get("cli")]
            combined_options: dict[str, str] = {}
            for e in answered:
                raw_opts: Any = e.get("options")
                if isinstance(raw_opts, dict):
                    for opt_k, opt_v in cast(dict[str, Any], raw_opts).items():
                        combined_options[str(opt_k)] = str(opt_v)
            evidence.update(reviewer="+".join(str(e["reviewer"]) for e in answered) or None,
                            model=", ".join(str(e["model"]) for e in answered) or None,
                            effort="/".join(efforts) or evidence["effort"],
                            provider="+".join(dict.fromkeys(providers)) or None,
                            cli="+".join(dict.fromkeys(clis)) or None,
                            options=combined_options or None,
                            fallback="; ".join(
                                f"{e['member']}: {e.get('error') or e.get('reason') or e.get('fallback')}"
                                for e in members
                                if e.get("status") != "finished" or e.get("reason") or e.get("fallback")) or None)
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

    def antigravity(self) -> str:
        return _antigravity_cli(self.project) or "agy"

    def _job(self, model: Model, checkout: str, prompt: Path, stem: Path) -> tuple[Job, Path]:
        """The process that runs one candidate, and the file its answer ends in. Codex and Claude read the prompt
        on stdin and write the answer (to -o, to stdout); Antigravity reads it as one stream-json message and its
        answer is decoded from the stream (fathers#201), unless `[cli.antigravity].review` names a command."""
        answer, log = Path(f"{stem}.md"), Path(f"{stem}.log")
        log.write_text("")
        if model.cli == "codex":
            return Job(_codex_argv(self.codex(), model, checkout, answer), prompt, log, log), answer
        if model.cli == "claude":
            return Job(_claude_argv(self.claude(), model), prompt, answer, log), answer
        if model.review_argv:
            argv, writes_output = _antigravity_template_argv(self.antigravity(), model, checkout, answer, prompt)
            return Job(argv, prompt, log if writes_output else answer, log), answer
        message = Path(f"{stem}.stdin.ndjson")
        message.write_text(_agy_message(prompt.read_text()))
        return Job(_agy_argv(self.antigravity(), model), message, Path(f"{stem}.ndjson"), log, _agy_result), answer


def _pick(choices: list[Model], model: Model, conditions: Conditions, panel: bool) -> tuple[Model | None, str | None]:
    """The model's first review choice whose conditions hold, in the review role's order (the model itself when the
    role lists it nowhere), and else why its choices do not hold. Under the panel's family policy (owner decision
    2026-11-16, fathers#151) a second pass drops `not_family`: a member of the implementer's family still reviews,
    on the first of its choices that holds without that condition, preferring one that asks for nothing about the
    family (Mastack's same-family Opus) because the first pass takes those."""
    own = [c for c in choices if c.alias == model.alias] or [model]
    why: str | None = None
    for lenient in (False, True) if panel else (False,):
        why = None
        for choice in own:
            reason = conditions.reason(choice, panel=lenient)
            if reason is None:
                return choice, None
            why = why or reason
    return None, why


def _noted(entry: dict[str, Any], model: Model, family: str | None) -> dict[str, Any]:
    """The entry with the note of the reviewer that actually reviews: a reviewer of the implementer's family gives
    its choice's note, else "same family as the implementer (F)"; any other reviewer gives none."""
    entry.pop("note", None)
    if family and model.family == family:
        entry["note"] = model.note or f"same family as the implementer ({family})"
    return entry


def _spare(slot: Slot, slots: list[Slot]) -> Model | None:
    """The next stand-in of the panel's shared pool that no other member reviews with, now queued as the slot's
    candidate and gone from the pool, so two failing members never take the same one; None without one."""
    if slot.pool is None:
        return None
    busy = {s.entry.get("reviewer") for s in slots}
    while slot.pool:
        model = slot.pool.pop(0)
        if model.alias not in busy:
            slot.candidates.append(model)
            return model
    return None


def _candidates(choices: list[Model], conditions: Conditions, role: Role | None,
                panel: bool = False) -> tuple[list[Model], list[str]]:
    """The review role's choices whose conditions hold, in order and one per model: role's choice first when role
    answered (it stays authoritative), then the choices after it; and why each choice before the first was skipped
    (fathers#155). When `role` chose a model whose local CLI cannot run (e.g. `role` evaluated quota/family without
    checking the reviewer's binary), its reason is recorded in `skipped` and the choices after it in the table take
    over. Under the panel's family policy (`panel`), a model no choice admits on the strict conditions may
    still stand in on its own family's choice (_pick), after every model the strict conditions admit."""
    ordered: list[Model] = []
    skipped: list[str] = []
    start = 0
    if role is not None:
        skipped = list(role.skipped.values())
        role_why = conditions.reason(role.chosen)
        if role_why is None:
            ordered.append(role.chosen)
        else:
            skipped.append(f"{role.chosen.alias}: {role_why}")
        start = next((i + 1 for i, c in enumerate(choices) if c.alias == role.chosen.alias), len(choices))
    for choice in choices[start:]:
        if any(c.alias == choice.alias for c in ordered):
            continue
        why = conditions.reason(choice)
        if why is None:
            ordered.append(choice)
        elif not ordered:
            skipped.append(why)
    if panel:
        for choice in choices[start:]:
            if not any(c.alias == choice.alias for c in ordered):
                found, _ = _pick(choices, choice, conditions, panel=True)
                if found is not None:
                    ordered.append(found)
    return ordered, skipped


def _codex_argv(codex: str, model: Model, checkout: str, output: Path) -> list[str]:
    return [codex, "exec", "-C", checkout, "-s", "read-only", "-m", model.id, "-c",
            f'model_reasoning_effort="{model.effort}"', "--ephemeral", "-o", str(output), "-"]


def _claude_argv(claude: str, model: Model) -> list[str]:
    return [claude, "-p", "--model", model.id, "--effort", model.effort, "--allowedTools", OPUS_TOOLS]


def _antigravity_template_argv(antigravity: str, model: Model, checkout: str, output: Path,
                               prompt: Path) -> tuple[list[str], bool]:
    """`[cli.antigravity].review` from `models.toml` expanded for one run, the escape hatch beside the built-in
    `agy` contract (fathers#201): the raw prompt on stdin, the answer as text on stdout or in `{output}`. Returns
    `(argv, writes_output_file)`."""
    substitutions = {
        "{cli}": antigravity,
        "{id}": model.id,
        "{model}": model.id,
        "{effort}": model.effort,
        "{thought_level}": model.effort,
        "{option}": model.option_name,
        "{checkout}": checkout,
        "{output}": str(output),
        "{prompt}": str(prompt),
    }
    argv: list[str] = []
    writes_output = False
    for index, token in enumerate(model.review_argv):
        if "{output}" in token:
            writes_output = True
        expanded = token
        for key, value in substitutions.items():
            expanded = expanded.replace(key, value)
        if index == 0 and expanded in ("antigravity", "agy") and antigravity:
            expanded = antigravity
        argv.append(expanded)
    return argv, writes_output


def _agy_argv(antigravity: str, model: Model) -> list[str]:
    """The headless `agy` review (fathers#201): plan mode in the sandbox, no slash commands or skills, one
    stream-json message in (the prompt can exceed one argument's limit) and the event stream out, and a time limit
    that ends the turn with a result before Lanefather's own."""
    base = shlex.split(antigravity) if " " in antigravity and not os.path.exists(antigravity) else [antigravity]
    # `agy --effort` takes low, medium, high, xhigh or max: Mastack's `minimal` runs as `low`.
    effort = "low" if model.effort == "minimal" else model.effort
    return [*base, "--model", model.id, "--effort", effort, "--mode", "plan", "--sandbox",
            "--disable-slash-commands", "--print-timeout", f"{AGY_PRINT_TIMEOUT}s", "--input-format", "stream-json",
            "--output-format", "stream-json", "-p="]


def _agy_message(prompt: str) -> str:
    """The prompt as the one stream-json user message `agy --input-format stream-json` reads on stdin."""
    return json.dumps({"event": "user", "message": {"content": prompt}}) + "\n"


def _agy_result(stream: str) -> Decoded:
    """The answer of an `agy --output-format stream-json` run: the `response` of its last `result` event. A result
    whose status is ERROR (a used-up quota) is the run's error; no result event at all is an empty answer. Lines
    that are not JSON events are skipped. The conversation id and token usage go to the receipt."""
    result: dict[str, Any] | None = None
    for line in stream.splitlines():
        try:
            event: Any = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or cast(dict[str, Any], event).get("event") != "result":
            continue
        payload: Any = cast(dict[str, Any], event).get("result")
        result = cast(dict[str, Any], payload) if isinstance(payload, dict) else cast(dict[str, Any], event)
    if result is None:
        return Decoded("", missing=True)
    session = result.get("conversation_id")
    raw_usage: Any = result.get("usage")
    usage = ({str(k): v for k, v in cast(dict[Any, Any], raw_usage).items()
              if isinstance(v, (int, float)) and not isinstance(v, bool)}
             if isinstance(raw_usage, dict) else None)
    response = result.get("response")
    error: str | None = None
    if str(result.get("status") or "").upper() == "ERROR":
        raw_error = result.get("error")
        error = (str(raw_error).strip() if raw_error else "") or "Antigravity reported an error"
    return Decoded("" if error else (response if isinstance(response, str) else ""), error,
                   session=session if isinstance(session, str) and session else None, usage=usage or None)


def _checkout_state(checkout: str) -> str:
    """The frozen checkout's `git status --porcelain`, untracked files included: empty while nothing changed it."""
    return git(checkout, "status", "--porcelain", "--untracked-files=all", check=False)


def _restore_checkout(checkout: str) -> None:
    """Put a changed review checkout back at its commit, so the next candidate reviews the change itself."""
    git(checkout, "reset", "--quiet", "--hard", check=False)
    git(checkout, "clean", "-fdq", check=False)


def _inline_diff(checkout: str, request: Request) -> str:
    """The diff itself, for a reviewer without a shell beyond git (Claude, Antigravity) and for every panel member."""
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


def _cli_option(data: dict[str, Any], cli: str, family: str | None) -> str:
    cli_table = data.get("cli")
    if isinstance(cli_table, dict):
        cli_entry = cast(dict[str, Any], cli_table).get(cli)
        if isinstance(cli_entry, dict):
            opt = cast(dict[str, Any], cli_entry).get("option")
            if isinstance(opt, str) and opt:
                return opt
    providers_table = data.get("providers")
    if family and isinstance(providers_table, dict):
        prov_entry = cast(dict[str, Any], providers_table).get(family)
        if isinstance(prov_entry, dict):
            opt = cast(dict[str, Any], prov_entry).get("option")
            if isinstance(opt, str) and opt:
                return opt
    return CLI_OPTIONS.get(cli) or (PROVIDER_OPTIONS.get(family) if family else None) or "effort"


def _cli_review_template(data: dict[str, Any], cli: str) -> tuple[str, ...]:
    cli_table = data.get("cli")
    if not isinstance(cli_table, dict):
        return ()
    cli_entry = cast(dict[str, Any], cli_table).get(cli)
    if not isinstance(cli_entry, dict):
        return ()
    entry = cast(dict[str, Any], cli_entry)
    # Only `review`: a `run` command implements and need not be read-only (fathers#201).
    raw = entry.get("review")
    if isinstance(raw, list) and raw and all(isinstance(item, str) for item in raw):  # pyright: ignore[reportUnknownVariableType]
        return tuple(str(item) for item in cast(list[Any], raw))
    return ()


def _models(project: Config) -> tuple[dict[str, Model], list[Model]]:
    """The models file: every model by alias (with the effort and limit of its first review choice), and the review
    role's choices in their order, a priority list (fathers#155); the defaults, Sol then Opus (with Known models
    registered for explicit `--reviewer`), when no file is found or it has no readable review role."""
    sol, opus = DEFAULTS
    default_models: dict[str, Model] = {m.alias: m for m in KNOWN_MODELS}
    default_choices: list[Model] = [sol, opus]
    path = _models_path(project)
    if path is None:
        return default_models, default_choices
    try:
        data = tomllib.loads(path.read_text())
        table: dict[str, Any] = data["models"]
        found: dict[str, Model] = dict(default_models)
        for alias, model in table.items():
            entry = cast(dict[str, Any], model) if isinstance(model, dict) else {}
            if isinstance(entry.get("id"), str) and isinstance(entry.get("cli"), str):
                cli = _normalize_cli(entry["cli"])
                raw_family = entry.get("family")
                family = raw_family if isinstance(raw_family, str) else CLI_FAMILIES.get(cli)
                option = _cli_option(data, cli, family)
                review_argv = _cli_review_template(data, cli) if cli == "antigravity" else ()
                found[alias] = Model(alias, entry["id"], cli, "medium",
                                     DEFAULT_MIN_LEFT if cli == "codex" else None,
                                     family, option=option, review_argv=review_argv)
        choices: list[Model] = []
        for choice in data["roles"]["review"]["choices"]:
            base = found[choice["model"]]
            when: dict[str, Any] = choice.get("when") or {}
            limits: dict[str, Any] = when.get("limit") or {}
            limit = limits.get("codex_weekly")
            # A Codex choice without a limit still needs DEFAULT_MIN_LEFT: no review runs on credits unread.
            min_left = (int(limit) if isinstance(limit, (int, float))
                        else DEFAULT_MIN_LEFT if base.cli == "codex" else None)
            note = choice.get("note")
            choices.append(Model(base.alias, base.id, base.cli, str(choice.get("effort", "medium")), min_left,
                                 base.family, bool(when.get("not_family")), note if isinstance(note, str) else None,
                                 base.option, base.review_argv))
        if not choices:
            raise ValueError("the review role lists no choices")
        for model in reversed(choices):
            found[model.alias] = model
        return found, choices
    except (OSError, KeyError, TypeError, ValueError, tomllib.TOMLDecodeError) as error:
        print(f"lane: warning: {path}: no review role ({error}); reviewing with the defaults", file=sys.stderr)
        return default_models, default_choices


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
    note = chosen.get("note")
    cli = _normalize_cli(str(chosen.get("cli") or ""))
    chosen_family = chosen.get("family") if isinstance(chosen.get("family"), str) else CLI_FAMILIES.get(cli)
    chosen_option = chosen.get("option") if isinstance(chosen.get("option"), str) else None
    review_argv: tuple[str, ...] = ()
    raw_argv = chosen.get("argv")
    if cli == "antigravity" and isinstance(raw_argv, dict):
        argv_map = cast(dict[str, Any], raw_argv)
        candidate = argv_map.get("review")
        if isinstance(candidate, list) and candidate and all(isinstance(x, str) for x in candidate):  # pyright: ignore[reportUnknownVariableType]
            review_argv = tuple(str(x) for x in cast(list[Any], candidate))
    model = Model(alias, str(chosen["id"]), cli, str(chosen.get("effort") or "medium"),
                  None, chosen_family, note=note if isinstance(note, str) else None,
                  option=chosen_option, review_argv=review_argv)
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
        try:
            child.kill()
            child.wait()
        finally:
            if child.stdin is not None:
                with contextlib.suppress(OSError):
                    child.stdin.close()
            reader.join(timeout=1)
            if child.stdout is not None and not child.stdout.closed:
                with contextlib.suppress(OSError):
                    child.stdout.close()
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
    explicit = project.env("CLAUDE")
    if explicit is not None:
        return explicit if explicit and os.access(explicit, os.X_OK) else None
    return shutil.which("claude")


def _antigravity_cli(project: Config) -> str | None:
    """The Antigravity CLI the review runs (fathers#201): LANEFATHER_ANTIGRAVITY or LANEFATHER_AGY; else
    ANTIGRAVITY_CLI_PATH or AGY_CLI_PATH from the environment; else `agy` or `antigravity` on PATH; else where the
    Antigravity installer puts it (~/.gemini/bin/agy); else the one T3 Code bundles. A set but empty override
    disables Antigravity."""
    for setting in ("ANTIGRAVITY", "AGY"):
        explicit = project.env(setting)
        if explicit is not None:
            stripped = explicit.strip()
            if not stripped:
                return None
            if " " in stripped and not os.path.exists(stripped):
                try:
                    parts = shlex.split(stripped)
                except ValueError:
                    return None
                if not parts:
                    return None
                first = parts[0]
            else:
                first = stripped
            return stripped if (os.access(first, os.X_OK) or shutil.which(first)) else None
    for env_var in ("ANTIGRAVITY_CLI_PATH", "AGY_CLI_PATH"):
        candidate = os.environ.get(env_var)
        if candidate is not None:
            return candidate if candidate and os.access(candidate, os.X_OK) else None
    found = shutil.which("agy") or shutil.which("antigravity")
    if found:
        return found
    home = Path(os.path.expanduser("~"))
    installed = home / ".gemini" / "bin" / "agy"
    if installed.is_file() and os.access(installed, os.X_OK):
        return str(installed)
    return _t3_antigravity(home)


def _t3_platform() -> str:
    """T3 Code's name for this machine's platform (darwin-arm64, linux-x64, ...)."""
    system = sys.platform if sys.platform in ("darwin", "win32") else sys.platform.split("-")[0]
    machine = platform.machine().lower()
    arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(machine, machine)
    return f"{system}-{arch}"


def _t3_antigravity(home: Path) -> str | None:
    """The antigravity binary T3 Code bundles: the release `active.json` names under
    ~/.t3/tools/antigravity-acp/<platform>/versions/; None when any part is missing or unreadable."""
    root = home / ".t3" / "tools" / "antigravity-acp" / _t3_platform()
    try:
        active: Any = json.loads((root / "active.json").read_text())
    except (OSError, ValueError):
        return None
    release = cast(dict[str, Any], active).get("releaseId") if isinstance(active, dict) else None
    if not isinstance(release, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", release) or release in (".", ".."):
        return None
    binary = root / "versions" / release / ("antigravity.exe" if sys.platform == "win32" else "antigravity")
    return str(binary) if binary.is_file() and os.access(binary, os.X_OK) else None


def _review_prompt(cfg: Config, request: Request, diff: str | None) -> str:
    """The reviewer gets the diff, the accepted scope and the coding standards; never the lane's report."""
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



def _extract_json_block(text: str) -> tuple[str | None, bool]:
    """Return `(raw_json, had_json_source)`: the last fenced ` ```json ` block, else the whole trimmed body if it
    starts with `{`. Never falls back past a malformed JSON block."""
    fenced = re.findall(r"```json[ \t]*\n(.*?)```", text, re.S)
    if fenced:
        return fenced[-1], True
    stripped = text.strip()
    if stripped.startswith("{"):
        return stripped, True
    return None, False


def parse_report_metadata(text: str) -> tuple[str | None, str | None]:
    """Extract optional `tip` and `tree` strings from a report's JSON block if present and valid."""
    block, has_json = _extract_json_block(text)
    if not has_json or block is None:
        return None, None
    try:
        data: Any = json.loads(block)
    except ValueError:
        return None, None
    if not isinstance(data, dict):
        return None, None
    table = cast(dict[str, Any], data)
    raw_tip = table.get("tip")
    raw_tree = table.get("tree")
    tip = raw_tip.strip() if isinstance(raw_tip, str) and raw_tip.strip() else None
    tree = raw_tree.strip() if isinstance(raw_tree, str) and raw_tree.strip() else None
    return tip, tree


def _parse_findings(text: str, *, strict: bool = False) -> list[dict[str, Any]]:
    """The findings of the last json block (or raw JSON body). Fails closed on malformed JSON or wrong-typed
    `findings`; when `strict=True` (used by `--record`), raises `LaneError` instead of returning an unreadable
    placeholder finding."""
    block, has_json = _extract_json_block(text)
    if has_json:
        assert block is not None
        try:
            data: Any = json.loads(block)
        except ValueError as error:
            if strict:
                raise LaneError(f"report JSON block is invalid: {error}") from None
            return _unreadable_answer(text)
        if not isinstance(data, dict) or "findings" not in data:
            if strict:
                raise LaneError("report JSON block must be an object with a 'findings' list")
            return _unreadable_answer(text)
        items: Any = cast(dict[str, Any], data).get("findings")
        if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):  # pyright: ignore[reportUnknownVariableType]
            if strict:
                raise LaneError("report JSON 'findings' must be a list of objects")
            return _unreadable_answer(text)
        found: list[dict[str, Any]] = []
        for item in cast(list[Any], items):
            entry = cast(dict[str, Any], item)
            raw_sev = entry.get("severity")
            severity = raw_sev.strip().upper() if isinstance(raw_sev, str) else ""
            raw_cat = entry.get("category")
            category = raw_cat.strip().lower() if isinstance(raw_cat, str) else ""
            package = entry.get("package") if category == "package-misuse" else None
            line = entry.get("line")
            raw_title = entry.get("title")
            raw_detail = entry.get("detail")
            found.append({
                "n": len(found) + 1,
                "severity": severity if severity in ("P0", *SEVERITIES) else "P2",
                "category": category if category in CATEGORIES else None,
                # An npm name is at most 214 characters.
                "package": (package.strip()[:214] if isinstance(package, str) and package.strip() else None),
                "file": entry.get("file") if isinstance(entry.get("file"), str) else None,
                "line": line if isinstance(line, int) and not isinstance(line, bool) else None,
                "title": raw_title.strip()[:300] if isinstance(raw_title, str) else "",
                "detail": raw_detail.strip()[:4000] if isinstance(raw_detail, str) else "",
                "mark": None,
            })
        return found
    if not strict and re.search(r"\bno findings\b", text, re.I) and len(text) < 400:
        return []
    if strict:
        raise LaneError("report has no readable JSON findings block")
    return _unreadable_answer(text)


parse_findings = _parse_findings


def resolve_reviewer(spec: str, project: Config) -> Model | None:
    """Resolve a reviewer alias or model spec against the project's models table."""
    models, choices = _models(project)
    return _resolve_reviewer(spec, models, choices)



def _unreadable_answer(text: str) -> list[dict[str, Any]]:
    return [{"n": 1, "severity": "P2", "category": None, "package": None, "file": None, "line": None, "mark": None,
             "title": "the review answer has no readable findings block: read it and mark it",
             "detail": text.strip()[:4000]}]



def _run_together(jobs: list[Job], cwd: str) -> list[tuple[int, float]]:
    """Start every job at once, each reading its own stdin file, and wait for all: each one's exit (124 on the
    timeout, 127 when it cannot start) and its seconds. Stopping this process stops them all."""
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
        for index, job in enumerate(jobs):
            try:
                with open(job.stdin) as stdin, open(job.stdout, "a") as out, open(job.log, "a") as err:
                    children.append(subprocess.Popen(job.argv, cwd=cwd, stdin=stdin, stdout=out,
                                                     stderr=err if job.stdout != job.log else subprocess.STDOUT,
                                                     text=True))
            except OSError as error:
                with open(job.log, "a") as err:
                    err.write(f"{job.argv[0]} did not start: {error}\n")
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
