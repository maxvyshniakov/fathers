"""One lifetime decision for registration and read-only readiness; execution slots are unrelated."""
from __future__ import annotations

import contextlib
from dataclasses import dataclass
import json
import math
import os
import selectors
import signal
import subprocess
import sys
import time
from typing import Any, cast

from . import config, host, slots, state
from .errors import LaneError

TIMEOUT = 5.0
MAX_OUTPUT = 65536


@dataclass(frozen=True)
class Decision:
    cap: int
    source: str
    machine: dict[str, Any]
    refusals: tuple[str, ...]


def transport(command: str, request: dict[str, Any]) -> bytes:
    """Bound input, both output streams and elapsed time while draining; own child group only."""
    payload = json.dumps(request, allow_nan=False).encode()
    if len(payload) > MAX_OUTPUT:
        raise LaneError("lifetime admission unavailable: request too large")
    process = subprocess.Popen([command, "lane-admission", "--json"], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    pipes = (process.stdin, process.stdout, process.stderr)
    failed = True
    try:
        output = bytearray()
        total = 0
        sent = 0
        deadline = time.monotonic() + TIMEOUT
        with selectors.DefaultSelector() as selector:
            for pipe, event, name in ((process.stdin, selectors.EVENT_WRITE, "stdin"),
                                      (process.stdout, selectors.EVENT_READ, "stdout"),
                                      (process.stderr, selectors.EVENT_READ, "stderr")):
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, event, name)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("query timed out")
                for key, _ in selector.select(min(remaining, 0.2)):
                    if key.data == "stdin":
                        try:
                            sent += os.write(key.fd, payload[sent:])
                        except BrokenPipeError:
                            sent = len(payload)
                        if sent == len(payload):
                            selector.unregister(key.fd)
                            process.stdin.close()
                    else:
                        chunk = os.read(key.fd, 16384)
                        if not chunk:
                            selector.unregister(key.fd)
                            continue
                        total += len(chunk)
                        if total > MAX_OUTPUT:
                            raise LaneError("lifetime admission unavailable: output exceeds 65536 bytes")
                        if key.data == "stdout":
                            output.extend(chunk)
        status = process.wait(timeout=max(0.001, deadline - time.monotonic()))
        if status:
            raise LaneError(f"lifetime admission unavailable: query exited {status}")
        failed = False
        return bytes(output)
    finally:
        if failed:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=1)
        for pipe in pipes:
            pipe.close()


def request_for(project: config.Config, occupied: int) -> dict[str, Any]:
    samples: dict[str, Any] = {}
    for name, field in (("CORES", "cores"), ("MEMORY_GB", "memory_gib"), ("LOAD15", "load15")):
        found = project.lookup(name)
        if not found or not found[1]:
            continue
        source, raw = found
        try:
            value = int(raw) if name == "CORES" else float(raw)
        except ValueError:
            if name == "LOAD15":
                raise LaneError("lifetime admission unavailable: invalid LOAD15 override") from None
            continue
        if name == "CORES" and (not raw.isdigit() or value < 1):
            continue
        if not math.isfinite(value) or value < 0:
            raise LaneError(f"lifetime admission unavailable: invalid {name} override")
        samples[field] = {"value": value, "source": source}
    cap = project.lookup("CAP")
    return {"v": 1, "kind": "lane-lifetime", "scope": str(state.Store(project).directory.resolve()),
            "occupied": occupied, "cap_override": {"value": cap[1], "source": cap[0]} if cap else None,
            "sample_overrides": samples}


def mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("expected object")
    return cast(dict[str, Any], value)


def finite(value: object, *, integer: bool = False, positive: bool = False) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("expected numeric fact")
    if not math.isfinite(value) or value < 0 or (positive and value <= 0) or (integer and type(value) is not int):
        raise ValueError("invalid numeric fact")
    return value


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for key, value in pairs:
        if key in found:
            raise ValueError("duplicate response field")
        found[key] = value
    return found


