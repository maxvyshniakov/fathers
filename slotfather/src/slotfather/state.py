"""Queue locking, atomic entry and sequence writes, bounded journals, and persisted state schema contracts."""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
from pathlib import Path
import pwd
import stat
import time
from typing import Any, Callable, Iterable, Iterator, Mapping, cast

from fathers_common.atomic import atomic_write

from . import config as _config
from . import entry as _entry


def foreign_owner(directory: Path) -> int | None:
    """The uid that owns the queue when it is not this process's (effective) one, else None. A queue not made yet
    belongs to the owner of the directory it would be made in, unless that is a sticky shared one such as /tmp.
    Another user's queue is read only (#653 review: root through the guest agent running the installed command
    must not leave root's files in the runner's queue)."""
    path = Path(directory)
    while not os.path.lexists(path) and path != path.parent:
        path = path.parent
    try:
        info = path.stat()
    except OSError:
        return None
    if info.st_uid == os.geteuid() or (path != Path(directory) and info.st_mode & stat.S_ISVTX):
        return None
    return info.st_uid


def require_own(directory: Path, what: str) -> None:
    """Refuses `what`, a change to the queue, from a user other than the queue's owner."""
    owner = foreign_owner(directory)
    if owner is None:
        return
    try:
        name = pwd.getpwuid(owner).pw_name
    except KeyError:
        name = str(owner)
    raise _config.SlotError(f"the queue {directory} belongs to uid {owner} ({name}): {what} by uid {os.geteuid()} would leave "
                    f"files that user cannot write; run it as that user: sudo -u {name} slotfather ...")


SCHEMA_VERSION = 1


def schema_version(directory: Path) -> int:
    """Unstamped queues are v1. Reject unknown formats before any filesystem mutation."""
    path = directory / "schema.json"
    try:
        value = json.loads(path.read_text())["v"]
    except FileNotFoundError:
        return SCHEMA_VERSION
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise _config.SlotError(f"cannot read state schema at {path}: {error}") from None
    if type(value) is not int or value != SCHEMA_VERSION:
        raise _config.SlotError(f"unsupported state schema {value!r} at {path}; update slotfather")
    return value


