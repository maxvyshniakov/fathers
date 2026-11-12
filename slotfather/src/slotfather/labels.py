"""A meaningful default label (fathers#100): the project (the git repository of the request's directory), the part of
the command that names the work, and the lane, ticket and phase of its metadata, such as `godfather · measure_cpu` or
`straylight · lane 743 · check`, where a request gives no `--label`, or one that says little (`scratchpad`, `tmp`, the
name of its directory when that is a temporary one, or the project's name alone).

Parts are joined by SEP. schedule.family() reads the part before the first SEP, so the requests of one project form
one family as before, while estimates still prefer the whole label.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
from typing import Callable

from . import config as _config

SEP = " · "
MAX = 80  # characters of a derived label
GENERIC = frozenset(("scratchpad", "scratch", "tmp", "temp", "test", "tests", "desktop", "work", "workspace", "home",
                     "src", "run", "job", "task", "build", "check", "default", "untitled", "x", "probe", "repo"))
WRAPPERS = frozenset(("env", "nice", "nohup", "time", "caffeinate", "taskpolicy", "timeout", "gtimeout", "stdbuf",
                      "exec", "command", "sudo", "arch", "xcrun"))
SHELLS = frozenset(("sh", "bash", "zsh", "dash", "ksh", "fish"))
INTERPRETERS = frozenset(("python", "python3", "pypy3", "node", "bun", "deno", "ruby", "perl", "php", "tsx", "ts-node"))
RUNNERS = frozenset(("npx", "bunx", "pnpx"))  # run the program that follows
SCRIPT_RUNNERS = frozenset(("npm", "pnpm", "yarn", "bun"))  # `bun run build`, `npm test`: the script names the work
QUEUE_WRAPPERS = frozenset(("slotfather", "slot", "heavy", "lanefather", "machine-slot"))  # the command after `--`
SKIPPED = frozenset(("cd", "set", "export", "source", ".", "true", ":", "unset", "ulimit", "trap", "umask"))
SCRIPT_DIRS = frozenset(("bin", "scripts", "script", "tools", "tool"))
DROPPED_SUFFIXES = (".py", ".mjs", ".cjs", ".js", ".ts", ".mts", ".tsx", ".rb", ".pl")
WORD = re.compile(r"[A-Za-z][A-Za-z0-9_:+-]{0,23}")
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
# Options that take the next word as their value, per program, so that the value is not read as the work.
VALUED = {
    "python": {"-X", "-W", "-c", "-Q"},
    "node": {"-r", "--require", "--import", "--loader", "--experimental-loader", "--test-name-pattern",
             "--test-reporter", "--test-reporter-destination", "--conditions", "-C", "--env-file", "--input-type"},
    "uv": {"--with", "--with-requirements", "--with-editable", "--python", "-p", "--project", "--directory",
           "--extra", "--group", "--package", "--env-file", "--index", "--index-url", "--from"},
    "env": {"-u", "--unset", "-C", "--chdir", "-S"},
    "nice": {"-n"}, "taskpolicy": {"-c", "-t", "-l"}, "timeout": {"-s", "--signal", "-k", "--kill-after"},
    "bun": {"--filter", "--cwd", "--config", "-c", "--env-file", "--preload", "-r"},
    "npm": {"--prefix", "-w", "--workspace"}, "pnpm": {"--filter", "-F", "-C", "--dir"},
    "caffeinate": {"-t", "-w"}, "sudo": {"-u", "-g", "-h", "-p"}, "gtimeout": {"-s", "--signal", "-k", "--kill-after"},
    "make": {"-C", "-f", "-j", "-I", "-o", "-W"}, "gmake": {"-C", "-f", "-j", "-I"},
    "xcodebuild": {"-scheme", "-project", "-workspace", "-destination", "-configuration", "-sdk", "-target",
                   "-derivedDataPath", "-resultBundlePath", "-testPlan", "-only-testing", "-skip-testing"},
    "cargo": {"-p", "--package", "--target-dir", "--manifest-path", "--features", "-j", "--jobs", "--target"},
    "swift": {"--package-path", "-c", "--configuration", "--filter", "--scratch-path", "-j"},
    "git": {"-C", "-c"}, "docker": {"-f", "--file", "-p", "--project-name", "--context"},
    "slotfather": {"-n", "--slots", "-l", "--label", "--meta", "--priority", "--ticket", "--wait", "--session"},
}
TEMP_NAMES = frozenset(("scratchpad",))  # an agent's scratch directory, wherever it lies
CONTROL = re.compile("[\x00-\x1f\x7f-\x9f]+")
KEYWORDS = frozenset(("then", "do", "else", "elif", "!", "{", "time"))  # words before a command in a script
CONTROL_WORDS = frozenset(("if", "while", "until", "for", "case", "fi", "done", "esac", "[", "[[", "test", "}",
                           "function", "select"))


def temp_roots() -> list[str]:
    """Directories whose contents are never a project: the system's temporary directories, or the colon-separated
    SLOTFATHER_TEMP_ROOTS."""
    given = _config.setting("TEMP_ROOTS")
    if given is not None:
        return [p for p in given.split(":") if p]
    roots = ["/tmp", "/private/tmp", "/var/folders", "/private/var/folders", tempfile.gettempdir()]
    return [os.path.realpath(r) for r in roots] + roots


def temporary(path: str) -> bool:
    """Whether `path` is a temporary or agent scratch directory, or lies in one."""
    if not path:
        return True
    for candidate in (os.path.realpath(path), path):
        if any(candidate == r or candidate.startswith(r.rstrip("/") + "/") for r in temp_roots()):
            return True
    return any(part.lower() in TEMP_NAMES for part in Path(path).parts)


def repository(path: str) -> str | None:
    """The name of the git repository `path` is in (a linked worktree names its main repository), with the
    top-level directory `path` lies in when it is below the top (`fathers/slotfather`: packages of one repository
    stay apart), or None outside one, in a temporary directory, or where git is missing or slow."""
    if not path or temporary(path) or (_config.setting("GIT_FACTS") or "1") == "0":
        return None
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
    try:
        found = subprocess.run(["git", "-C", path, "rev-parse", "--show-toplevel", "--git-common-dir"],
                               capture_output=True, text=True, timeout=1.0, env=env, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError):
        return None
    lines = found.stdout.splitlines()
    if found.returncode or len(lines) != 2:
        return None
    top, common = lines
    if temporary(top):
        return None
    common_path = Path(common if os.path.isabs(common) else os.path.join(path, common)).resolve()
    if common_path.name == ".git":
        name = common_path.parent.name
    else:
        name = common_path.name[:-4] if common_path.name.endswith(".git") else Path(top).name
    if not name:
        return None
    below = os.path.relpath(os.path.realpath(path), os.path.realpath(top))
    first = below.split(os.sep)[0]
    return f"{name}/{first}" if below != "." and first not in ("..", "") and not first.startswith(".") else name


def package_of(script: str) -> str | None:
    """The package a script inside node_modules belongs to (`.../node_modules/@scope/repo-kit/src/cli.mjs`)."""
    parts: list[str] = list(Path(script).parts)
    if "node_modules" not in parts:
        return None
    at = len(parts) - 1 - parts[::-1].index("node_modules")
    rest = parts[at + 1:]
    if not rest:
        return None
    if rest[0].startswith("@") and len(rest) > 1:
        return rest[1] if rest[1] != ".bin" else None
    return None if rest[0] == ".bin" else rest[0]


def script_name(path: str) -> str:
    """`tools/measure_cpu.py` -> measure_cpu, `bin/check` -> bin/check, `./verify.sh` -> verify.sh."""
    name = os.path.basename(path.rstrip("/")) or path
    for suffix in DROPPED_SUFFIXES:
        if name.endswith(suffix) and len(name) > len(suffix):
            return name[:-len(suffix)]
    parent = os.path.basename(os.path.dirname(path))
    if "." not in name and parent in SCRIPT_DIRS:
        return f"{parent}/{name}"
    return name


def program_name(word: str) -> str:
    base = os.path.basename(word)
    if re.fullmatch(r"python\d+(\.\d+)?", base):
        return "python3" if base.startswith("python3") else "python"
    return base


def options_skipped(argv: list[str], valued: set[str]) -> list[str]:
    """argv without its leading options (and their values)."""
    at = 0
    while at < len(argv) and argv[at].startswith("-") and argv[at] not in ("-", "--"):
        at += 2 if argv[at] in valued else 1
    if at < len(argv) and argv[at] == "--":
        at += 1
    return argv[at:]


def subcommand(rest: list[str], valued: set[str] | None = None) -> str | None:
    """The first plain word among `rest` once options are skipped: `run` of `vitest --config x.ts run src`."""
    at = 0
    valued = valued or set()
    while at < len(rest):
        word = rest[at]
        if word == "--":
            return None
        if word.startswith("-"):
            at += 2 if word in valued else 1
            # an option's value (`--config vitest.config.ts`) is no subcommand: skip a word that cannot be one
            if word not in valued and "=" not in word and at < len(rest) and not rest[at].startswith("-") \
                    and not WORD.fullmatch(rest[at]):
                at += 1
            continue
        return word if WORD.fullmatch(word) else None
    return None


def after_separator(argv: list[str]) -> list[str] | None:
    return argv[argv.index("--") + 1:] if "--" in argv else None


def shell_script(text: str) -> list[str] | None:
    """The first command of a shell script that does work: `set -e && cd tests && python3 -m unittest` gives
    python3 -m unittest."""
    try:
        lexer = shlex.shlex(text, posix=True, punctuation_chars=";&|()")
        lexer.whitespace_split = True
        words = list(lexer)
    except ValueError:
        words = text.split()
    command: list[str] = []
    for word in words + [";"]:
        if word and all(c in ";&|()" for c in word):
            plain = [w for w in command if not ASSIGNMENT.match(w)]
            while plain and plain[0] in KEYWORDS:
                plain = plain[1:]
            if plain and plain[0] not in SKIPPED and plain[0] not in CONTROL_WORDS:
                return plain
            command = []
        else:
            command.append(word)
    return None


def work(argv: list[str], depth: int = 0) -> tuple[str | None, str | None]:
    """(the part of the command that names the work, the script it runs if any)."""
    argv = [a for a in argv if a != ""]
    while argv and ASSIGNMENT.match(argv[0]):
        argv = argv[1:]
    if not argv or depth > 6:
        return None, None
    program = program_name(argv[0])
    rest = argv[1:]
    if program in WRAPPERS:
        if program in ("timeout", "gtimeout"):
            rest = options_skipped(rest, VALUED["timeout"])[1:]  # its duration
        else:
            rest = options_skipped(rest, VALUED.get(program, set()))
        while rest and ASSIGNMENT.match(rest[0]):
            rest = rest[1:]
        return work(rest, depth + 1)
    if program in SHELLS:
        flags = [at for at, word in enumerate(rest[:3]) if re.fullmatch(r"-[a-zA-Z]*c[a-zA-Z]*", word)]
        if flags:  # -c, and -lc, -ec, -euc: the script follows
            at = flags[0] + 1
            inner = shell_script(rest[at]) if at < len(rest) else None
            return work(inner, depth + 1) if inner else (program, None)
        rest = options_skipped(rest, set())
        return (script_name(rest[0]), rest[0]) if rest else (program, None)
    if program == "uv" and rest[:1] in (["run"], ["tool"]):
        rest = rest[1:] if rest[0] == "run" else rest[2:]
        return work(options_skipped(rest, VALUED["uv"]), depth + 1)
    if program == "uvx" or program in RUNNERS:
        return work(options_skipped(rest, VALUED["uv"] | {"-p", "--package"}), depth + 1)
    if program in QUEUE_WRAPPERS:
        inner = after_separator(rest)
        if inner:
            return work(inner, depth + 1)
    if program in SCRIPT_RUNNERS:
        valued = VALUED.get(program, set())
        rest = options_skipped(rest, valued)
        if rest[:1] in (["run"], ["exec"], ["x"]):
            verb, rest = rest[0], options_skipped(rest[1:], valued)
            if verb in ("exec", "x"):
                return work(rest, depth + 1)
            return (f"{program} run {rest[0]}" if rest and WORD.fullmatch(rest[0]) else f"{program} run"), None
        if program == "bun" and rest and ("/" in rest[0] or "." in rest[0]):
            return named_script(rest[0], rest[1:])
        word = subcommand(rest, valued)
        return (f"{program} {word}" if word else program), None
    if program in INTERPRETERS:
        family = "python" if program.startswith("python") or program == "pypy3" else program
        valued = VALUED.get(family, set())
        if family == "python":
            at = 0
            while at < len(rest) and rest[at].startswith("-") and rest[at] != "-":
                if rest[at] == "-m" and at + 1 < len(rest):
                    module = rest[at + 1]
                    if module in ("slotfather", "lanefather"):
                        inner = after_separator(rest[at + 2:])
                        if inner:
                            return work(inner, depth + 1)
                    word = subcommand(rest[at + 2:])
                    return (f"{module} {word}" if word else module), None
                if rest[at] == "-c":
                    return f"{program} -c", None
                at += 2 if rest[at] in valued else 1
            rest = rest[at:]
        else:
            if any(word in ("-e", "--eval", "-p", "--print") for word in rest[:3]):
                return f"{program} -e", None  # code on the command line: its text names nothing
            if family == "node" and "--test" in rest and not options_skipped(rest, valued):
                return "node --test", None
            rest = options_skipped(rest, valued)
        if not rest:
            return program, None
        if rest[0] == "-":
            return f"{program} -", None
        return named_script(rest[0], rest[1:], depth)
    installed = os.path.isabs(argv[0]) or any(part in ("node_modules", ".venv", "venv", ".bin")
                                               for part in Path(argv[0]).parts)
    if not installed and ("/" in argv[0] or "." in os.path.basename(argv[0])):
        return named_script(argv[0], rest, depth)
    if installed and "." in program:
        return named_script(argv[0], rest, depth)  # an absolute script path (tools/heavy.py, run.mjs)
    valued = VALUED.get(program, set())
    word = subcommand(rest, valued)
    return (f"{program} {word}" if word else program), None


def named_script(script: str, rest: list[str], depth: int = 0) -> tuple[str | None, str | None]:
    """A script and the subcommand after it; a queue wrapper's (tools/heavy.py, lanefather.mjs) command after `--`."""
    name = script_name(script)
    package = package_of(script)
    if os.path.basename(name) in QUEUE_WRAPPERS or package in QUEUE_WRAPPERS:
        inner = after_separator(rest)
        if inner:
            return work(inner, depth + 1)
    if package:
        name = package if os.path.basename(script).split(".")[0] in ("cli", "index", "main", "bin") else name
    word = subcommand(rest)
    if word and word != "--":
        name = f"{name} {word}"
    return name, script


