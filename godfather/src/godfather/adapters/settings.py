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


def validate_reply(tool: str, reply: dict[str, Any]) -> dict[str, Any]:
    """Checks the required fields of a v1 `settings --json` document so malformed rows fail visibly."""
    if reply.get("v") not in SETTINGS_VERSIONS:
        raise SettingsError(f"{tool} settings version {reply.get('v')!r}; needs a newer godfather")
    sections: Any = reply.get("sections")
    if not isinstance(sections, list):
        raise SettingsError(f"{tool} settings: sections is missing; update {tool}")
    status: Any = reply.get("status")
    if status is not None and (not isinstance(status, list) or any(
            not isinstance(item, dict) or not isinstance(cast(dict[str, Any], item).get("label"), str)
            or not isinstance(cast(dict[str, Any], item).get("value"), str) for item in cast(list[Any], status))):
        raise SettingsError(f"{tool} settings: malformed status; update {tool}")
    for part in cast(list[Any], sections):
        if not isinstance(part, dict) or not isinstance(cast(dict[str, Any], part).get("settings"), list):
            raise SettingsError(f"{tool} settings: malformed section; update {tool}")
        section = cast(dict[str, Any], part)
        for raw_item in cast(list[Any], section["settings"]):
            if not isinstance(raw_item, dict):
                raise SettingsError(f"{tool} settings: a setting has no key; update {tool}")
            item = cast(dict[str, Any], raw_item)
            raw_key: Any = item.get("key")
            if not isinstance(raw_key, str) or not raw_key.strip():
                raise SettingsError(f"{tool} settings: a setting has no key; update {tool}")
            key: str = raw_key
            kind: Any = item.get("type")
            if not isinstance(kind, str) or not kind.strip():
                raise SettingsError(f"{tool} settings: {key} has no type; update {tool}")
            if "value" not in item:
                raise SettingsError(f"{tool} settings: {key} has no value; update {tool}")
            if item.get("label") is not None and not isinstance(item.get("label"), str):
                raise SettingsError(f"{tool} settings: {key} has a malformed label; update {tool}")
            if kind == "choice":
                choices: Any = item.get("choices")
                if not isinstance(choices, list) or not choices or any(
                        not isinstance(choice, dict) or "value" not in cast(dict[str, Any], choice)
                        for choice in cast(list[Any], choices)):
                    raise SettingsError(f"{tool} settings: {key} has malformed choices; update {tool}")
            elif kind in ("integer", "number", "duration") and item["value"] is not None:
                if not isinstance(item["value"], (int, float)) or isinstance(item["value"], bool):
                    raise SettingsError(f"{tool} settings: {key} has a non-numeric value; update {tool}")
            elif kind == "boolean" and item["value"] is not None and not isinstance(item["value"], bool):
                raise SettingsError(f"{tool} settings: {key} has a non-boolean value; update {tool}")
    return reply


class SettingsClient:
    """`<tool> settings ...` through sources.run_json(), with `--json` last."""

    TIMEOUT = TIMEOUT

    def __init__(self, executable: Optional[str] = None, tool: str = "slotfather",
                 runner: Optional[Any] = None) -> None:
        self.executable, self.tool, self.runner = executable, tool, runner

    def call(self, argv: list[str]) -> tuple[int, Any]:
        from .. import sources  # here, so the adapter stays importable without the I/O module's state

        timeout = sources.SETTINGS_TIMEOUT if self.TIMEOUT == TIMEOUT else self.TIMEOUT
        active_runner = self.runner if self.runner is not None else sources.default_runner()
        code, value, problem = active_runner.run_json(self.tool, ["settings", *argv, "--json"], timeout,
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
        return validate_reply(self.tool, cast(dict[str, Any], value))

    def check(self, key: str, value: str) -> dict[str, Any]:
        """{"ok", "error", "preview", "slots"?}; a refusal (exit 2, 4 or 5) is an answer, not an error."""
        code, reply = self.call(["check", key, value])
        if not isinstance(reply, dict):
            return {"ok": False, "error": f"{self.tool} settings check exited {code}", "preview": None}
        return cast(dict[str, Any], reply)

    def changed(self, code: int, value: Any) -> dict[str, Any]:
        if code == CONFLICT:
            found = cast(dict[str, Any], value) if isinstance(value, dict) else {}
            raise Conflict(validate_reply(self.tool, found) if found.get("sections") else None,
                           self.error(value, "changed elsewhere"))
        if code in READ_ONLY:
            raise ReadOnly(self.error(value, f"{self.tool} settings are read-only here"))
        if code != DONE or not isinstance(value, dict):
            raise SettingsError(self.error(value, "not saved"))
        return validate_reply(self.tool, cast(dict[str, Any], value))

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
