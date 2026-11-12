"""A project's seams: `.lanefather.toml` at the repository top, and the environment settings.

lanefather keeps the lane workflow; a project names what is its own in `.lanefather.toml`: the verify command and
the plan module that the check contract runs, the stamp tool of the full gate, the install and deploy steps after a
promotion, the rule snippets and coding standards a lane brief and a review read. Every key is optional; a
repository without the file gets the defaults below, which suit a project with a `verify.sh` and nothing else.

The environment overrides a setting as LANEFATHER_<NAME>. A project that ran an older lane tool under another
prefix names it as `legacy_env_prefix` (Straylight: STRAYLIGHT_LANE_), and `[env]` maps single variables
(LANEFATHER_LOAD_WAIT = "STRAYLIGHT_LOAD_WAIT"), so the lanes of an older branch keep their settings.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess
import tomllib
from typing import Any, cast

from .errors import LaneError

CONFIG_NAME = ".lanefather.toml"
# Slotfather's request priorities, and the runs whose priority `[slots.priority]` and LANEFATHER_PRIORITY[_<PHASE>]
# set (lanefather#24).
PRIORITIES = ("interactive", "normal", "batch")
SLOT_PHASES = ("check", "post-merge", "gate", "rerun", "perf")


@dataclass(frozen=True)
class Lease:
    """An execution's machine budget and request fields; Slotfather owns admission and nested leases. Without a
    priority of its own the lease takes the gate's (slots.phase_priority("gate"), normal when nothing sets it)."""
    slots: int | str
    priority: str | None = None
    ticket: str | None = None


@dataclass(frozen=True)
class Step:
    """One install or deploy step: argv run in the gate worktree, with extra environment. `{sha}`, `{short}` (its
    first 10 characters), `{host}` and `{package}` in either are replaced."""
    name: str
    argv: tuple[str, ...]
    env: tuple[tuple[str, str], ...] = ()
    lease: Lease | None = None


@dataclass(frozen=True)
class Tickets:
    """The ticket lifecycle (`[tickets]`, lanefather.tickets): each rule is off unless the project turns it on."""
    integrated_label: bool = False
    land_comment: bool = False
    fixes_trailer: bool = False
    release_comment: bool = False
    # The page to try a landed merge on: `{sha}`, `{short}`, `{branch}`, `{ticket}` and `{lane}` are replaced.
    preview: str = ""
    # owner/name for gh; by default the GitHub repository of the origin remote.
    repo: str = ""

    def enabled(self) -> bool:
        return self.integrated_label or self.land_comment or self.fixes_trailer or self.release_comment


@dataclass(frozen=True)
class Config:
    path: Path | None = None
    project: str = "this project"
    # How messages name the command: Straylight's shim keeps `tools/lane`.
    command: str = "lanefather"
    state_dir: str = "~/.local/state/lanefather/lanes"
    legacy_env_prefix: str = ""
    # Settings the project's other tools read by their legacy names only (Straylight's janitor, idle watchdog and
    # hooks read STRAYLIGHT_LANE_STATE): a LANEFATHER_ value the legacy name does not carry is refused, so the tools
    # never read two state directories (review of #648, finding 3).
    shared_env: tuple[str, ...] = ()
    env_aliases: tuple[tuple[str, str], ...] = ()
    report_doc: str = "the implementer brief's report block"
    # Where the overall review's ticket goes, in its body ("the #347 queue").
    queue_name: str = "the queue"
    coding_standards: str = "CODING_STANDARDS.md"
    rules_dir: str = ""
    # What steers agents and the reviewer is reviewed even when it is Markdown.
    steering: tuple[str, ...] = (".agents/**", ".claude/**", "CODING_STANDARDS.md", "**/AGENTS.md", "**/CLAUDE.md")
    # Paths every lane may change, never claimed (release notes that merge by union).
    unclaimed: tuple[str, ...] = ()
    # `git -c` settings for land's merge (a union merge driver).
    merge_config: tuple[str, ...] = ()
    # The check contract (#649): verify --changed --base B --plan --json, --checks, --suites, --keep-going, --jobs.
    verify: str = "verify.sh"
    plan_module: str = ""
    triage: tuple[str, ...] = ()
    toolchain: tuple[str, ...] = ()
    toolchain_pin: str = ""
    # The variables a verify run that queues itself reads: <slot_env>_LABEL, _PRIORITY, _TICKET, _META, _YIELDABLE.
    slot_env: str = "LANEFATHER_SLOT"
    # [slots.priority]: a phase's (or `default`) Slotfather priority, below the environment's (lanefather#24).
    slot_priority: tuple[tuple[str, str], ...] = ()
    cache_name: str = "lanefather"
    lane_targets: tuple[str, ...] = ()
    cache_partitions: str = ""
    stamp_tool: str = ""
    stamp_file: str = "verified-trees"
    pre_push_hook: str = ".githooks/pre-push"
    ship_refuses: str = ""
    ship_refusal: str = "fold it into a release first"
    install: tuple[Step, ...] = ()
    deploy: tuple[Step, ...] = ()
    install_env_drop: tuple[str, ...] = ("LANDING_TOKEN",)
    after_promote: tuple[str, ...] = ()
    process_scan: str = ""
    project_hooks: tuple[tuple[str, str], ...] = ()
    gc_after_remove: tuple[str, ...] = ()
    gc_after_remove_report: bool = False
    admission_provider: str = "legacy"
    tickets: Tickets = Tickets()

    def names(self, name: str) -> list[str]:
        """The variables of setting `name`, first wins: LANEFATHER_<name>, an [env] alias, the legacy prefix's."""
        found = [f"LANEFATHER_{name}"]
        found += [legacy for key, legacy in self.env_aliases if key == f"LANEFATHER_{name}"]
        if self.legacy_env_prefix:
            found.append(self.legacy_env_prefix + name)
        return list(dict.fromkeys(found))

    def lookup(self, name: str) -> tuple[str, str] | None:
        """The first set variable of setting `name` and its value, or None. Two names of one setting with different
        values are refused, as is a shared setting (`shared_env`) set only as LANEFATHER_<name>."""
        names = self.names(name)
        found = [(key, os.environ[key]) for key in names if key in os.environ]
        if not found:
            return None
        if len({value for _, value in found}) > 1:
            raise LaneError(" and ".join(f"{key}={value!r}" for key, value in found)
                            + f" differ: give setting {name} one value")
        if name in self.shared_env and len(names) > 1 and [key for key, _ in found] == [names[0]]:
            raise LaneError(f"{names[0]} is set, but {' or '.join(names[1:])} is not: the project's other tools read "
                            f"only the latter, so set it there (or both, equal)")
        return found[0]

    def env(self, name: str) -> str | None:
        """The value of setting `name` in the environment, or None."""
        found = self.lookup(name)
        return found[1] if found else None

    def env_names(self, name: str) -> str:
        """The variable names of a setting, for messages."""
        return " or ".join(self.names(name))