def project_of(cwd: str, script: str | None, look: Callable[[str], str | None] = repository) -> str | None:
    """The repository of `cwd`; else that of the script; else the directory a script's tools/, bin/, scripts/ or src/
    sits in, unless temporary paths all the way."""
    found = look(cwd) if cwd else None
    if found or not script:
        return found
    path = script if os.path.isabs(script) else os.path.join(cwd or ".", script)
    folder = os.path.dirname(path)
    found = look(folder) if folder and os.path.isdir(folder) else None
    if found:
        return found
    if not cwd or not os.path.normpath(path).startswith(os.path.normpath(cwd).rstrip("/") + "/"):
        return None  # a path outside cwd (/usr/local/bin/x, ~/.local/state/...) names no project by its folders
    parts = Path(os.path.normpath(path)).parts
    for at in range(len(parts) - 2, 0, -1):
        if parts[at] in SCRIPT_DIRS | {"src", "tests", "test", "lib"}:
            owner = parts[at - 1]
            if owner.lower() not in TEMP_NAMES and owner not in ("/", "") and not owner.startswith("."):
                return owner
            break
    return None


def lane_part(meta: dict[str, str], ticket: str | None) -> str | None:
    lane = meta.get("lane")
    if lane:
        return f"lane {lane}"[:40]
    return f"#{ticket}" if ticket else None


