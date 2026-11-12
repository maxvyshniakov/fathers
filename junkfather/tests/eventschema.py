"""A strict check of Godfather's event schema version 1, written from the draft in godfather's SPEC.md (Event schema,
version 1) until godfather ships its canonical validator, `eventcheck.py`.

TODO(godfather eventcheck): vendor godfather's eventcheck.py here with its SHA-256 beside it, validate with
`eventcheck.validate(obj, strict=True)`, and compare it with the canonical file when GODFATHER_DIR points at a
checkout (tests/test_events.py, test_the_vendored_validator_matches_godfathers). This file then goes.
"""
from __future__ import annotations

import re
from typing import Any, List

TOP = {"v", "time", "host", "source", "kind", "subject", "actor", "details", "level", "id"}
REQUIRED = TOP - {"level", "id"}
TIME = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
SOURCE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
KIND = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
HOST = re.compile(r"^[a-z0-9][a-z0-9-]*$")
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
ACTORS = {"agent", "process", "user", "tool"}
ACTOR_FIELDS = {"type", "session", "agent", "pid", "lane", "name", "via"}


def strings(value: Any, path: str, problems: List[str]) -> None:
    if isinstance(value, str):
        if CONTROL.search(value):
            problems.append(f"{path}: control character")
    elif isinstance(value, dict):
        for key, item in value.items():
            strings(key, f"{path}.key", problems)
            strings(item, f"{path}.{key}", problems)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            strings(item, f"{path}[{index}]", problems)


def depth(value: Any) -> int:
    if isinstance(value, dict):
        return 1 + max((depth(v) for v in value.values()), default=0)
    if isinstance(value, list):
        return 1 + max((depth(v) for v in value), default=0)
    return 0


def validate(event: Any, line: str = "", strict: bool = True) -> List[str]:
    problems: List[str] = []
    if not isinstance(event, dict):
        return ["not an object"]
    if line and len(line.encode("utf-8")) > 16 << 10:
        problems.append("longer than 16 KiB")
    missing = REQUIRED - set(event)
    if missing:
        problems.append(f"missing {sorted(missing)}")
    if strict and set(event) - TOP:
        problems.append(f"unknown fields {sorted(set(event) - TOP)}")
    if event.get("v") != 1:
        problems.append("v is not 1")
    if not (isinstance(event.get("time"), str) and TIME.match(event["time"])):
        problems.append(f"time {event.get('time')!r}")
    if not (isinstance(event.get("host"), str) and HOST.match(event["host"])):
        problems.append(f"host {event.get('host')!r}")
    if not (isinstance(event.get("source"), str) and SOURCE.match(event["source"])):
        problems.append("source")
    kind = event.get("kind")
    if not (isinstance(kind, str) and len(kind) <= 64 and KIND.match(kind)):
        problems.append(f"kind {kind!r}")
    subject = event.get("subject")
    if not (isinstance(subject, dict) and isinstance(subject.get("type"), str) and isinstance(subject.get("id"), str)):
        problems.append("subject needs string type and id")
    elif any(isinstance(v, (dict, list)) for v in subject.values()):
        problems.append("subject holds scalars only")
    actor = event.get("actor")
    if not (isinstance(actor, dict) and actor.get("type") in ACTORS):
        problems.append(f"actor type {actor.get('type') if isinstance(actor, dict) else actor!r}")
    elif strict and set(actor) - ACTOR_FIELDS:
        problems.append(f"actor fields {sorted(set(actor) - ACTOR_FIELDS)}")
    details = event.get("details")
    if not isinstance(details, dict):
        problems.append("details is not an object")
    elif depth(details) > 4:
        problems.append("details nest deeper than four levels")
    if "level" in event and event["level"] not in ("info", "warn", "error"):
        problems.append("level")
    if "id" in event and not (isinstance(event["id"], str) and len(event["id"]) <= 64):
        problems.append("id")
    strings(event, "event", problems)
    return problems