@contextlib.contextmanager
def locked(directory: Path):
    """The queue's lock. In another user's queue (foreign_owner()) nothing is created: an existing lock is taken
    through a read-only handle, a missing queue is read as it is."""
    schema_version(directory)
    if foreign_owner(directory) is not None:
        try:
            handle = open(directory / ".lock", "r")
        except OSError:
            yield
            return
        with handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield
        return
    (directory / "queue").mkdir(parents=True, exist_ok=True)
    with open(directory / ".lock", "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        schema_version(directory)
        if not (directory / "schema.json").exists():
            write(directory / "schema.json", {"v": SCHEMA_VERSION})
        yield


def read_entry(path: Path) -> dict[str, Any] | None:
    """An entry as stored, read whole by rename. Missing or invalid JSON has no entry."""
    try:
        entry: object = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return cast(dict[str, Any], entry) if isinstance(entry, dict) else None


YIELD_SUFFIX = ".yield"
IDLE_SUFFIX = ".idle"
SESSION_SUFFIX = ".session"
SIDECAR_SUFFIXES = (YIELD_SUFFIX, IDLE_SUFFIX, SESSION_SUFFIX)


def idle_path(path: Path) -> Path:
    """The idle record beside a queue entry."""
    return path.with_suffix(IDLE_SUFFIX)


def session_path(path: Path) -> Path:
    """A session's run records beside its lease entry."""
    return path.with_suffix(SESSION_SUFFIX)


def session_uses(path: Path) -> dict[str, Any] | None:
    return read_entry(session_path(path))


@contextlib.contextmanager
def editing(path: Path) -> Iterator[dict[str, Any] | None]:
    """Read the current entry while holding the queue lock for its transaction."""
    with locked(path.parent.parent):
        yield read_entry(path)


def entries(directory: Path) -> list[tuple[Path, dict[str, Any]]]:
    found: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted((directory / "queue").glob("*.json")):
        entry = read_entry(path)
        if entry is not None and isinstance(entry.get("count"), int):
            found.append((path, entry))
    return found


def write(path: Path, entry: Any) -> None:
    """Writes `entry` as JSON to `path` whole via `fathers_common.atomic.atomic_write` (fathers#123, fathers#117)."""
    atomic_write(path, json.dumps(entry) + "\n")


def commit(path: Path, entry: Mapping[str, Any] | None,
           effect: _entry.SidecarEffect = _entry.NO_EFFECT) -> None:
    """Atomically commit `entry` (if changed) and apply declarative `SidecarEffect` sidecar mutations."""
    if effect.unlink_session and (effect.write_session is not None or effect.init_session is not None):
        raise _entry.IllegalTransitionError("conflicting session write and unlink in SidecarEffect")
    if effect.write_session is not None and effect.init_session is not None:
        raise _entry.IllegalTransitionError("conflicting write_session and init_session in SidecarEffect")
    if effect.unlink_yield and effect.write_yield is not None:
        raise _entry.IllegalTransitionError("conflicting yield write and unlink in SidecarEffect")
    if effect.write_session is not None:
        write(session_path(path), effect.write_session)
    elif effect.init_session is not None and not session_path(path).exists():
        write(session_path(path), effect.init_session)
    if effect.write_yield is not None:
        write(marker(path), effect.write_yield)
    if entry is not None:
        write(path, entry)
    if effect.unlink_yield:
        marker(path).unlink(missing_ok=True)
    if effect.unlink_idle:
        idle_path(path).unlink(missing_ok=True)
    if effect.unlink_session:
        session_path(path).unlink(missing_ok=True)


def remove_entry(path: Path) -> None:
    """Remove the queue entry JSON at `path` along with any `.yield`, `.idle`, and `.session` sidecars."""
    path.unlink(missing_ok=True)
    for suffix in SIDECAR_SUFFIXES:
        path.with_suffix(suffix).unlink(missing_ok=True)


def unknown(directory: Path) -> list[tuple[Path, dict[str, Any]]]:
    """Queue entries that read but that this client does not know (entry.unknown(); fathers#19), in queue order: a
    state it does not know, no state, or a field the scheduler needs missing (entries() leaves out one without an
    int count). Nothing grants, reaps or removes them: one may be a newer client's live lease. `status` reports
    them."""
    found: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted((directory / "queue").glob("*.json")):
        try:
            entry: object = json.loads(path.read_text())
        except (OSError, ValueError):
            continue  # unreadable(), or gone
        if isinstance(entry, dict) and _entry.unknown(cast(dict[str, Any], entry)) is not None:
            found.append((path, cast(dict[str, Any], entry)))
    return found


def unreadable(directory: Path) -> list[Path]:
    """Queue entries that do not read as one (empty or torn by a crash, or not JSON). entries() leaves them out and
    nothing removes them: one may be a newer client's live lease. `status` reports them."""
    found: list[Path] = []
    for path in sorted((directory / "queue").glob("*.json")):
        try:
            json.loads(path.read_text())
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            found.append(path)
    return found


def record(directory: Path, entry: dict[str, Any], outcome: str | _entry.Outcome, status: int | None = None,
           usage: dict[str, Any] | None = None, path: Path | None = None,
           expected: dict[str, Any] | None = None, *, capacity: Callable[[], int],
           above: Callable[[float, Any], bool]) -> dict[str, Any]:
    """Add a request that left the queue to history.jsonl: released (it ran), left (it gave up waiting), reaped
    (its holder died) or refused, with what its process tree used where the holder measured it. `capacity()` gives the
    queue's slots for the line (settings.total()), `above(ran, p90)` whether a run is past its series `expected`
    (runtype.above()): the caller's, so that persistence imports neither. Callers hold the queue lock. Returns the line's
    fields (the events of events.py read them)."""
    if _entry.stage_of(entry) == _entry.Stage.UNSETTLED:
        entry = dict(entry)
        _entry.settle(entry)
    ended = time.time()
    extra_fields: dict[str, Any] = {}
    never_ran = False
    if isinstance(outcome, (_entry.Ran, _entry.Left, _entry.Reaped, _entry.Refused)):
        extra_fields = _entry.outcome_fields(outcome)
        outcome_str = str(extra_fields["outcome"])
        if isinstance(outcome, _entry.Ran) and status is None:
            status = outcome.exit
        never_ran = isinstance(outcome, (_entry.Left, _entry.Refused))
    else:
        outcome_str = outcome
    queued = _config.seconds(entry.get("since"))
    granted_stamp = None if never_ran else entry.get("granted")
    granted = None if never_ran else _config.seconds(granted_stamp)
    item: dict[str, Any] = {"v": _config.HISTORY_VERSION, "label": entry.get("label"), "cmd": entry.get("cmd"), "cwd": entry.get("cwd"),
            "count": entry.get("count"), "pid": entry.get("pid"), "queued": entry.get("since"),
            "granted": granted_stamp, "ended": _config.now(), "outcome": outcome_str,
            "waited": None if queued is None else round((granted or ended) - queued),
            "ran": None if granted is None else round(ended - granted), "status": status,
            "priority": _entry.priority_of(entry)}
    for key in ("reason", "was_held"):
        if key in extra_fields:
            item[key] = extra_fields[key]
    ticket = _entry.ticket_of(entry)
    if ticket is not None:
        item["ticket"] = ticket
    with contextlib.suppress(_config.SlotError, OSError):
        item.update(cores=_config.cores(), capacity=capacity())
    item.update(usage or {})
    if not never_ran:
        yielded = _entry.yielded_of(entry)
        away = _entry.epoch(entry.get("yielded_at"))
        total_yielded = round(yielded + (max(0.0, ended - away) if away is not None else 0.0), 2)
        if entry.get("yields"):
            item["yields"] = int(entry["yields"])
        if entry.get("yields") or total_yielded > 0:
            item["yielded"] = total_yielded
    if entry.get("until"):
        item["until"] = entry["until"]  # when its client would have given up (run --wait): the replay's patience
    fields = entry
    if fields.get("aged") in _config.PRIORITIES:
        item["aged"] = fields["aged"]  # the class it was granted in after ageing; `priority` stays the one it asked
    if fields.get("reprioritized") in _config.PRIORITIES:
        item["reprioritized"] = fields["reprioritized"]  # the class it asked first (slotfather reprioritize)
    meta = _entry.meta_of(entry)
    if meta:
        item["meta"] = meta
    if path is not None or isinstance(entry.get("lease_id"), str):
        # the entry's unique id, as status --json and EVENTS give it (an older client's entry: the same fallback)
        item["lease_id"] = _entry.lease_id(path.stem if path is not None else "", entry)
    if expected is not None and outcome_str == "released" and isinstance(item.get("ran"), (int, float)) \
            and not entry.get("session"):
        # the run type it is judged by and its verdict against that series (runtype.py, fathers#106)
        item["run_key"] = expected["key"]
        item["verdict"] = "pending" if above(float(item["ran"]), expected.get("p90")) else "normal"
    elif expected is None and outcome_str == "released" and entry.get("run_key"):
        item["run_key"] = entry["run_key"]
    for key in ("label_given", "label_derived", "project"):
        if isinstance(entry.get(key), str):
            item[key] = entry[key]  # the derived label, and the --label as given where it differs
    if isinstance(entry.get("session"), dict):
        item["session"] = entry["session"]  # a measurement session: its name, runs, busy seconds, how it ended
    if isinstance(entry.get("git"), dict):
        item["git"] = entry["git"]  # head, tree, dirty of its directory (fathers#97)
    with contextlib.suppress(OSError):
        append_bounded(directory / "history.jsonl", json.dumps(item) + "\n", _config.HISTORY_MAX)
    return item


def append_bounded(path: Path, line: str, limit: int) -> None:
    """Append a line; past `limit` bytes the file becomes <name>.1, replacing the previous one, so at most two
    files' worth stays. Readers read <name>.1 first, then <name>. The rotation holds an flock on the file it wrote
    and renames only while that file is still <name>: of two writers that both find it over the limit, the second
    finds it rotated already and leaves <name>.1 alone (fathers#123 review)."""
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        size = os.fstat(fd).st_size
        # A torn earlier write (a holder killed mid-line) must not swallow this line: start it on a line of its own.
        prefix = b"\n" if size and os.pread(fd, 1, size - 1) != b"\n" else b""
        os.write(fd, prefix + line.encode("utf-8"))  # one write, at the end even with other appenders
        if os.fstat(fd).st_size > limit:
            rotate_bounded(path, fd, limit)
    finally:
        os.close(fd)


def rotate_bounded(path: Path, fd: int, limit: int) -> None:
    """Renames `path` to <name>.1 when `fd` (open on the file this writer appended to) is still `path` and over
    `limit`, under an flock on that file."""
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        mine, current = os.fstat(fd), os.stat(path)
        if (mine.st_dev, mine.st_ino) == (current.st_dev, current.st_ino) and current.st_size > limit:
            path.replace(path.with_name(path.name + ".1"))
    except FileNotFoundError:
        pass
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)