def derive(cwd: str, argv: list[str], meta: dict[str, str] | None = None, ticket: str | None = None,
           look: Callable[[str], str | None] = repository) -> str | None:
    """The label the request's facts give, or None when they give nothing."""
    return derive_parts(cwd, argv, meta, ticket, look)[0]


def derive_parts(cwd: str, argv: list[str], meta: dict[str, str] | None = None, ticket: str | None = None,
                 look: Callable[[str], str | None] = repository) -> tuple[str | None, str | None]:
    """(the derived label, the project it found)."""
    meta = meta or {}
    name, script = work(list(argv))
    project = project_of(cwd, script, look)
    parts = [project, lane_part(meta, ticket), name]
    phase = meta.get("phase")
    if phase and phase not in (name or "").split():
        parts.append(phase)
    found = [CONTROL.sub(" ", p).strip() for p in parts if p]
    found = [p for p in found if p]
    if not found or (len(found) == 1 and found[0] == project and not name):
        return project, project
    label = SEP.join(dict.fromkeys(found))  # no part twice
    return (label if len(label) <= MAX else label[:MAX - 1] + "…"), project


def generic(label: str, cwd: str, project: str | None) -> bool:
    """A label that says little: a generic word, the name of a temporary directory it runs in, or the project's
    name alone."""
    word = label.strip().lower()
    if word in GENERIC:
        return True
    base = os.path.basename(cwd.rstrip("/")) if cwd else ""
    if label == base and temporary(cwd):
        return True
    return bool(project) and label == project


