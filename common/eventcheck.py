# MIT License
#
# Copyright (c) 2026 Max Wish
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the "Software"), to deal in the Software without restriction, including without limitation the
# rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all copies or substantial portions of
# the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
# THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT,
# TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
"""Validator for the shared tool event schema, version 1 (Godfather, docs/events.md).

One file, standard library only, Python 3.9+. It lives in common/ of the fathers repository, and every tool's tests
load it from there; another project may copy it unchanged and record its SHA-256 beside the copy.

    validate(obj, strict=False) -> list[str]        problems with one decoded event, empty when valid
    validate_line(line, strict=False) -> list[str]  the same for one log line (bytes or str), JSON and size included
    validate_file(path, strict=False) -> list[str]  every line of a log, each problem prefixed with "LINE: "

    python3 eventcheck.py [--strict] FILE...        prints FILE:LINE: problem, exits 1 on any problem

Lenient mode (the default) is what a reader accepts: unknown fields are ignored. Strict mode, for the tools' own
tests, also refuses unknown top-level fields. Every message starts with the field it is about.
"""
from __future__ import annotations

import datetime
import json
import re
import sys
from typing import Any, Dict, List, Optional, Tuple, Union, cast

SCHEMA_VERSION = 1
MAX_LINE_BYTES = 16 * 1024  # with its newline
MAX_DETAILS_DEPTH = 4  # details itself is level 1

REQUIRED = ("v", "time", "host", "source", "kind", "subject", "actor", "details")
OPTIONAL = ("level", "id")
LEVELS = ("info", "warn", "error")
ACTOR_TYPES = ("agent", "process", "user", "tool")
ACTOR_STRINGS = ("session", "agent", "lane", "name", "via")
SUBJECT_STRINGS = ("label", "project")

_TIME = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z\Z")
_HOST = re.compile(r"\A[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_SOURCE = re.compile(r"\A[a-z][a-z0-9-]{0,31}\Z")
# noun.verb; a tool may document a single-word kind (Lanefather's "override"), so the dot is not forced.
_KIND = re.compile(r"\A[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*\Z")
_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f]")

Problems = List[str]


def _mapping(value: Any) -> Dict[Any, Any]:
    return cast(Dict[Any, Any], value)


def _sequence(value: Any) -> List[Any]:
    return cast(List[Any], value)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _control(path: str, value: Any, problems: Problems) -> None:
    """Every string, keys included, is free of control characters, so nothing reaches a terminal as an escape."""
    if isinstance(value, str):
        if _CONTROL.search(value):
            problems.append(f"{path}: contains a control character")
    elif isinstance(value, dict):
        for key, item in _mapping(value).items():
            if isinstance(key, str) and _CONTROL.search(key):
                problems.append(f"{path}: key {key!r} contains a control character")
            _control(f"{path}.{key}", item, problems)
    elif isinstance(value, list):
        for index, item in enumerate(_sequence(value)):
            _control(f"{path}[{index}]", item, problems)


def _depth(value: Any) -> int:
    if isinstance(value, dict):
        return 1 + max((_depth(item) for item in _mapping(value).values()), default=0)
    if isinstance(value, list):
        return 1 + max((_depth(item) for item in _sequence(value)), default=0)
    return 0


def _finite(path: str, value: Any, problems: Problems) -> None:
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        problems.append(f"{path}: must be a finite number")
    elif isinstance(value, dict):
        for key, item in _mapping(value).items():
            _finite(f"{path}.{key}", item, problems)
    elif isinstance(value, list):
        for index, item in enumerate(_sequence(value)):
            _finite(f"{path}[{index}]", item, problems)


def _check_time(value: Any, problems: Problems) -> None:
    if not isinstance(value, str) or not _TIME.match(value):
        problems.append("time: must be RFC 3339 UTC with milliseconds, like 2026-11-12T02:24:16.204Z")
        return
    try:
        datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError:
        problems.append(f"time: {value!r} is not a real date and time")


def _check_subject(found: Any, problems: Problems) -> None:
    if not isinstance(found, dict):
        problems.append("subject: must be an object")
        return
    value = _mapping(found)
    for key in ("type", "id"):
        if key not in value:
            problems.append(f"subject.{key}: missing")
        elif not isinstance(value[key], str) or not value[key]:
            problems.append(f"subject.{key}: must be a non-empty string")
    for key in SUBJECT_STRINGS:
        if key in value and not isinstance(value[key], str):
            problems.append(f"subject.{key}: must be a string")
    if "pid" in value and (not _is_int(value["pid"]) or value["pid"] <= 0):
        problems.append("subject.pid: must be a positive integer")
    for key, item in value.items():
        if not _is_scalar(item):
            problems.append(f"subject.{key}: must be a scalar (string, number, boolean or null)")


