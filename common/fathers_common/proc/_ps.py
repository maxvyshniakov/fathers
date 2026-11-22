"""Running `ps` and reading its rows by column (fathers#171, fail-closed per fathers#121)."""
from __future__ import annotations

import os
import subprocess
from typing import Callable, Mapping, Sequence

from ._lstart import Clock
from ._lstart import parse as parse_lstart
from ._table import Proc


class ProcTableError(Exception):
    """A process table (or listing) that could not be read: never taken for "nothing runs". `detail` is the cause
    without the command's name, for callers that word the message themselves."""

    def __init__(self, message: str, detail: str | None = None) -> None:
        super().__init__(message)
        self.detail = message if detail is None else detail


def environment(clock: Clock | None = "utc", base: Mapping[str, str] | None = None) -> dict[str, str]:
    """`base` (default this process's environment) in the C locale, so ps prints its words in English; with
    clock "utc" also TZ=UTC, so every caller reads the same lstart whatever its own TZ. "local" (or None) keeps the
    caller's TZ."""
    env = dict(os.environ if base is None else base)
    env["LC_ALL"] = "C"
    if clock == "utc":
        env["TZ"] = "UTC"
    return env


Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def run(argv: Sequence[str], *, env: Mapping[str, str] | None = None, timeout: float,
        ok: Sequence[int] | None = None, runner: Runner | None = None) -> subprocess.CompletedProcess[str]:
    """One run of `argv` (through `runner`, default subprocess.run). Raises ProcTableError when it cannot start,
    takes longer than `timeout`, or (with `ok`) exits with a status outside `ok`; without `ok` the status is the
    caller's to judge (`ps -p` exits 1 when some of the pids are gone)."""
    name = argv[0] if argv else "?"
    try:
        result = (subprocess.run if runner is None else runner)(
            list(argv), capture_output=True, text=True, env=None if env is None else dict(env), timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise ProcTableError(f"{name} did not answer in {timeout:.0f} s", str(error)) from None
    except OSError as error:
        raise ProcTableError(f"{name}: {error}", str(error)) from None
    if ok is not None and result.returncode not in ok:
        raise ProcTableError(f"{' '.join(argv[:2])} exited {result.returncode}: {result.stderr.strip()[:200]}",
                             f"{name} exited {result.returncode}")
    return result


def rows(argv: Sequence[str], *, caller: bool = False, env: Mapping[str, str] | None = None,
         timeout: float) -> list[str]:
    """The rows (lines whose first word is a pid) of a ps over the machine (run(), then table_rows())."""
    return table_rows(run(argv, env=env, timeout=timeout), caller=caller,
                      name=os.path.basename(argv[0]) if argv else "ps")


def table_rows(result: subprocess.CompletedProcess[str], *, caller: bool = False, name: str = "ps") -> list[str]:
    """The rows (lines whose first word is a pid) of a finished ps over the machine. Raises ProcTableError for a ps
    that exited non-zero, a table without a row and, with `caller`, a table that leaves out this process: a filtered
    or cut table would read the processes it hides as gone (fathers#121)."""
    if result.returncode:
        raise ProcTableError(f"{name} exited {result.returncode}: {result.stderr.strip()[:200]}",
                             f"{name} exited {result.returncode}")
    found = [line for line in result.stdout.splitlines() if (line.split() or [""])[0].isdigit()]
    if caller:
        me = str(os.getpid())
        if not any(line.split(None, 1)[0] == me for line in found):
            message = f"{name} listed {len(found)} processes and not this one"
            raise ProcTableError(f"{message} ({os.getpid()})", message)
    elif not found:
        raise ProcTableError(f"{name} listed no live processes", f"{name} listed no live processes")
    return found


# Columns: each takes one word except lstart (five); `command`, `args` and `comm` take the rest of the line (Proc's
# `cmd`) and come last. pcpu and %cpu fill `cpu`.
INTEGERS = ("pid", "ppid", "pgid", "uid", "rss")
FLOATS = ("pcpu", "%cpu")
WORDS = ("tty", "stat")
REST = ("command", "args", "comm")
NO_TTY = ("?", "??", "-")


def columns(spec: str | Sequence[str]) -> tuple[str, ...]:
    """Column names from a ps -o spec ("pid=,ppid=,lstart=") or a sequence of names. Raises ValueError for a column
    this reader does not know, a rest-of-line column that is not last, or a spec without the pid first."""
    names = tuple(part.strip().rstrip("=") for part in (spec.split(",") if isinstance(spec, str) else spec))
    known = {*INTEGERS, *FLOATS, *WORDS, *REST, "lstart"}
    for at, name in enumerate(names):
        if name not in known:
            raise ValueError(f"unknown ps column {name!r}")
        if name in REST and at != len(names) - 1:
            raise ValueError(f"ps column {name!r} takes the rest of the line and must come last")
    if not names or names[0] != "pid":
        raise ValueError("a ps column set starts with the pid")
    return names


def fields(spec: str | Sequence[str]) -> str:
    """The ps -o argument for `spec`: every column with an empty header."""
    return ",".join(f"{name}=" for name in columns(spec))


def parse(text: str | Sequence[str], spec: str | Sequence[str], clock: Clock = "utc") -> dict[int, Proc]:
    """pid -> Proc for each row of `ps -o fields(spec)` output (text, or its lines) whose numbers read; a line that
    does not read is skipped. `start` is the lstart normalised; `epoch` the second it names in `clock` (None
    without an lstart column or when it does not parse). A column the spec lacks keeps Proc's default."""
    names = columns(spec)
    rest = names[-1] in REST
    before = sum(5 if name == "lstart" else 1 for name in (names[:-1] if rest else names))
    lines = text.splitlines() if isinstance(text, str) else list(text)
    found: dict[int, Proc] = {}
    for line in lines:
        parts = line.split(None, before) if rest else line.split()
        if len(parts) < before:
            continue
        number: dict[str, int] = {}
        cpu, tty, stat, start, epoch = 0.0, "", "", "", None
        at = 0
        usable = True
        for name in names[:-1] if rest else names:
            if name == "lstart":
                start = " ".join(parts[at:at + 5])
                epoch = parse_lstart(start, clock)
                at += 5
                continue
            word = parts[at]
            at += 1
            if name in INTEGERS:
                usable = word.isdigit()
                if not usable:
                    break
                number[name] = int(word)
            elif name in FLOATS:
                try:
                    cpu = float(word.replace(",", "."))
                except ValueError:
                    usable = False
                    break
            elif name == "tty":
                tty = "" if word in NO_TTY else word
            else:
                stat = word
        if not usable:
            continue
        cmd = parts[before].strip() if rest and len(parts) > before else ""
        proc = Proc(number["pid"], number.get("ppid", 0), number.get("pgid", 0), number.get("uid", -1), tty, start,
                    cmd, cpu, number.get("rss", 0), stat, epoch, clock)
        found[proc.pid] = proc
    return found