def objects(lines: Iterable[str | bytes]) -> list[dict[str, Any]]:
    """The JSON objects among lines; lines that do not parse (a torn write, a foreign line) are skipped."""
    found: list[dict[str, Any]] = []
    for line in lines:
        with contextlib.suppress(ValueError, UnicodeDecodeError):
            item: object = json.loads(line)
            if isinstance(item, dict):
                found.append(cast(dict[str, Any], item))
    return found


def journal(path: Path) -> Iterator[dict[str, Any]]:
    """The JSON objects of a bounded file and its rotated predecessor, oldest first."""
    for part in (path.with_name(path.name + ".1"), path):
        try:
            data = part.read_bytes()
        except FileNotFoundError:
            continue
        yield from objects(data.splitlines())


def tail_objects(path: Path, limit: int) -> list[dict[str, Any]]:
    """The last `limit` JSON objects of one file, read backwards from its end in blocks."""
    try:
        handle = open(path, "rb")
    except FileNotFoundError:
        return []
    with handle:
        end = handle.seek(0, os.SEEK_END)
        block, data = 65536, b""
        while True:
            start = max(0, end - block)
            handle.seek(start)
            data = handle.read(end - start)
            lines = data.splitlines()
            found = objects(lines[1:] if start else lines)  # the first line of a block may be cut
            if len(found) >= limit or not start:
                return found[-limit:]
            block *= 4