def _check_actor(found: Any, problems: Problems) -> None:
    if not isinstance(found, dict):
        problems.append("actor: must be an object")
        return
    value = _mapping(found)
    if "type" not in value:
        problems.append("actor.type: missing")
    elif value["type"] not in ACTOR_TYPES:
        problems.append(f"actor.type: must be one of {', '.join(ACTOR_TYPES)}")
    for key in ACTOR_STRINGS:
        if key in value and not isinstance(value[key], str):
            problems.append(f"actor.{key}: must be a string")
    if "pid" in value and (not _is_int(value["pid"]) or value["pid"] <= 0):
        problems.append("actor.pid: must be a positive integer")
    for key, item in value.items():
        if not _is_scalar(item):
            problems.append(f"actor.{key}: must be a scalar (string, number, boolean or null)")


def validate(found: Any, strict: bool = False) -> Problems:
    """The problems with one decoded event; an empty list when it is valid."""
    if not isinstance(found, dict):
        return ["event: must be a JSON object"]
    obj = _mapping(found)
    problems: Problems = []
    for key in REQUIRED:
        if key not in obj:
            problems.append(f"{key}: missing")
    if "v" in obj and (not _is_int(obj["v"]) or obj["v"] != SCHEMA_VERSION):
        problems.append(f"v: must be {SCHEMA_VERSION}, got {obj['v']!r}")
    if "time" in obj:
        _check_time(obj["time"], problems)
    if "host" in obj and (not isinstance(obj["host"], str) or not _HOST.match(obj["host"])):
        problems.append("host: must be the lowercase short host name, without dots")
    if "source" in obj and (not isinstance(obj["source"], str) or not _SOURCE.match(obj["source"])):
        problems.append("source: must match [a-z][a-z0-9-]{0,31}")
    if "kind" in obj:
        kind: Any = obj["kind"]
        if not isinstance(kind, str) or len(kind) > 64 or not _KIND.match(kind):
            problems.append("kind: must be lowercase noun.verb, dotted, at most 64 characters")
    if "subject" in obj:
        _check_subject(obj["subject"], problems)
    if "actor" in obj:
        _check_actor(obj["actor"], problems)
    if "details" in obj:
        details: Any = obj["details"]
        if not isinstance(details, dict):
            problems.append("details: must be an object")
        elif _depth(details) > MAX_DETAILS_DEPTH:
            problems.append(f"details: nested deeper than {MAX_DETAILS_DEPTH} levels")
    if "level" in obj and obj["level"] not in LEVELS:
        problems.append(f"level: must be one of {', '.join(LEVELS)}")
    if "id" in obj:
        ident: Any = obj["id"]
        if not isinstance(ident, str) or not ident or len(ident) > 64:
            problems.append("id: must be a non-empty string of at most 64 characters")
    if strict:
        for key in obj:
            if key not in REQUIRED and key not in OPTIONAL:
                problems.append(f"{key}: unknown top-level field (strict)")
    _control("event", obj, problems)
    _finite("event", obj, problems)
    return problems


def _no_duplicates(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    seen: Dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate key {key!r}")
        seen[key] = value
    return seen


def _no_constants(name: str) -> Any:
    raise ValueError(f"{name} is not JSON")


def validate_line(line: Union[bytes, str], strict: bool = False) -> Problems:
    """The problems with one log line, its newline optional: size, UTF-8, JSON, then validate()."""
    data = line.encode("utf-8", "surrogatepass") if isinstance(line, str) else line
    body = data[:-1] if data.endswith(b"\n") else data
    problems: Problems = []
    if len(body) + 1 > MAX_LINE_BYTES:
        problems.append(f"line: {len(body) + 1} bytes with its newline, more than {MAX_LINE_BYTES}")
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as error:
        return problems + [f"line: not UTF-8 ({error.reason} at byte {error.start})"]
    try:
        obj = json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_no_constants)
    except ValueError as error:
        return problems + [f"line: not one JSON object ({error})"]
    return problems + validate(obj, strict=strict)


def validate_file(path: str, strict: bool = False) -> Problems:
    """Every line of an events.jsonl, each problem prefixed with its 1-based line number."""
    with open(path, "rb") as handle:
        data = handle.read()
    problems: Problems = []
    if not data:
        return problems
    lines = data.split(b"\n")
    last: Optional[bytes] = lines.pop()  # b"" when the file ends with a newline
    for number, line in enumerate(lines, 1):
        problems.extend(f"{number}: {problem}" for problem in validate_line(line, strict=strict))
    if last:
        number = len(lines) + 1
        problems.append(f"{number}: line: no newline at the end (a torn or partial write)")
        problems.extend(f"{number}: {problem}" for problem in validate_line(last, strict=strict))
    return problems


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    strict = False
    if args and args[0] == "--strict":
        strict = True
        args = args[1:]
    if not args or any(arg.startswith("-") and arg != "-" for arg in args):
        print("usage: eventcheck.py [--strict] FILE...", file=sys.stderr)
        return 2
    failed = False
    for path in args:
        try:
            problems = validate_file("/dev/stdin" if path == "-" else path, strict=strict)
        except OSError as error:
            problems = [f"0: cannot read: {error.strerror or error}"]
        for problem in problems:
            print(f"{path}:{problem}")
        failed = failed or bool(problems)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
