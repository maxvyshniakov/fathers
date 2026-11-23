"""The live sweep's stops, through Spawnfather (fathers#171): Spawnfather is the one tool that stops processes.

Slotfather still decides what goes: the classes, the ages and the leases it owns (sweep_plan). Each process tree a
live sweep would stop is handed to

  spawnfather reap --pid PID --started EPOCH --cmd CMD --tree --reason TEXT --grace S --via sweep --json

which checks the identity again (the start second and the command, as Slotfather read them) and Spawnfather's own
rules before it signals anything. A Spawnfather that refuses (an entry of its own holds the process, say) leaves it
running: the item is spared with Spawnfather's reason.

For one release a live sweep without a Spawnfather that offers this (`capabilities --json`: `lifetimes.reap` lists
`identity`) still stops the trees itself (sweep_os.stop) and says so: a warning on stderr, a note in the report and
in sweep.jsonl (DEPRECATED). The release after drops that fallback. $SLOTFATHER_SPAWNFATHER names the command
(default `spawnfather` on PATH); one that cannot run counts as none.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any, Callable, cast

from .sweep_plan import Item, Snapshot

ENV = "SLOTFATHER_SPAWNFATHER"
TIMEOUT = 30.0  # seconds a spawnfather call may take beyond the grace period
DEPRECATED = ("Spawnfather is not installed or too old (needs `spawnfather reap --pid --started`): this live sweep "
              "stopped processes itself, which the next release no longer does; install it with "
              "`python3 install.py spawnfather`")
Run = Callable[..., "subprocess.CompletedProcess[str]"]


def command() -> str | None:
    """The spawnfather command: $SLOTFATHER_SPAWNFATHER, else `spawnfather` on PATH; None without one."""
    return os.environ.get(ENV) or shutil.which("spawnfather")


class Spawnfather:
    """The installed spawnfather, as far as the live sweep needs it."""

    def __init__(self, program: str, run: Run = subprocess.run) -> None:
        self.program = program
        self.run = run

    def call(self, args: list[str], timeout: float) -> tuple[int, str, str]:
        try:
            done = self.run([self.program, *args], capture_output=True, text=True, timeout=timeout,
                            stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired) as error:
            return -1, "", str(error)
        return done.returncode, done.stdout, done.stderr

    def supported(self) -> bool:
        """Whether it offers `reap --pid --started --cmd` (capabilities: `lifetimes.reap` lists `identity`)."""
        code, out, _ = self.call(["capabilities", "--json"], TIMEOUT)
        if code != 0:
            return False
        try:
            data: object = json.loads(out)
        except ValueError:
            return False
        lifetimes: object = cast(dict[str, Any], data).get("lifetimes") if isinstance(data, dict) else None
        reaps: object = cast(dict[str, Any], lifetimes).get("reap") if isinstance(lifetimes, dict) else None
        return isinstance(reaps, list) and "identity" in cast(list[Any], reaps)

    def stop(self, item: Item, snap: Snapshot, grace: float) -> dict[str, Any]:
        """Stop the item's tree through `spawnfather reap --pid --started --cmd --tree`. Returns sweep_os.stop's
        shape, {"result": stopped|killed|gone|failed|spared, "signalled", "left"}, with Spawnfather's `reason` when
        it refused (spared) and an `error` when it failed."""
        pid = int(item["pid"])
        proc = snap.procs.get(pid)
        if proc is None or proc.started is None:
            return {"result": "failed", "signalled": [], "left": list(item["tree"]),
                    "error": "no start time to name the process by"}
        args = ["reap", "--pid", str(pid), "--started", str(round(proc.started)), f"--cmd={proc.cmd}", "--tree",
                "--reason", f"slotfather sweep: {item['class']} ({item['kind']})", "--grace", f"{grace:g}",
                "--via", "sweep", "--json"]
        code, out, err = self.call(args, grace + TIMEOUT)
        why = (err.strip().splitlines() or [""])[-1].removeprefix("spawnfather: ")
        if code == 3 and why.startswith("refusing pid"):  # its rules keep the process (an entry of its own holds it)
            return {"result": "spared", "signalled": [], "left": [], "reason": f"Spawnfather refused: {why}"}
        try:
            data: object = json.loads(out)
            rows: object = cast(dict[str, Any], data).get("results") if isinstance(data, dict) else None
        except ValueError:
            rows = None
        first: object = cast(list[Any], rows)[0] if isinstance(rows, list) and rows else None
        row: dict[str, Any] = cast(dict[str, Any], first) if isinstance(first, dict) else {}
        result = row.get("result")
        pids = [p for p in cast(list[Any], row.get("pids") or []) if isinstance(p, int)]
        if code == 0 and result in ("stopped", "killed", "gone"):
            return {"result": result, "signalled": pids, "left": []}
        return {"result": "failed", "signalled": pids, "left": pids,
                "error": why or f"spawnfather reap exited {code}"}


def find(run: Run = subprocess.run) -> Spawnfather | None:
    """The installed Spawnfather that offers identity reaps, or None (then the deprecated fallback applies)."""
    program = command()
    if not program:
        return None
    found = Spawnfather(program, run)
    return found if found.supported() else None
