"""Any tool's `settings` CLI (SPEC.md, Settings contract): read, check, set and reset, each a short call with the
tool's exit codes. 0 done, 2 invalid, 3 conflict, and read-only as 4 (Slotfather: another user's queue; Lanefather)
or 5 (Junkfather, which keeps 4 for Slotfather's meaning, fathers#118). Any other code is a failure with the tool's
message."""
from __future__ import annotations

from typing import Any, Optional, cast

SETTINGS_VERSIONS = (1,)  # `settings --json` versions, and capabilities' `dashboard.settings.versions`
DONE, INVALID, CONFLICT = 0, 2, 3
READ_ONLY = (4, 5)
TIMEOUT = 5.0


class SettingsError(Exception):
    """A settings call that failed or was refused: the message is the tool's own, shown in red."""


class Conflict(SettingsError):
    """`settings set` with a revision that no longer matches (exit 3); `reply` is the current settings."""

    def __init__(self, reply: Optional[dict[str, Any]], message: str = "changed elsewhere") -> None:
        super().__init__(message)
        self.reply = reply


class ReadOnly(SettingsError):
    """A refusal because the settings cannot be changed here (exit 4 or 5)."""


def supported(capabilities: Optional[dict[str, Any]]) -> bool:
    """Whether a tool's capabilities advertise a settings contract this Godfather reads."""
    block: Any = (capabilities or {}).get("dashboard")
    found: Any = cast(dict[str, Any], block).get("settings") if isinstance(block, dict) else None
    versions: Any = cast(dict[str, Any], found).get("versions") if isinstance(found, dict) else None
    return isinstance(versions, list) and any(v in SETTINGS_VERSIONS for v in cast(list[Any], versions))


class Contract:
    """A tool read only for its settings (sources.Source's adapter for Settings' own sources, fathers#87 review):
    any dashboard block; whether it has a settings contract is supported()'s to say."""

    def __init__(self, name: str) -> None:
        self.name, self.TAG = name, name[:4]

    def unsupported(self, capabilities: dict[str, Any]) -> Optional[str]:
        if capabilities.get("v") != 1 or not isinstance(capabilities.get("dashboard"), dict):
            return f"no dashboard contract; update {self.name}"
        return None


class SettingsClient:
    """`<tool> settings ...` through sources.run_json(), with `--json` last."""

    TIMEOUT = TIMEOUT

    def __init__(self, executable: Optional[str] = None, tool: str = "slotfather") -> None:
        self.executable, self.tool = executable, tool

    def call(self, argv: list[str]) -> tuple[int, Any]:
        from .. import sources  # here, so the adapter stays importable without the I/O module's state

        code, value, problem = sources.run_json(self.tool, ["settings", *argv, "--json"], self.TIMEOUT,
                                                self.executable)
        if value is None and code in (DONE, CONFLICT):
            raise SettingsError(problem or f"{self.tool} settings {' '.join(argv)} printed no JSON")
        if code not in (DONE, INVALID, CONFLICT, *READ_ONLY):
            raise SettingsError(self.error(value, problem or f"{self.tool} settings exited {code}"))
        return code, value

    def read(self) -> dict[str, Any]:
        code, value = self.call([])
        if code != DONE or not isinstance(value, dict):
            raise SettingsError(self.error(value, f"{self.tool} settings cannot be read"))
        reply = cast(dict[str, Any], value)
        if reply.get("v") not in SETTINGS_VERSIONS:
            raise SettingsError(f"{self.tool} settings version {reply.get('v')!r}; needs a newer godfather")
        return reply

    def check(self, key: str, value: str) -> dict[str, Any]:
        """{"ok", "error", "preview", "slots"?}; a refusal (exit 2, 4 or 5) is an answer, not an error."""
        code, reply = self.call(["check", key, value])
        if not isinstance(reply, dict):
            return {"ok": False, "error": f"{self.tool} settings check exited {code}", "preview": None}
        return cast(dict[str, Any], reply)

    def changed(self, code: int, value: Any) -> dict[str, Any]:
        if code == CONFLICT:
            found = cast(dict[str, Any], value) if isinstance(value, dict) else {}
            raise Conflict(found if found.get("sections") else None, self.error(value, "changed elsewhere"))
        if code in READ_ONLY:
            raise ReadOnly(self.error(value, f"{self.tool} settings are read-only here"))
        if code != DONE or not isinstance(value, dict):
            raise SettingsError(self.error(value, "not saved"))
        return cast(dict[str, Any], value)

    def set(self, key: str, value: str, revision: Optional[str] = None) -> dict[str, Any]:
        """Saves one value; the settings as they are now. Raises Conflict, ReadOnly or SettingsError."""
        return self.changed(*self.call(["set", key, value, *(["--revision", revision] if revision else [])]))

    def reset(self, key: Optional[str], revision: Optional[str] = None) -> dict[str, Any]:
        """Resets one key, or every setting of the tool (None)."""
        target = [key] if key else ["--all"]
        return self.changed(*self.call(["reset", *target, *(["--revision", revision] if revision else [])]))

    @staticmethod
    def error(value: Any, fallback: str) -> str:
        found: Any = cast(dict[str, Any], value).get("error") if isinstance(value, dict) else None
        return str(found) if found else fallback