def response_for(raw: bytes, request: dict[str, Any]) -> dict[str, Any]:
    response = mapping(json.loads(raw, object_pairs_hook=unique_object))
    if (type(response.get("v")) is not int or response["v"] != 1
            or response.get("kind") != "lane-lifetime" or response.get("policy") != "lane-lifetime-v1"
            or response.get("available") is not True):
        raise ValueError("unsupported or unavailable response")
    for key in ("scope", "occupied", "cap_override"):
        if response.get(key) != request[key] or key not in response:
            raise ValueError(f"mismatched {key}")
    finite(response["occupied"], integer=True)
    finite(response["cap"], integer=True, positive=True)
    finite(response["derived_cap"], integer=True, positive=True)
    finite(response["load_limit"], positive=True)
    finite(response["sampled_at"])
    if (not isinstance(response["cap_source"], str) or not response["cap_source"].strip()
            or len(response["cap_source"]) > 200):
        raise ValueError("invalid cap provenance")
    machine = mapping(response["host"])
    for key in ("cores", "memory_gib", "load15"):
        fact = mapping(machine[key])
        finite(fact["value"], integer=key == "cores", positive=key == "cores")
        if not isinstance(fact["source"], str) or not fact["source"].strip() or len(fact["source"]) > 200:
            raise ValueError("invalid host provenance")
        if key in request["sample_overrides"] and any(
                fact[field] != request["sample_overrides"][key][field] for field in ("value", "source")):
            raise ValueError("mismatched sample override")
    expected: list[str] = []
    if request["occupied"] >= response["cap"]:
        expected.append("cap")
    if machine["load15"]["value"] > response["load_limit"]:
        expected.append("load15")
    if (response["refusals"] != expected or type(response["allowed"]) is not bool
            or response["allowed"] != (not expected)):
        raise ValueError("inconsistent admission decision")
    if response["warnings"] not in ([], ["invalid_cap_override"]):
        raise ValueError("invalid warnings")
    if response["cap_source"] == "derived":
        if response["cap"] != response["derived_cap"]:
            raise ValueError("inconsistent derived cap")
    elif not request["cap_override"] or response["cap_source"] != request["cap_override"]["source"]:
        raise ValueError("mismatched cap provenance")
    return response


def evaluate(project: config.Config, records: list[state.Record]) -> Decision:
    if project.admission_provider not in ("legacy", "slotfather"):
        raise LaneError("unknown lifetime admission provider")
    active = state.occupying(records)
    if project.admission_provider == "legacy":
        cap, source = host.lane_cap(project)
        machine = host.host(project)
        codes: list[str] = []
        if len(active) >= cap:
            codes.append("cap")
        if machine["load15"] > host.load_limit(machine["cores"]):
            codes.append("load15")
    else:
        command = slots.command()
        if command is None:
            raise LaneError("lifetime admission unavailable: slotfather is not on PATH")
        try:
            request = request_for(project, len(active))
            answer = response_for(transport(command, request), request)
        except (OSError, ValueError, KeyError, TypeError, OverflowError,
                subprocess.SubprocessError, RecursionError) as error:
            raise LaneError(f"lifetime admission unavailable: {error}") from None
        cap = int(answer["cap"])
        source = f"slotfather {answer['cap_source']}; derived: {answer['derived_cap']}"
        machine = {key: answer["host"][key]["value"] for key in ("cores", "load15")}
        machine["load_limit"] = answer["load_limit"]
        machine["disk_free_gb"] = host.disk_free_gb(project)
        codes = answer["refusals"]
        for warning in answer["warnings"]:
            print(f"lane: warning: {warning}; the cap is {cap}", file=sys.stderr)
    refusals: list[str] = []
    if "cap" in codes:
        refusals.append(f"{len(active)} lanes are planned or active ({', '.join(r['id'] for r in active)}); "
                        f"the cap is {cap} ({source}): land one to make room under the lane cap")
    if machine["disk_free_gb"] < host.MIN_DISK_GB:
        refusals.append(f"free disk is {machine['disk_free_gb']:.0f} GB, under {host.MIN_DISK_GB} GB: "
                        f"run {project.command} done on landed lanes")
    if "load15" in codes:
        limit = (f"{host.LOAD_FACTOR}x{machine['cores']} cores" if project.admission_provider == "legacy"
                 else f"the Slotfather admission limit {machine['load_limit']:.1f}")
        refusals.append(f"load15 is {machine['load15']:.1f}, above {limit}")
    return Decision(cap, source, machine, tuple(refusals))