def _strings(data: dict[str, Any], key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    value = data.get(key, default)
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) for v in value):  # pyright: ignore[reportUnknownVariableType]
        raise LaneError(f"{CONFIG_NAME}: {key} must be a string or a list of strings")
    return tuple(value)  # pyright: ignore[reportUnknownArgumentType]


def _string(data: dict[str, Any], key: str, default: str) -> str:
    value = data.get(key, default)
    if not isinstance(value, str):
        raise LaneError(f"{CONFIG_NAME}: {key} must be a string")
    return value


def _step_lease(table: dict[str, Any], name: str) -> Lease | None:
    if "lease" not in table:
        return None
    raw = table["lease"]
    if not isinstance(raw, dict):
        raise LaneError(f"{CONFIG_NAME}: step {name}: lease must be a table with slots")
    lease = cast(dict[str, Any], raw)
    _known(lease, f"step {name} lease", ("slots", "priority", "ticket"))
    count = lease.get("slots")
    if not ((type(count) is int and count > 0) or count == "all"):
        raise LaneError(f"{CONFIG_NAME}: step {name}: lease slots must be a positive integer or 'all'")
    priority = _string(lease, "priority", "") if "priority" in lease else None
    if priority is not None and priority not in PRIORITIES:
        raise LaneError(f"{CONFIG_NAME}: step {name}: lease priority must be interactive, normal or batch")
    ticket = _string(lease, "ticket", "") if "ticket" in lease else None
    if ticket is not None and (not ticket or len(ticket) > 200 or not ticket.isprintable()):
        raise LaneError(f"{CONFIG_NAME}: step {name}: lease ticket must be a short printable identifier")
    return Lease(count, priority, ticket)


def _slot_priority(slots: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    raw = slots.get("priority", {})
    if not isinstance(raw, dict):
        raise LaneError(f"{CONFIG_NAME}: [slots.priority] must be a table of phase = priority")
    table = cast(dict[str, Any], raw)
    _known(table, "slots.priority", (*SLOT_PHASES, "default"))
    for key, value in table.items():
        if value not in PRIORITIES:
            raise LaneError(f"{CONFIG_NAME}: [slots.priority] {key} = {value!r} is not a Slotfather priority; "
                            f"use {', '.join(PRIORITIES)}")
    return tuple(sorted((key, str(value)) for key, value in table.items()))


def _steps(data: dict[str, Any], key: str) -> tuple[Step, ...]:
    raw = data.get(key, [])
    if not isinstance(raw, list):
        raise LaneError(f"{CONFIG_NAME}: {key} must be an array of tables, [[promote.{key}]]")
    steps: list[Step] = []
    for item in raw:  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):  # pyright: ignore[reportUnknownMemberType]
            raise LaneError(f"{CONFIG_NAME}: each {key} step needs a name and an argv")
        table: dict[str, Any] = item  # pyright: ignore[reportUnknownVariableType]
        env = table.get("env", {})
        if not isinstance(env, dict) or not all(isinstance(v, str) for v in env.values()):  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
            raise LaneError(f"{CONFIG_NAME}: {key} step {table['name']}: env must map names to strings")
        env_items: dict[str, str] = env  # pyright: ignore[reportUnknownVariableType]
        argv = _strings(table, "argv", ())
        if not table["name"].strip() or not argv or not argv[0]:
            raise LaneError(f"{CONFIG_NAME}: each {key} step needs a nonempty name and argv")
        steps.append(Step(table["name"], argv, tuple(sorted(env_items.items())), _step_lease(table, table["name"])))
    return tuple(steps)