def history_items(directory: Path | None = None) -> list[dict[str, Any]]:
    """Every request in history.jsonl (and its rotated part), oldest first. Version-1 lines read as they are: they
    lack "v" and the usage fields."""
    return list(journal((directory or _config.state_dir()) / "history.jsonl"))


def held_cores(item: dict[str, Any]) -> float | None:
    """The cores a history line's slots held (its count times its cores over its capacity), or None for a line
    without them, an all-slot request or a session: those ask for the machine, not for cores."""
    found: list[int] = []
    for key in ("count", "cores", "capacity"):
        value = item.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            return None
        found.append(value)
    count, cores, capacity = found
    if item.get("session") or (capacity > 1 and count >= capacity):
        return None
    return round(count * cores / capacity, 2)


def history_lines(limit: int, directory: Path | None = None) -> list[str]:
    """The last `limit` requests that left the queue (every one for 0), oldest first. The rotated part is read only
    when the current file holds fewer."""
    path = (directory or _config.state_dir()) / "history.jsonl"
    if limit:
        items = tail_objects(path, limit)
        if len(items) < limit:
            items = tail_objects(path.with_name(path.name + ".1"), limit - len(items)) + items
    else:
        items = list(journal(path))
    found: list[str] = []
    for item in items:
        ended = _config.seconds(item.get("ended"))
        when = time.strftime("%m-%d %H:%M", time.localtime(ended)) if ended is not None else "?"
        waited = "?" if item.get("waited") is None else _config.duration(item["waited"])
        ran = "" if item.get("ran") is None else f" ran {_config.duration(item['ran'])}"
        status = "" if item.get("status") is None else f" exit {item['status']}"
        used = ""
        if isinstance(item.get("cpu"), (int, float)):
            wall = item.get("wall") or item.get("ran")
            cores_used = f"{item['cpu'] / wall:.1f} cores" if wall else f"{item['cpu']:.0f} s CPU"
            held = held_cores(item)
            if wall and held is not None and item["cpu"] / wall >= _config.CORES_OVER * held:
                cores_used += f" > {round(held, 1):g} held"  # fathers#148: far more than its slots hold
            used = f" used {cores_used}, {item.get('rss_peak_mb', 0):.0f} MB"
        meta = _entry.meta_of(item)
        tags = f" meta={_config.meta_text(meta)}" if meta else ""
        found.append(f"{when} {item.get('outcome')}: {item.get('count')} slot(s) {item.get('label')} waited "
                     f"{waited}{ran}{status}{used}{tags} cmd={item.get('cmd')}")
    return found


def marker(path: Path) -> Path:
    """The yield request beside a queue entry (SLOTFATHER_YIELD in a yieldable lease's command)."""
    return path.with_suffix(YIELD_SUFFIX)


def asked(path: Path) -> dict[str, Any] | None:
    """The yield request for the entry at `path`, or None."""
    try:
        found = json.loads(marker(path).read_text())
    except (OSError, ValueError):
        return None
    return cast(dict[str, Any], found) if isinstance(found, dict) else None


QUEUE_LOG_MAX = 4 << 20  # bytes of queue.log; past it the file becomes queue.log.1, replacing the older one


def log(directory: Path, message: str) -> None:
    """Appends a line to queue.log, bounded as history.jsonl is (append_bounded()); callers hold the queue lock."""
    with contextlib.suppress(OSError):
        append_bounded(directory / "queue.log", f"{_config.now()} {message}\n", QUEUE_LOG_MAX)
