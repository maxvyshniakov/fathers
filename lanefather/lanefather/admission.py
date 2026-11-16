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
from .gitops import common_dir, git_ok, main_checkout

TIMEOUT = 5.0
MAX_OUTPUT = 65536


@dataclass(frozen=True)
class Decision:
    cap: int
    source: str
    machine: dict[str, Any]
    refusals: tuple[str, ...]
    # The planned and active lanes of the repository the cap counts (fathers#149), and of every project together
    # with the global ceiling and its variable, when one is set.
    occupying: tuple[state.Record, ...] = ()
    everywhere: tuple[state.Record, ...] = ()
    ceiling: tuple[int, str] | None = None


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
    cap = host.cap_override(project)
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
    override = request["cap_override"]
    raw_cap = override["value"] if override else ""
    try:
        explicit_cap = int(raw_cap) if raw_cap.strip().isdigit() else 0
    except ValueError:
        explicit_cap = 0
    expected_cap = explicit_cap if explicit_cap > 0 else response["derived_cap"]
    expected_source = override["source"] if explicit_cap > 0 else "derived"
    expected_warnings = ["invalid_cap_override"] if raw_cap and explicit_cap <= 0 else []
    if (response["cap"] != expected_cap or response["cap_source"] != expected_source
            or response["warnings"] != expected_warnings):
        raise ValueError("cap override was not applied consistently")
    return response


def repository_of(path: str | os.PathLike[str]) -> str | None:
    """The Git common directory of the repository `path` is in; None outside one, or when Git cannot be run."""
    try:
        return common_dir(path) if git_ok(path, "rev-parse", "--git-dir") else None
    except OSError:
        return None


def running_common(project: config.Config) -> str | None:
    """The repository whose lanes a read of the cap counts (review of fathers#149): the one the command runs in, as
    start registers there; outside any repository, the one the configuration's file is in; else None (every lane
    counts)."""
    return repository_of(os.getcwd()) or (repository_of(project.path.parent) if project.path else None)


def grouped(records: list[state.Record]) -> str:
    """The lanes by repository: `data-grid: a, b; sid-kit: c`. Two repositories of one name show their paths."""
    groups: dict[str | None, list[str]] = {}
    for record in records:
        groups.setdefault(record.get("common_dir"), []).append(record["id"])
    names = {common: state.project_name(common) for common in groups}
    shared = {name for name in names.values() if list(names.values()).count(name) > 1}

    def label(common: str | None) -> str:
        name = names[common]
        return f"{name} ({main_checkout(common)})" if common and name in shared else name

    return "; ".join(f"{label(common)}: {', '.join(groups[common])}"
                     for common in sorted(groups, key=lambda c: (names[c], c or "")))


def evaluate(project: config.Config, records: list[state.Record], common: str | None = None, *,
             cap_exempt: bool = False, repaired: str | None = None) -> Decision:
    """Whether one more lane of the repository whose Git common directory is `common` may be registered: the cap
    counts that repository's planned and active lanes (all of `records` when `common` is None), the global ceiling
    every project's. `cap_exempt` admits a lane outside both (a fix lane in its landed parent's place, fathers#45);
    disk and load still apply. `repaired` says what start's disk repair did (fathers#157), for a disk refusal."""
    if project.admission_provider not in ("legacy", "slotfather"):
        raise LaneError("unknown lifetime admission provider")
    active = state.occupying(state.of_project(records, common))
    everywhere = state.occupying(records)
    ceiling = host.global_cap(project)
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
    if "cap" in codes and not cap_exempt:
        where = f" in {state.project_name(common)}" if common else ""
        refusals.append(f"{len(active)} lanes are planned or active{where} ({', '.join(r['id'] for r in active)}); "
                        f"the cap is {cap} ({source}): land one to make room under the lane cap")
    if ceiling and len(everywhere) >= ceiling[0] and not cap_exempt:
        refusals.append(f"{len(everywhere)} lanes are planned or active across projects ({grouped(everywhere)}); "
                        f"the global ceiling is {ceiling[0]} ({ceiling[1]}): land one to make room")
    if machine["disk_free_gb"] < host.MIN_DISK_GB:
        refusals.append(f"free disk is {machine['disk_free_gb']:.0f} GB, under {host.MIN_DISK_GB} GB: "
                        + (f"{repaired}; the rest is not finished lanes' to free" if repaired else
                           f"run {project.command} done --finished, or done on landed lanes"))
    if "load15" in codes:
        limit = (f"{host.LOAD_FACTOR}x{machine['cores']} cores" if project.admission_provider == "legacy"
                 else f"the Slotfather admission limit {machine['load_limit']:.1f}")
        refusals.append(f"load15 is {machine['load15']:.1f}, above {limit}")
    return Decision(cap, source, machine, tuple(refusals), tuple(active), tuple(everywhere), ceiling)
