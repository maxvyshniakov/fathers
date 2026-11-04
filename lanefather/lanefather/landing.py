"""The landing lock (B2): land, promote and sync hold <git common dir>/landing.lock while they move refs, and give git
its LANDING_TOKEN. A project's reference-transaction hook lets an agent session move main or dev only with the token
in the lock, taken less than LANDING_TTL ago; the file format is that hook's contract:

  token <hex>
  holder <who>
  acquired <unix seconds>
  pid <pid>
  host <nodename>
"""
from __future__ import annotations

from collections.abc import Generator
import contextlib
import fcntl
import os
from pathlib import Path
import re
import secrets
import time

from . import journal
from .errors import LaneError

LANDING_TTL = 2 * 3600


def read_landing(path: Path) -> dict[str, str] | None:
    """The lock's fields, {} for a file the hook would not parse, None when there is none."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    except OSError:
        return {}
    fields = dict(line.split(" ", 1) for line in text.splitlines() if " " in line)
    return fields if fields.get("token") and re.fullmatch(r"[0-9]+", fields.get("acquired", "")) else {}


def landing_stale(fields: dict[str, str]) -> str | None:
    """Why a lock may be taken over: malformed, expired, or its holder process has exited."""
    if not fields:
        return "malformed"
    if time.time() - int(fields["acquired"]) > LANDING_TTL:
        return "expired"
    pid = fields.get("pid", "")
    if pid.isdigit() and fields.get("host") == os.uname().nodename:
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            return f"holder pid {pid} has exited"
        except PermissionError:
            pass
    return None


def landing_holder(fields: dict[str, str]) -> str:
    since = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(fields["acquired"])))
    return f"{fields.get('holder', 'an unnamed holder')} since {since}"


@contextlib.contextmanager
def landing(common: str, holder: str, wait: float) -> Generator[dict[str, str]]:
    """Hold the landing lock and yield an environment whose LANDING_TOKEN passes the hook."""
    path = Path(common) / "landing.lock"
    token = secrets.token_hex(16)
    deadline = time.monotonic() + wait
    while True:
        with open(path.with_name("landing.lock.guard"), "a") as guard:
            fcntl.flock(guard, fcntl.LOCK_EX)
            current = read_landing(path)
            stale = landing_stale(current) if current is not None else None
            if current is None or stale:
                pending = path.with_name(f"landing.lock.{os.getpid()}")
                pending.write_text(f"token {token}\nholder {holder}\nacquired {int(time.time())}\n"
                                   f"pid {os.getpid()}\nhost {os.uname().nodename}\n")
                pending.replace(path)
                if stale and current is not None:
                    journal.ledger(f"landing lock taken over from {current.get('holder', '?')} ({stale}) by {holder}")
                break
        if time.monotonic() >= deadline:
            raise LaneError(f"the landing lock ({path}) is held by {landing_holder(current)}; it expires "
                            f"{LANDING_TTL // 3600} h after it was taken. Wait for that landing to finish")
        time.sleep(0.5)
    try:
        yield dict(os.environ, LANDING_TOKEN=token)
    finally:
        with open(path.with_name("landing.lock.guard"), "a") as guard:
            fcntl.flock(guard, fcntl.LOCK_EX)
            if (read_landing(path) or {}).get("token") == token:
                path.unlink()