TOP_KEYS = ("project", "command", "state_dir", "legacy_env_prefix", "shared_env", "report_doc", "queue_name",
            "coding_standards", "rules_dir", "steering", "unclaimed", "merge_config", "checks", "lane", "gate",
            "promote", "hooks", "env", "gc", "admission", "tickets", "slots")
TABLE_KEYS = {
    "admission": ("provider",),
    "checks": ("verify", "plan_module", "triage", "toolchain", "toolchain_pin", "slot_env"),
    "lane": ("cache_name", "targets", "cache_partitions"),
    "gate": ("stamp_tool", "stamp_file", "pre_push_hook"),
    "promote": ("ship_refuses", "ship_refusal", "install", "deploy", "install_env_drop", "after"),
    "hooks": ("process_scan", "project"),
    "gc": ("after_remove", "after_remove_report"),
    "tickets": ("integrated_label", "land_comment", "fixes_trailer", "release_comment", "preview", "repo"),
    "slots": ("priority",),
}


def _known(table: dict[str, Any], name: str, keys: tuple[str, ...]) -> None:
    """Refuse a key lanefather does not read: a typo (`afer`) would otherwise drop a seam without a word."""
    unknown = sorted(set(table) - set(keys))
    if unknown:
        where = f"[{name}] " if name else ""
        raise LaneError(f"{CONFIG_NAME}: {where}has unknown keys {', '.join(unknown)}; known: {', '.join(keys)}")


def _tickets(table: dict[str, Any]) -> Tickets:
    flags: dict[str, bool] = {}
    for key in ("integrated_label", "land_comment", "fixes_trailer", "release_comment"):
        value = table.get(key, False)
        if type(value) is not bool:
            raise LaneError(f"{CONFIG_NAME}: [tickets] {key} must be a boolean")
        flags[key] = value
    repo = _string(table, "repo", "")
    if repo and not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        raise LaneError(f"{CONFIG_NAME}: [tickets] repo must be owner/name")
    return Tickets(**flags, preview=_string(table, "preview", ""), repo=repo)


