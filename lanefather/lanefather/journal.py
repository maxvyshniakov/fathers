"""The ledger: one line per action and override in <state>/ledger.log, which orchestration metrics read; and the
run logs beside it."""
from __future__ import annotations

import datetime
from pathlib import Path
import time

from . import state


def now() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(text: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))


def ledger(message: str) -> None:
    with open(state.state_dir() / "ledger.log", "a", encoding="utf-8") as handle:
        handle.write(f"{now()} {message}\n")


def logs() -> Path:
    """The run logs directory beside the records."""
    path = state.state_dir() / "logs"
    path.mkdir(exist_ok=True)
    return path


def log_path(label: str) -> Path:
    return logs() / f"{label}-{time.strftime('%Y%m%dT%H%M%S')}.log"
