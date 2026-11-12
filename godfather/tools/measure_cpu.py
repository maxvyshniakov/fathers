#!/usr/bin/env python3
"""CPU of Godfather (with its snapshot calls) against today's `slotfather dashboard`, side by side on one queue.

    python3 tools/measure_cpu.py [--seconds 600] [--godfather CMD] [--slotfather CMD]

Both run at the same time in their own pseudo-terminal at 156×43 and refresh every second; after `--seconds` each
gets `q`, and the CPU time (user + system) of it and of every process it ran and waited for (the snapshot calls)
is read from getrusage(RUSAGE_CHILDREN) of a wrapper around it. Prints one JSON line and a summary.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import pty
import select
import shlex
import struct
import subprocess
import sys
import termios
import time

WRAPPER = ("import resource, subprocess, sys, json; subprocess.run(sys.argv[1:]); "
           "u = resource.getrusage(resource.RUSAGE_CHILDREN); "
           "sys.stderr.write(json.dumps({'user': u.ru_utime, 'system': u.ru_stime}) + '\\n')")


def start(argv: list[str], report: str) -> tuple[subprocess.Popen[bytes], int, int]:
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 43, 156, 0, 0))
    handle = os.open(report, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    process = subprocess.Popen([sys.executable, "-c", WRAPPER, *argv], stdin=slave, stdout=slave, stderr=handle,
                               start_new_session=True)
    os.close(slave)
    return process, master, handle


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=600)
    parser.add_argument("--godfather", default="godfather")
    parser.add_argument("--slotfather", default="slotfather dashboard")
    args = parser.parse_args()
    runs = {}
    reports = {}
    for name, command in (("slotfather", args.slotfather), ("godfather", args.godfather)):
        reports[name] = f"/tmp/measure-cpu-{os.getpid()}-{name}.json"
        runs[name] = start(shlex.split(command), reports[name])
    deadline = time.monotonic() + args.seconds
    masters = [master for _, master, _ in runs.values()]
    while time.monotonic() < deadline:  # keep reading both screens, as a terminal would
        for ready in select.select(masters, [], [], 0.5)[0]:
            try:
                os.read(ready, 65536)
            except OSError:
                masters.remove(ready)
    for _, master, _ in runs.values():
        os.write(master, b"q")
    found = {}
    for name, (process, master, handle) in runs.items():
        end = time.monotonic() + 10
        while process.poll() is None and time.monotonic() < end:
            if select.select([master], [], [], 0.2)[0]:
                try:
                    os.read(master, 65536)
                except OSError:
                    break
        process.wait(timeout=5)
        os.close(handle)
        with open(reports[name]) as report:
            lines = [line for line in report.read().splitlines() if line.startswith("{")]
        os.unlink(reports[name])
        usage = json.loads(lines[-1])
        found[name] = round(usage["user"] + usage["system"], 2)
    ratio = found["godfather"] / found["slotfather"] if found["slotfather"] else float("inf")
    print(json.dumps({"seconds": args.seconds, "cpu_seconds": found, "ratio": round(ratio, 2)}))
    print(f"{args.seconds:.0f} s side by side: slotfather dashboard {found['slotfather']} s CPU, godfather "
          f"{found['godfather']} s CPU (with its snapshot calls), {ratio:.2f}×")
    return 0


if __name__ == "__main__":
    sys.exit(main())