def parse(data: dict[str, Any], path: Path | None = None) -> Config:
    """The config of a parsed `.lanefather.toml`."""
    checks: dict[str, Any] = data.get("checks", {})
    lane: dict[str, Any] = data.get("lane", {})
    gate: dict[str, Any] = data.get("gate", {})
    promote: dict[str, Any] = data.get("promote", {})
    hooks: dict[str, Any] = data.get("hooks", {})
    env: dict[str, Any] = data.get("env", {})
    gc: dict[str, Any] = data.get("gc", {})
    admission: dict[str, Any] = data.get("admission", {})
    tickets: dict[str, Any] = data.get("tickets", {})
    slot_table: dict[str, Any] = data.get("slots", {})
    for name, table in (("checks", checks), ("lane", lane), ("gate", gate), ("promote", promote), ("hooks", hooks),
                        ("env", env), ("gc", gc), ("admission", admission), ("tickets", tickets),
                        ("slots", slot_table)):
        if not isinstance(table, dict):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise LaneError(f"{CONFIG_NAME}: [{name}] must be a table")
    if not all(isinstance(v, str) for v in env.values()):
        raise LaneError(f"{CONFIG_NAME}: [env] maps LANEFATHER_<NAME> to the project's variable name")
    _known(data, "", TOP_KEYS)
    for name, table in (("checks", checks), ("lane", lane), ("gate", gate), ("promote", promote),
                        ("hooks", hooks), ("gc", gc), ("admission", admission), ("tickets", tickets),
                        ("slots", slot_table)):
        _known(table, name, TABLE_KEYS[name])
    for key in ("install", "deploy"):
        steps: list[Any] = promote.get(key, []) if isinstance(promote.get(key), list) else []
        for step in steps:
            if isinstance(step, dict):
                _known(step, f"promote.{key}", ("name", "argv", "env", "lease"))  # pyright: ignore[reportUnknownArgumentType]
    raw_hooks: Any = hooks.get("project", {})
    if not isinstance(raw_hooks, dict):
        raise LaneError(f"{CONFIG_NAME}: [hooks.project] maps hook names to script paths")
    project_hooks = cast(dict[str, Any], raw_hooks)
    if not all(name and isinstance(script, str) and script for name, script in project_hooks.items()):
        raise LaneError(f"{CONFIG_NAME}: [hooks.project] maps hook names to script paths")
    default = Config()
    gc_report = gc.get("after_remove_report", default.gc_after_remove_report)
    if type(gc_report) is not bool:
        raise LaneError(f"{CONFIG_NAME}: after_remove_report must be a boolean")
    provider = _string(admission, "provider", default.admission_provider)
    if provider not in ("legacy", "slotfather"):
        raise LaneError(".lanefather.toml: admission provider must be legacy or slotfather")
    return Config(
        path=path,
        project=_string(data, "project", default.project),
        command=_string(data, "command", default.command),
        state_dir=_string(data, "state_dir", default.state_dir),
        legacy_env_prefix=_string(data, "legacy_env_prefix", default.legacy_env_prefix),
        shared_env=_strings(data, "shared_env", default.shared_env),
        env_aliases=tuple(sorted((k, str(v)) for k, v in env.items())),
        report_doc=_string(data, "report_doc", default.report_doc),
        queue_name=_string(data, "queue_name", default.queue_name),
        coding_standards=_string(data, "coding_standards", default.coding_standards),
        rules_dir=_string(data, "rules_dir", default.rules_dir),
        steering=_strings(data, "steering", default.steering),
        unclaimed=_strings(data, "unclaimed", default.unclaimed),
        merge_config=_strings(data, "merge_config", default.merge_config),
        verify=_string(checks, "verify", default.verify),
        plan_module=_string(checks, "plan_module", default.plan_module),
        triage=_strings(checks, "triage", default.triage),
        toolchain=_strings(checks, "toolchain", default.toolchain),
        toolchain_pin=_string(checks, "toolchain_pin", default.toolchain_pin),
        slot_env=_string(checks, "slot_env", default.slot_env),
        slot_priority=_slot_priority(slot_table),
        cache_name=_string(lane, "cache_name", default.cache_name),
        lane_targets=_strings(lane, "targets", default.lane_targets),
        cache_partitions=_string(lane, "cache_partitions", default.cache_partitions),
        stamp_tool=_string(gate, "stamp_tool", default.stamp_tool),
        stamp_file=_string(gate, "stamp_file", default.stamp_file),
        pre_push_hook=_string(gate, "pre_push_hook", default.pre_push_hook),
        ship_refuses=_string(promote, "ship_refuses", default.ship_refuses),
        ship_refusal=_string(promote, "ship_refusal", default.ship_refusal),
        install=_steps(promote, "install"),
        deploy=_steps(promote, "deploy"),
        install_env_drop=_strings(promote, "install_env_drop", default.install_env_drop),
        after_promote=_strings(promote, "after", default.after_promote),
        process_scan=_string(hooks, "process_scan", default.process_scan),
        project_hooks=tuple(sorted((str(name), str(script)) for name, script in project_hooks.items())),
        gc_after_remove=_strings(gc, "after_remove", default.gc_after_remove),
        gc_after_remove_report=gc_report,
        admission_provider=provider,
        tickets=_tickets(tickets),
    )


def find(cwd: str | os.PathLike[str]) -> Path | None:
    """LANEFATHER_CONFIG, else `.lanefather.toml` at the top of the repository `cwd` is in."""
    configured = os.environ.get("LANEFATHER_CONFIG")
    if configured:
        return Path(os.path.expanduser(configured))
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd, capture_output=True, text=True)
    if top.returncode:
        return None
    path = Path(top.stdout.strip()) / CONFIG_NAME
    return path if path.is_file() else None


def load(path: Path | None) -> Config:
    if path is None:
        return Config()
    try:
        data = tomllib.loads(path.read_text())
    except FileNotFoundError:
        raise LaneError(f"no config at {path}") from None
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise LaneError(f"{path}: {error}") from None
    return parse(data, path.resolve())


_current: Config | None = None


def current() -> Config:
    """The config of this run: the one `use` set, else the one found from the working directory."""
    global _current
    if _current is None:
        _current = load(find(os.getcwd()))
    return _current


def use(config: Config | None) -> None:
    """Set the config of this run (the command line's --config); None finds it again on the next use."""
    global _current
    _current = config


def prog() -> str:
    """How messages name the command. An unreadable configuration names it `lanefather`, so the machine-wide
    commands (capabilities, status, settings) still run in such a repository; any command that needs the
    configuration reports its error when it reads it."""
    try:
        return current().command
    except LaneError:
        return Config().command
