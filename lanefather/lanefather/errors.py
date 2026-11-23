"""The one error a command refuses with: its message names the rule and what to do."""
from __future__ import annotations


class LaneError(Exception):
    code = 1


class LaneRefused(LaneError):
    """A refusal by the current state that no override lifts (exit 3): Spawnfather, which removes a lane's things,
    is missing or too old (fathers#171)."""
    code = 3
