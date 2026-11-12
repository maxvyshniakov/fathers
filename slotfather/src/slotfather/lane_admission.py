"""Stateless lifetime policy; never reads or mutates execution queue state."""
from __future__ import annotations

import json
import math
import sys
import time
from typing import cast

from . import platform


class AdmissionError(ValueError):
    def __init__(self, message: str, code: str = "invalid_request") -> None:
        super().__init__(message)
        self.code = code


def mapping(value: object, fields: set[str], required: set[str]) -> dict[str, object]:
    if not isinstance(value, dict):
        raise AdmissionError("expected a JSON object")
    found = cast(dict[str, object], value)
    if set(found) - fields or required - set(found):
        raise AdmissionError("unexpected or missing fields")
    return found


def text(value: object, maximum: int = 1024) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise AdmissionError("expected bounded nonempty text")
    return value


def number(value: object, integer: bool = False) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AdmissionError("expected a finite nonnegative number")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite or value < 0 or (integer and not isinstance(value, int)):
        raise AdmissionError("expected a finite nonnegative number of the required type")
    return value


def validate(value: object) -> dict[str, object]:
    found = mapping(value, {"v", "kind", "scope", "occupied", "cap_override", "sample_overrides"},
                    {"v", "kind", "scope", "occupied"})
    if type(found["v"]) is not int or found["v"] != 1:
        raise AdmissionError("unsupported lane admission request version", "unsupported_version")
    if found["kind"] != "lane-lifetime":
        raise AdmissionError("expected lane-lifetime kind")
    text(found["scope"])
    number(found["occupied"], integer=True)
    cap = found.setdefault("cap_override", None)
    if cap is not None:
        override = mapping(cap, {"value", "source"}, {"value", "source"})
        if not isinstance(override["value"], str) or len(override["value"]) > 256:
            raise AdmissionError("cap override value must be bounded raw text")
        text(override["source"], 200)
    samples = mapping(found.setdefault("sample_overrides", {}), {"cores", "memory_gib", "load15"}, set())
    for key, sample in samples.items():
        fact = mapping(sample, {"value", "source"}, {"value", "source"})
        measured = number(fact["value"], integer=key == "cores")
        if key == "cores" and not 1 <= measured <= 2147483647:
            raise AdmissionError("cores must be an integer from 1 through 2147483647")
        text(fact["source"], 200)
    return found


def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    found: dict[str, object] = {}
    for key, value in pairs:
        if key in found:
            raise AdmissionError("duplicate JSON field")
        found[key] = value
    return found


def advise(request: dict[str, object]) -> dict[str, object]:
    request = validate(request)
    overrides = cast(dict[str, dict[str, object]], request["sample_overrides"])
    host: dict[str, dict[str, object]] = dict(overrides)
    if "cores" not in host:
        sampled_cores = platform.physical_cores()
        host["cores"] = {"value": sampled_cores or 1, "source": "physical" if sampled_cores else "unknown_fallback"}
    if "memory_gib" not in host:
        sampled_memory = platform.physical_memory_gib()
        host["memory_gib"] = {"value": sampled_memory if sampled_memory is not None else 0.0,
                              "source": "physical" if sampled_memory is not None else "unknown_fallback"}
    if "load15" not in host:
        host["load15"] = {"value": platform.physical_load15(), "source": "physical"}
    cores = cast(int, host["cores"]["value"])
    memory = float(cast(float, host["memory_gib"]["value"]))
    load = float(cast(float, host["load15"]["value"]))
    cap = max(1, min(cores // 2, int((memory - 8) // 4)))
    derived = cap
    cap_source = "derived"
    warnings: list[str] = []
    cap_override = cast(dict[str, object] | None, request["cap_override"])
    if cap_override is not None:
        raw = cast(str, cap_override["value"])
        try:
            parsed = int(raw) if raw.strip().isdigit() else 0
        except ValueError:
            parsed = 0
        if parsed > 0:
            cap = parsed
            cap_source = cast(str, cap_override["source"])
        elif raw:
            warnings.append("invalid_cap_override")
    occupied = cast(int, request["occupied"])
    refusals: list[str] = []
    if occupied >= cap:
        refusals.append("cap")
    if load > 1.5 * cores:
        refusals.append("load15")
    return {"v": 1, "kind": "lane-lifetime", "policy": "lane-lifetime-v1", "scope": request["scope"],
            "occupied": occupied, "available": True, "allowed": not refusals, "refusals": refusals, "cap": cap,
            "load_limit": 1.5 * cores, "sampled_at": time.time(), "derived_cap": derived,
            "cap_source": cap_source, "warnings": warnings, "cap_override": cap_override, "host": host}


def command(argv: list[str]) -> int:
    if argv != ["--json"]:
        print("slotfather: lane-admission takes --json and one stdin request", file=sys.stderr)
        return 2
    try:
        raw = sys.stdin.read(65537)
        if len(raw) > 65536:
            raise AdmissionError("request exceeds 65536 characters")
        request = validate(cast(object, json.loads(raw, object_pairs_hook=unique_object)))
        response = advise(request)
    except (AdmissionError, ValueError, OverflowError, RecursionError) as error:
        response = {"v": 1, "kind": "lane-lifetime", "available": False,
                    "error": {"code": error.code if isinstance(error, AdmissionError) else "invalid_request",
                              "message": str(error) if isinstance(error, AdmissionError) else "invalid JSON request"}}
        print(json.dumps(response, allow_nan=False, sort_keys=True))
        return 75
    except (OSError, AttributeError):
        print(json.dumps({"v": 1, "kind": "lane-lifetime", "available": False,
                          "error": {"code": "host_unavailable", "message": "host facts unavailable"}}, sort_keys=True))
        return 75
    print(json.dumps(response, allow_nan=False, sort_keys=True))
    return 0