def choose(given: str | None, cwd: str, argv: list[str], meta: dict[str, str] | None = None,
           ticket: str | None = None, look: Callable[[str], str | None] = repository) -> dict[str, str | None]:
    """{"label": what the request is called, "given": the --label (None without one), "derived": the label its
    facts give (None when they give nothing), "hint": a line for `run` to print, or None}. A good --label stays as
    it is; a missing one is the derived label (else the directory's name, as before); a generic one gets the
    derived parts it lacks appended."""
    base = os.path.basename(cwd.rstrip("/")) if cwd else ""
    derived, project = derive_parts(cwd, argv, meta, ticket, look)
    if given is None:
        return {"label": derived or base or "request", "given": None, "derived": derived, "hint": None,
                "project": project}
    if not derived or not generic(given, cwd, project):
        return {"label": given, "given": given, "derived": derived, "hint": None, "project": project}
    rest = [p for p in derived.split(SEP) if p != given]
    if not rest:
        return {"label": given, "given": given, "derived": derived, "hint": None, "project": project}
    # the derived parts first: the family (schedule.family()) is then the project, never the generic word
    label = SEP.join([*rest, given]) if project and project != given else SEP.join([given, *rest])
    return {"label": label, "given": given, "derived": derived, "project": project,
            "hint": f"hint: the label {given!r} says little about the work; it runs as {label!r} (a --label that "
                    f"names the work replaces it)"}
