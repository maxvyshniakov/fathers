"""Resources that outlive the process that made them: a booted iOS simulator, a Docker container, a `tailscale serve`
route, a Safari Technology Preview instance, or anything a pair of commands can check and stop.

A resource entry is an ordinary entry (owner, TTL, keep, hand-over, `reap`, the hooks and the sweep all apply) with
`mode: "resource"`, no process group of its own, and `resource: {kind, id, ...}`. Each kind knows three things:

  listing   what of its kind runs now ({id: description}), from one call, or None when that cannot be told
  alive     whether one resource still runs: True, False, or None (unknown, which never marks it gone)
  stop      the argv that ends it; it runs without a shell, with a timeout, and the resource must then be seen gone

  simulator        xcrun simctl list -j devices (Booted)       xcrun simctl shutdown UDID
  docker           docker ps -q --no-trunc                     docker stop -t GRACE ID (then docker rm ID with --remove)
  tailscale-serve  tailscale serve status --json               tailscale serve --PROTO=PORT [--set-path=P] off --yes
  safari-tp        its main process, by pid and start time     osascript: tell application id ... to quit
  command          --alive ARGV (exit 0 alive, 1 gone)         --stop ARGV
  worktree         git worktree list (its repository's)        worktrees.plan: git worktree remove, then that tip's ref
  dir              the folder exists (not a symbolic link)     dirs.plan: rm -rf, once no guard keeps it

A tailscale-serve id is `PROTO:PORT` or `PROTO:PORT/PATH` (`https:6475`, `https:443/docs`, `tcp:5432`); stopping one
removes that route only. An unowned resource is only reported (by the sweep), never stopped, with one exception: a
`worktree` no entry owns that passes every guard and was idle for a day is removed by the sweep through the same
guarded removal an owned one's stop uses (worktrees.plan; owner-approved, fathers#112).

A reap of worktrees and dirs takes the requester's evidence (fathers#171): `--base` (refs its work may be merged
into), `--disposable` (ignored-file patterns that may go), `--override REASON` (lifts the work guards and "still in
use after stopping", never an integrity guard; evented with its reason) and `--stop-inside` (first stops the
unowned processes working inside, see `stop_inside`).
"""
from __future__ import annotations

import copy
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, TextIO, Tuple, cast

from . import entry as _entry
from . import events as _events
from . import procs as _procs
from . import reap as _reap
from . import safety as _safety
from . import errors as _errors
from .defaults import CREATE_TIMEOUT, NO_TTL, RESOURCE_TIMEOUT, SETTLE, STOP_TIMEOUT
from .entry import Entry
from .state import Store, as_dict, as_list
from .text import duration

EXTRA_PATH = ("/opt/homebrew/bin", "/usr/local/bin", "/Applications/Docker.app/Contents/Resources/bin", "/usr/bin")
UDID = re.compile(r"\A[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\Z")
CONTAINER = re.compile(r"\A[0-9a-f]{64}\Z")
ROUTE = re.compile(r"\A(https|http|tcp|tls-terminated-tcp):(\d{1,5})(/\S*)?\Z")
STP_BUNDLE = "com.apple.SafariTechnologyPreview"
STP_MAIN = re.compile(r"Safari Technology Preview\.app/Contents/MacOS/Safari Technology Preview(\s|$)")


def stp_main(cmd: str) -> bool:
    """A Safari Technology Preview main process. $SPAWNFATHER_STP_APP (or legacy $JUNKFATHER_STP_APP; the tests) names
    the one executable that counts, so the machine's own instance, a person's or another suite's, is never theirs to
    count or quit."""
    only = os.environ.get("SPAWNFATHER_STP_APP") or os.environ.get("JUNKFATHER_STP_APP")
    if only:
        return re.search(re.escape(only) + r"(\s|$)", cmd) is not None
    return STP_MAIN.search(cmd) is not None

Listing = Optional[Dict[str, Dict[str, Any]]]


class Refused(ValueError):
    """A stop a guard refuses (a worktree with work in it): the message says why, and nothing was run."""


class Ran:
    """One command's outcome: code None when it could not run or timed out (`error` says which)."""

    def __init__(self, code: Optional[int], out: str = "", err: str = "", error: str = ""):
        self.code, self.out, self.err, self.error = code, out, err, error

    def why(self) -> str:
        return self.error or (self.err.strip() or self.out.strip())[:300] or f"exit {self.code}"


def tool(name: str) -> Optional[str]:
    """A command on PATH, else in the usual install places (launchd's PATH can be short)."""
    return shutil.which(name) or shutil.which(name, path=os.pathsep.join(EXTRA_PATH))


def run(argv: List[str], timeout: float = RESOURCE_TIMEOUT, env: Optional[Mapping[str, str]] = None) -> Ran:
    """argv as given, never through a shell, with a timeout; no stdin."""
    found = tool(argv[0]) if "/" not in argv[0] else argv[0]
    if not found:
        return Ran(None, error=f"{argv[0]}: not found")
    try:
        done = subprocess.run([found, *argv[1:]], capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL, env=env)
    except subprocess.TimeoutExpired:
        return Ran(None, error=f"{argv[0]} did not answer in {timeout:.0f} s")
    except OSError as error:
        return Ran(None, error=f"{argv[0]}: {error}")
    return Ran(done.returncode, done.stdout, done.stderr)


# Kinds

class Kind:
    name = ""
    reports = True  # whether the sweep reports unowned resources of this kind
    lists = True  # whether one listing tells every resource's liveness (Snapshot.alive passes it)

    def prepare(self, argv: List[str]) -> None:
        """Before `own --kind` runs its command: what the listings that follow need to know of it."""

    def check(self, resource: Mapping[str, Any]) -> Optional[str]:
        """Why a stop would be refused now, or None: what a dry run reports."""
        return None

    def listing(self, table: Optional[_procs.Table]) -> Listing:
        return None

    def alive(self, resource: Mapping[str, Any], listed: Listing, table: Optional[_procs.Table]) -> Optional[bool]:
        if listed is None:
            return None
        return str(resource.get("id")) in listed

    def resolve(self, given: str, listed: Listing) -> Dict[str, Any]:
        """The resource record for an id given on the command line. Refused when there is none, with docs/cli.md's
        code: 2 (errors.Usage) for an id that cannot name one, 6 when none runs, 3 when the state refuses it, 1
        when it cannot be told."""
        if listed is None:
            raise _errors.Refused(f"the running {self.name}s cannot be listed")
        if given in listed:
            return {"kind": self.name, "id": given, **listed[given]}
        raise _errors.Refused(f"no running {self.name} {given}", _errors.NOT_FOUND)

    def stop(self, resource: Mapping[str, Any], grace: float) -> List[List[str]]:
        raise NotImplementedError

    def named(self, argv: List[str], out: str) -> List[str]:
        """Ids the creating command names (its arguments, its output), most likely first."""
        return []


class Simulator(Kind):
    name = "simulator"

    def listing(self, table: Optional[_procs.Table]) -> Listing:
        ran = run(["xcrun", "simctl", "list", "-j", "devices"])
        if ran.code != 0:
            return None
        try:
            data = json.loads(ran.out)
        except ValueError:
            return None
        found: Dict[str, Dict[str, Any]] = {}
        for runtime, devices in as_dict(as_dict(data).get("devices")).items():
            for device in map(as_dict, as_list(devices)):
                udid = device.get("udid")
                if isinstance(udid, str) and device.get("state") not in (None, "Shutdown"):
                    found[udid] = {"name": device.get("name"), "runtime": runtime.rsplit(".", 1)[-1]}
        return found

    def resolve(self, given: str, listed: Listing) -> Dict[str, Any]:
        if not UDID.match(given):
            raise _errors.Usage(f"not a simulator UDID: {given!r} (xcrun simctl list devices shows them)",
                                "own-resource")
        return super().resolve(given.upper(), listed)

    def stop(self, resource: Mapping[str, Any], grace: float) -> List[List[str]]:
        return [["xcrun", "simctl", "shutdown", str(resource["id"])]]

    def named(self, argv: List[str], out: str) -> List[str]:
        return [word.upper() for word in argv if UDID.match(word)]


class Docker(Kind):
    name = "docker"

    def listing(self, table: Optional[_procs.Table]) -> Listing:
        ran = run(["docker", "ps", "--no-trunc", "--format", "{{.ID}}\t{{.Names}}\t{{.Image}}"])
        if ran.code != 0:
            return None
        found: Dict[str, Dict[str, Any]] = {}
        for line in ran.out.splitlines():
            parts = line.split("\t")
            if parts and CONTAINER.match(parts[0]):
                found[parts[0]] = {"name": parts[1] if len(parts) > 1 else None,
                                   "image": parts[2] if len(parts) > 2 else None}
        return found

    def resolve(self, given: str, listed: Listing) -> Dict[str, Any]:
        ran = run(["docker", "inspect", "--type", "container", "--format", "{{.Id}}\t{{.Name}}", given])
        if ran.code != 0:  # None: docker could not run; else it answered that there is no such container
            raise _errors.Refused(f"docker inspect {given}: {ran.why()}",
                                  _errors.FAILED if ran.code is None else _errors.NOT_FOUND)
        full, _, name = ran.out.strip().partition("\t")
        if not CONTAINER.match(full):
            raise _errors.Refused(f"docker inspect {given} gave no container id")
        if listed is None:
            raise _errors.Refused("running containers cannot be listed (docker ps failed)")
        if full not in listed:
            raise _errors.Refused(f"container {given} is not running", _errors.REFUSED)
        return {"kind": self.name, "id": full, "name": name.lstrip("/") or None,
                **({"image": listed[full].get("image")} if listed else {})}

    def stop(self, resource: Mapping[str, Any], grace: float) -> List[List[str]]:
        steps = [["docker", "stop", "-t", str(int(max(1, grace))), str(resource["id"])]]
        if resource.get("remove"):
            steps.append(["docker", "rm", str(resource["id"])])
        return steps

    def named(self, argv: List[str], out: str) -> List[str]:
        lines = [line.strip() for line in out.splitlines() if CONTAINER.match(line.strip())]
        return lines[-1:]


class TailscaleServe(Kind):
    name = "tailscale-serve"

    def listing(self, table: Optional[_procs.Table]) -> Listing:
        ran = run(["tailscale", "serve", "status", "--json"])
        if ran.code != 0:
            return None
        if not ran.out.strip():
            return {}
        try:
            data = json.loads(ran.out)
        except ValueError:
            return None
        return routes(as_dict(data)) if isinstance(data, dict) else None

    def resolve(self, given: str, listed: Listing) -> Dict[str, Any]:
        if not ROUTE.match(given):
            raise _errors.Usage(f"not a serve route: {given!r} (PROTO:PORT or PROTO:PORT/PATH, like https:6475)",
                                "own-resource")
        return super().resolve(given, listed)

    def stop(self, resource: Mapping[str, Any], grace: float) -> List[List[str]]:
        found = ROUTE.match(str(resource["id"]))
        if not found:
            raise ValueError(f"not a serve route: {resource['id']!r}")
        proto, port, path = found[1], found[2], found[3]
        argv = ["tailscale", "serve", f"--{proto}={port}"]
        if path and path != "/":
            argv.append(f"--set-path={path}")
        return [argv + ["off", "--yes"]]

    def named(self, argv: List[str], out: str) -> List[str]:
        """The route a `tailscale serve` command sets: its --PROTO=PORT (or --PROTO PORT) and --set-path, else the
        default `https:443` (`tailscale serve --bg 6475`)."""
        if "serve" not in argv:
            return []
        words = argv[argv.index("serve") + 1:]
        options: Dict[str, str] = {}
        for index, word in enumerate(words):
            name, equals, value = word.partition("=")
            if not equals and name in ("--https", "--http", "--tcp", "--tls-terminated-tcp", "--set-path") \
                    and index + 1 < len(words):
                value = words[index + 1]
            if name.startswith("--"):
                options[name[2:]] = value
        if "service" in options or any(w in ("status", "reset", "drain", "clear", "off") for w in words):
            return []
        proto = next((p for p in ("https", "http", "tcp", "tls-terminated-tcp") if options.get(p)), None)
        port = options[proto] if proto else "443"
        path = options.get("set-path") or ""
        if path and not path.startswith("/"):
            path = "/" + path
        route = f"{proto or 'https'}:{port}" + ("" if path in ("", "/") else path)
        return [route] if ROUTE.match(route) else []


def routes(status: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    """`tailscale serve status --json` as route ids: one per web handler path, one per TCP forward."""
    found: Dict[str, Dict[str, Any]] = {}
    ports = as_dict(status.get("TCP"))
    web = as_dict(status.get("Web"))
    for port, value in ports.items():
        if not isinstance(value, dict) or not port.isdigit():
            continue
        how = as_dict(value)
        if how.get("TCPForward"):
            proto = "tls-terminated-tcp" if how.get("TerminateTLS") else "tcp"
            found[f"{proto}:{port}"] = {"target": how.get("TCPForward")}
            continue
        proto = "https" if how.get("HTTPS") else "http" if how.get("HTTP") else None
        if proto is None:
            continue
        for host_port, site in web.items():
            if not host_port.endswith(f":{port}") or not isinstance(site, dict):
                continue
            for path, raw in as_dict(as_dict(site).get("Handlers")).items():
                handler = as_dict(raw)
                target = handler.get("Proxy") or handler.get("Path") or handler.get("Text") if handler else None
                found[f"{proto}:{port}" + ("" if path == "/" else path)] = {"target": target}
    return found


class SafariTP(Kind):
    """One Safari Technology Preview instance, by its main process (pid and start time): `open -g -a ...` exits at
    once, so the wrapper owns only a main process that appeared after it. Its stop is the app's own quit, sent only
    while that instance is the only one running: a quit by bundle id cannot tell two instances apart, and a
    person's own windows are never closed. Otherwise the stop fails, is reported, and the instance keeps running."""
    name = "safari-tp"
    reports = False  # a person's own browser as often as an agent's: not reported

    def listing(self, table: Optional[_procs.Table]) -> Listing:
        if table is None:
            return None
        return {str(p.pid): {"pid": p.pid, "start": p.start, "name": "Safari Technology Preview"}
                for p in table.procs.values() if p.uid == os.getuid() and stp_main(p.cmd)}

    def alive(self, resource: Mapping[str, Any], listed: Listing, table: Optional[_procs.Table]) -> Optional[bool]:
        if table is None:
            return None
        return table.alive(resource.get("pid"), resource.get("start"))

    def resolve(self, given: str, listed: Listing) -> Dict[str, Any]:
        if listed is not None and given in (STP_BUNDLE, "safari-tp"):
            if len(listed) != 1:
                raise _errors.Usage(f"{len(listed)} instances run; name one by its pid", "own-resource")
            given = next(iter(listed))
        if not given.isdigit():
            raise _errors.Usage(f"safari-tp takes --id PID (of its main process) or {STP_BUNDLE}", "own-resource")
        return super().resolve(given, listed)

    def stop(self, resource: Mapping[str, Any], grace: float) -> List[List[str]]:
        table = _table()
        listed = self.listing(table)
        if listed is None:
            raise ValueError("the process table cannot be read")
        mine = str(resource.get("pid"))
        if set(listed) != {mine} or listed[mine].get("start") != resource.get("start"):
            raise ValueError(f"{len(listed)} Safari Technology Preview instance(s) run and ours is not the only one; "
                             "a quit could close someone else's windows, so nothing was quit")
        return [["osascript", "-e", f'tell application id "{STP_BUNDLE}" to quit']]


class Command(Kind):
    name = "command"
    reports = False
    lists = False

    def alive(self, resource: Mapping[str, Any], listed: Listing, table: Optional[_procs.Table]) -> Optional[bool]:
        argv = resource.get("alive")
        if not isinstance(argv, list) or not argv:
            return None
        ran = run([str(word) for word in as_list(argv)])
        return True if ran.code == 0 else False if ran.code == 1 else None

    def stop(self, resource: Mapping[str, Any], grace: float) -> List[List[str]]:
        return [[str(word) for word in as_list(resource.get("stop"))]]


class Worktree(Kind):
    """A linked git worktree, by its real path and its repository's common git dir (worktrees.py has the guards). It
    runs while the path exists and that repository's `git worktree list` lists it; its stop is worktrees.plan, the
    one guarded removal the sweep uses too: `git worktree remove`, then that branch tip's ref, refused while any
    guard keeps it. Each `own --kind worktree` works on its own copy of this kind (`prepare` keeps the -C folder)."""
    name = "worktree"
    reports = False
    lists = False

    def __init__(self) -> None:
        self.where: Optional[str] = None

    def prepare(self, argv: List[str]) -> None:
        self.where = _worktrees_where(argv)

    def listing(self, table: Optional[_procs.Table]) -> Listing:
        from . import worktrees
        items = worktrees.listing(self.where or os.getcwd())
        if items is None:
            return None
        repo = str(items[0]["path"])
        common = worktrees.common_dir(repo)
        return {str(i["path"]): {"repo": repo, "common": common, "branch": i.get("branch")}
                for i in items if not i.get("main")}

    def alive(self, resource: Mapping[str, Any], listed: Listing, table: Optional[_procs.Table]) -> Optional[bool]:
        """Gone when its repository no longer lists it, or when the repository there is another one (a different
        common git dir): nothing of the recorded one is left to remove."""
        from . import worktrees
        path = str(resource.get("id"))
        items = worktrees.listing(str(resource.get("repo") or path))
        if items is None:
            return False if not os.path.exists(path) and not os.path.exists(str(resource.get("repo"))) else None
        recorded = resource.get("common")
        if recorded and worktrees.common_dir(str(items[0]["path"])) != recorded:
            return False
        return os.path.isdir(path) and any(i.get("path") == path for i in items)

    def resolve(self, given: str, listed: Listing) -> Dict[str, Any]:
        from . import worktrees
        path = os.path.realpath(os.path.expanduser(given))
        items = worktrees.listing(path) if os.path.isdir(path) else None
        item = next((i for i in items or [] if i.get("path") == path), None)
        if items is None or item is None:
            raise _errors.Refused(f"{path} is not a git worktree", _errors.NOT_FOUND)
        if item.get("main"):
            raise _errors.Refused(f"{path} is a repository's main worktree", _errors.REFUSED)
        repo = str(items[0]["path"])
        return {"kind": self.name, "id": path, "repo": repo, "common": worktrees.common_dir(repo),
                "branch": item.get("branch")}

    def stop(self, resource: Mapping[str, Any], grace: float) -> List[List[str]]:
        from . import worktrees
        return worktrees.plan(resource, exclude=as_list(resource.get("_exclude")),
                              evidence=worktrees.Evidence.of(resource.get("_evidence")))

    def check(self, resource: Mapping[str, Any]) -> Optional[str]:
        from . import worktrees
        try:
            worktrees.plan(resource, exclude=as_list(resource.get("_exclude")),
                           evidence=worktrees.Evidence.of(resource.get("_evidence")))
        except Refused as error:
            return str(error)
        return None

    def named(self, argv: List[str], out: str) -> List[str]:
        """The path a `git [-C DIR] worktree add [OPTIONS] PATH [COMMIT]` names, made absolute."""
        words = argv[1:] if argv and os.path.basename(argv[0]) == "git" else []
        if "worktree" not in words:
            return []
        rest = words[words.index("worktree") + 1:]
        if rest[:1] != ["add"]:
            return []
        valued = ("-b", "-B", "--reason", "--orphan")
        index = 1
        while index < len(rest):
            word = rest[index]
            if word == "--":
                index += 1
                break
            if word in valued:
                index += 2
                continue
            if word.startswith("-"):
                index += 1
                continue
            break
        if index >= len(rest):
            return []
        base = _worktrees_where(argv) or os.getcwd()
        return [os.path.realpath(os.path.join(base, os.path.expanduser(rest[index])))]


class Dir(Kind):
    """A folder a requester owns (a lane's scratch or build directory, fathers#171), by its real path. It runs while
    the folder exists; its stop is dirs.plan, which refuses `/`, the home folder, Spawnfather's state, a symbolic
    link, anything holding a Git repository or worktree, and any folder a process works in, then `rm -rf`."""
    name = "dir"
    reports = False
    lists = False

    def alive(self, resource: Mapping[str, Any], listed: Listing, table: Optional[_procs.Table]) -> Optional[bool]:
        path = str(resource.get("id"))
        if not os.path.lexists(path):
            return False
        return True if os.path.isdir(path) and not os.path.islink(path) else None

    def resolve(self, given: str, listed: Listing) -> Dict[str, Any]:
        from . import dirs
        absolute = os.path.abspath(os.path.expanduser(given))
        if os.path.islink(absolute):
            raise _errors.Refused(f"{absolute} is a symbolic link", _errors.REFUSED)
        path = os.path.realpath(absolute)
        if not os.path.lexists(path):
            raise _errors.Refused(f"no folder {path}", _errors.NOT_FOUND)
        why = dirs.integrity_refusal(path)
        if why:
            raise _errors.Refused(f"{path}: {why}", _errors.REFUSED)
        return {"kind": self.name, "id": path}

    def stop(self, resource: Mapping[str, Any], grace: float) -> List[List[str]]:
        from . import dirs
        return dirs.plan(resource, exclude=as_list(resource.get("_exclude")),
                         evidence=dirs.Evidence.of(resource.get("_evidence")))

    def check(self, resource: Mapping[str, Any]) -> Optional[str]:
        try:
            self.stop(resource, 0.0)
        except Refused as error:
            return str(error)
        return None


def _worktrees_where(argv: List[str]) -> Optional[str]:
    """The folder a `git -C DIR ...` command runs in, or None (the current one)."""
    words = argv[1:] if argv and os.path.basename(argv[0]) == "git" else []
    where: Optional[str] = None
    for index, word in enumerate(words[:-1]):
        if word == "worktree":
            break
        if word == "-C":
            where = os.path.join(where or os.getcwd(), os.path.expanduser(words[index + 1]))
    return where


KINDS: Dict[str, Kind] = {k.name: k for k in (Simulator(), Docker(), TailscaleServe(), SafariTP(), Command(),
                                               Worktree(), Dir())}
FOLDERS = ("worktree", "dir")  # the kinds a reap takes the requester's evidence for (fathers#171)
WRAPPABLE = ("simulator", "docker", "tailscale-serve", "safari-tp", "worktree")


def key(resource: Mapping[str, Any]) -> str:
    return f"{resource.get('kind')}:{resource.get('id')}"


class Snapshot:
    """Each kind listed at most once per pass (a sweep, a list)."""

    def __init__(self, table: Optional[_procs.Table]):
        self.table = table
        self.listed: Dict[str, Listing] = {}

    def listing(self, kind: str) -> Listing:
        if kind not in self.listed:
            self.listed[kind] = KINDS[kind].listing(self.table) if kind in KINDS else None
        return self.listed[kind]

    def bound(self, resource: Mapping[str, Any]) -> Callable[[], Optional[bool]]:
        """`alive` for this one resource, as resources.stop takes it."""
        return lambda: self.alive(resource)

    def alive(self, resource: Mapping[str, Any]) -> Optional[bool]:
        kind = KINDS.get(str(resource.get("kind")))
        if kind is None:
            return None
        return kind.alive(resource, self.listing(kind.name) if kind.lists else None, self.table)


def subject(entry: Mapping[str, Any]) -> Dict[str, Any]:
    resource = as_dict(entry.get("resource"))
    found: Dict[str, Any] = {"type": "resource", "id": key(resource), "kind": str(resource.get("kind")),
                             "entry": str(entry.get("id"))}
    label = entry.get("label") or resource.get("name")
    if label:
        found["label"] = str(label)
    return found


def describe(resource: Mapping[str, Any]) -> str:
    name = f" ({resource['name']})" if resource.get("name") else ""
    ident = str(resource.get("id"))
    return f"{resource.get('kind')} {ident[:12] if resource.get('kind') == 'docker' else ident}{name}"


def owned_keys(store: Store) -> Dict[str, str]:
    """kind:id -> entry id, for every live resource entry."""
    return {key(as_dict(e.get("resource"))): e["id"] for e in store.entries()
            if e.get("state") in _entry.LIVE and isinstance(e.get("resource"), dict)}


# Owning

def record(store: Store, resource: Dict[str, Any], argv: List[str], ttl: float, keep: str, label: Optional[str],
           owner: Dict[str, Any], cwd: str, extra: Optional[Dict[str, Any]] = None) -> Tuple[Optional[Entry], str]:
    """A running resource entry, or None and why not: one entry per resource. `extra`: keys the new entry also
    gets (`no_ttl` for a lane owner's)."""
    fresh = _entry.new(store, argv, ttl, keep, label, owner, "resource", cwd)
    fresh.update(cast(Entry, extra or {}))
    entry, holder = _entry.resource_owned(store, fresh, resource,
                                          lambda e: key(e.get("resource") or {}) == key(resource))
    if entry is None:
        return None, f"already owned by {holder}"
    from .owner import lane_key
    lane = lane_key(owner)
    _events.write(store.root, "resource.owned", subject(entry), _events.actor_of(entry.get("owner")),
                  {"resource": resource, "argv": argv, "cwd": cwd, "ttl": ttl, "keep": keep,
                   **({"owner": lane, "no_ttl": True} if lane else {})})
    return entry, ""


def announce(entry: Entry, out: Optional[TextIO] = None) -> None:
    from .owner import describe as who
    keep = "" if entry["keep"] == "owner" else f", kept for the {entry['keep']}"
    note = f" ({entry.get('claim_note')})" if entry.get("claim_note") else ""
    what = describe(entry.get("resource") or {})
    print(f"spawnfather: {entry['id']} owns {what} for {who(entry['owner'])}{note}, ttl "
          f"{duration(entry['ttl'])}{keep}; stop it: spawnfather reap --id {entry['id']}", file=out or sys.stdout,
          flush=True)


def own_resource(store: Store, kind: str, given: str, ttl: float, keep: str, label: Optional[str],
                 owner: Dict[str, Any], alive: Optional[List[str]] = None, stop: Optional[List[str]] = None,
                 remove: bool = False, as_json: bool = False) -> int:
    """`own-resource`: record a resource that runs now. cli.py refuses safari-tp and a command kind without --alive
    and --stop before this runs. A lane owner's resource has no TTL, and owning one the same lane holds already
    succeeds ("already"), so a requester may register again whenever it is unsure (fathers#171)."""
    found = KINDS[kind]
    if kind == "command":
        resource: Optional[Dict[str, Any]] = {"kind": kind, "id": given, "alive": alive, "stop": stop}
        state = found.alive(resource, None, None)
        if state is not True:
            raise _errors.Refused(f"refusing {given}: its --alive command says "
                                  f"{'it is gone' if state is False else 'nothing (exit 0 alive, 1 gone)'}",
                                  _errors.NOT_FOUND if state is False else _errors.FAILED)
    else:
        table = _table()
        try:
            resource = found.resolve(given, found.listing(table))
        except _errors.Usage as error:
            raise _errors.Usage(f"refusing {given}: {error}", error.command) from None
        except _errors.Refused as error:
            raise _errors.Refused(f"refusing {given}: {error}", error.code) from None
    if remove:
        resource["remove"] = True
    lane = owner.get("kind") == "lane"
    entry, why = record(store, resource, [], NO_TTL if lane else ttl, "owner" if lane else keep, label, owner,
                        os.getcwd(), extra={"no_ttl": True} if lane else None)
    already = False
    if entry is None:
        from . import owner as _owner
        holder = holder_of(store, resource)
        if not (lane and holder is not None and _owner.same_lane(holder.get("owner") or {}, owner)):
            raise _errors.Refused(f"refusing {describe(resource)}: {why}", _errors.REFUSED)
        entry, already = holder, True
    if as_json:
        print(json.dumps({"v": 1, "id": entry["id"], "already": already, "resource": entry.get("resource"),
                          "owner": entry.get("owner")}, sort_keys=True))
    elif already:
        print(f"spawnfather: {entry['id']} owns {describe(resource)} for that lane already")
    else:
        announce(entry)
    return 0


def holder_of(store: Store, resource: Mapping[str, Any]) -> Optional[Entry]:
    """The live entry that holds this resource, or None."""
    wanted = key(resource)
    return next((e for e in store.entries() if e.get("state") in _entry.LIVE
                 and key(as_dict(e.get("resource"))) == wanted), None)


def own_wrapped(store: Store, kind: str, argv: List[str], ttl: float, keep: str, label: Optional[str],
                owner: Dict[str, Any], remove: bool = False, quiet: bool = False,
                clock: Callable[[], float] = time.monotonic,
                pause: Callable[[float], None] = time.sleep) -> int:
    """`own --kind KIND -- CMD`: run the command that makes the resource, then own what it made. Nothing it made
    already ran before, and only what it names or what newly appeared counts."""
    found = copy.copy(KINDS[kind])  # prepare() keeps per-command state: never on the shared instance
    found.prepare(argv)
    table = _table()
    before = found.listing(table)
    started = time.time()
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=CREATE_TIMEOUT, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        print(f"spawnfather: {shlex.join(argv)} did not finish in {CREATE_TIMEOUT:.0f} s; nothing owned",
              file=sys.stderr)
        return 124
    except OSError as error:
        print(f"spawnfather: cannot start {shlex.join(argv)}: {error}", file=sys.stderr)
        return 127
    sys.stdout.write(done.stdout)
    sys.stderr.write(done.stderr)
    sys.stdout.flush()
    if done.returncode != 0:
        print(f"spawnfather: {shlex.join(argv)} exited {done.returncode}; nothing owned", file=sys.stderr)
        return done.returncode
    named = found.named(argv, done.stdout)
    made, why = made_by(found, before, named, started, table, clock=clock, pause=pause)
    if not made:
        print(f"spawnfather: {shlex.join(argv)}: {why}; nothing owned (own one that runs: spawnfather own-resource "
              f"--kind {kind} --id ID)", file=sys.stderr)
        return 1
    after = found.listing(_table())
    code = 0
    for ident in made:
        resource = {"kind": kind, "id": ident, **(after or {}).get(ident, {})}
        if remove:
            resource["remove"] = True
        entry, why = record(store, resource, argv, ttl, keep, label, dict(owner), os.getcwd())
        if entry is None:
            print(f"spawnfather: not owning {describe(resource)}: {why}", file=sys.stderr)
            code = 1
        elif not quiet:
            announce(entry, out=sys.stderr)
    return code


def made_by(found: Kind, before: Listing, named: List[str], started: float, table: Optional[_procs.Table],
            clock: Callable[[], float] = time.monotonic,
            pause: Callable[[float], None] = time.sleep) -> Tuple[List[str], str]:
    """What the command made, or [] and why not. What it names counts once it shows as new or changed; otherwise
    exactly one new or changed item, from one listing right after it (safari-tp: within SETTLE seconds, its main
    process started after the command did). Never everything that runs, never two unnamed items at once: another
    agent's or a person's resource could be among them."""
    deadline = clock() + SETTLE
    while True:
        after = found.listing(_table())
        if after is not None:
            fresh = [i for i in after if before is not None and (i not in before or before[i] != after[i])]
            if named:
                made = [i for i in named if i in after and (before is None or i in fresh)]
                if made:
                    return made, ""
            elif before is not None:
                if found.name == "safari-tp":
                    fresh = [i for i in fresh if (_procs.started_at(str(after[i].get("start") or "")) or 0)
                             >= int(started) - 1]
                if len(fresh) == 1:
                    return fresh, ""
                if len(fresh) > 1:
                    return [], f"{len(fresh)} new {found.name} items appeared together, and it names none of them"
        if (named or found.name == "safari-tp") and clock() < deadline:
            pause(0.5)
            continue
        if before is None and not named:
            return [], f"the {found.name} listing could not be read before it ran, and it names nothing"
        return [], f"it made no new {found.name} that spawnfather can see"


def _table() -> Optional[_procs.Table]:
    try:
        return _procs.Table.read()
    except _procs.ProcError:
        return None


# Stopping

def stop(resource: Mapping[str, Any], grace: float, alive: Callable[[], Optional[bool]],
         pause: Callable[[float], None] = time.sleep, commands: Optional[List[List[str]]] = None,
         clock: Callable[[], float] = time.monotonic) -> Dict[str, Any]:
    """Run the kind's stop commands (or `commands`, already planned and guarded), then wait up to `grace` seconds
    (at least 5) for the resource to be gone. Returns {"result": stopped|gone|failed, "steps": [...], "error"};
    failed when a step failed, timed out, or the resource still runs (or cannot be told) afterwards."""
    first = alive()
    if first is False:
        return {"result": "gone", "steps": []}
    if first is None:
        return {"result": "failed", "steps": [], "error": "whether it runs cannot be told; nothing was run"}
    try:
        if commands is None:
            commands = KINDS[str(resource.get("kind"))].stop(resource, grace)
    except Refused as error:
        return {"result": "failed", "steps": [], "error": str(error)}
    except (KeyError, ValueError, NotImplementedError) as error:
        return {"result": "failed", "steps": [], "error": f"no stop command: {error}"}
    steps: List[Dict[str, Any]] = []
    for index, argv in enumerate(commands):
        if not argv:
            return {"result": "failed", "steps": steps, "error": "an empty stop command"}
        ran = run(argv, timeout=STOP_TIMEOUT + grace)
        steps.append({"argv": argv, "exit": ran.code, **({"error": ran.why()} if ran.code != 0 else {})})
        if ran.code != 0 and index == 0:
            if alive() is False:
                return {"result": "stopped", "steps": steps}
            return {"result": "failed", "steps": steps, "error": ran.why()}
    state = alive()
    failing = next((step["error"] for step in steps if step.get("error")), None)
    if failing and state is True:  # a later step failed and it still runs: no point waiting for it to go
        return {"result": "failed", "steps": steps, "error": failing}
    deadline = clock() + max(grace, 5.0)
    while state is not False and clock() < deadline:
        pause(0.5)
        state = alive()
    if state is False:
        later = [step["error"] for step in steps[1:] if step.get("error")]
        return {"result": "stopped", "steps": steps, **({"warning": "; ".join(later)} if later else {})}
    return {"result": "failed", "steps": steps,
            "error": "it still runs after its stop command" if state else "whether it still runs cannot be told"}


def ending_owner(entry: Entry, lifecycle: Optional[_reap.Lifecycle], table: Optional[_procs.Table]) -> List[int]:
    """The processes that do not count as using a resource while its owner's end reaps it: at a SessionEnd the
    session's process and everything below it, at a SubagentStop the session's process alone (its other work goes
    on). [] outside those lifecycle events."""
    owner = entry.get("owner") or {}
    pid = owner.get("pid")
    if table is None or not isinstance(pid, int) or not table.alive(pid, owner.get("pid_start")):
        return []
    if lifecycle == _reap.Lifecycle.SESSION_END:
        return [pid, *(p.pid for p in table.descendants(pid))]
    return [pid] if lifecycle == _reap.Lifecycle.SUBAGENT_STOP else []


def stopped_event(store: Store, subject: Dict[str, Any], actor: Optional[Dict[str, Any]], reason: str,
                  resource: Mapping[str, Any], outcome: Mapping[str, Any], done: bool,
                  owner: Optional[Mapping[str, Any]] = None) -> None:
    """resource.stopped or resource.stop_failed for one stop's outcome (an entry's reap and the sweep's removal of an
    unowned worktree). A lane owner's entry names it (`owner`); a stop an override let through names the override's
    reason and the guards it lifted (`override`), and the processes --stop-inside stopped first (`stopped`)."""
    from .owner import lane_key
    lane = lane_key(owner or {})
    evidence = resource.get("_evidence")
    lifted = list(getattr(evidence, "lifted", []) or [])
    override = getattr(evidence, "override", None)
    _events.write(store.root, "resource.stopped" if done else "resource.stop_failed", subject,
                  actor or _events.caller(), {"reason": reason, "result": outcome["result"],
                                              "resource": {k: v for k, v in resource.items() if not k.startswith("_")},
                                              "steps": [step_text(step) for step in outcome["steps"]],
                                              **({"owner": lane} if lane else {}),
                                              **({"override": {"reason": override, "lifted": lifted}}
                                                 if override and lifted else {}),
                                              **({"stopped": outcome["stopped"]} if outcome.get("stopped") else {}),
                                              **({"error": outcome["error"]} if outcome.get("error") else {}),
                                              **({"warning": outcome["warning"]} if outcome.get("warning") else {})},
                  ("warn" if outcome.get("warning") or (override and lifted) else None) if done else "error")


def inside_stoppable(store: Store, path: str, table: Optional[_procs.Table], cwds: Optional[Mapping[int, str]]
                     ) -> Optional[List[_procs.Proc]]:
    """What `--stop-inside` stops before removing `path`: the processes working in it (or naming it in their
    command) that no active entry holds, that are this user's and signallable (safety.signallable: no terminal, no
    agent session, nothing above one of those), and that run below no live agent session. None when that cannot
    be told (an unreadable table or working-directory listing): nothing is stopped then."""
    if table is None or cwds is None:
        return None
    from . import sweep as _sweep
    prefix = path.rstrip("/") + "/"
    owned: set[int] = set()
    for other in store.entries():
        if other.get("state") in _entry.ACTIVE and other.get("mode") != "resource":
            owned |= {p.pid for p in _reap.members(other, table)[0]}
    sessions = _sweep.agent_sessions(table)
    found: List[_procs.Proc] = []
    for proc in table.procs.values():
        cwd = cwds.get(proc.pid)
        named = any(word == path or word.startswith(prefix) for word in proc.cmd.split())
        if not (named or (cwd is not None and (cwd == path or cwd.startswith(prefix)))):
            continue
        if proc.pid in owned or _safety.below_session(proc.pid, table, sessions) \
                or _safety.untouchable_tree(proc.pid, table):
            continue
        found.append(proc)
    return _safety.signallable(found, table)


def stop_inside(store: Store, path: str, grace: float, actor: Optional[Dict[str, Any]], reason: str
                ) -> Dict[str, Any]:
    """Stop what `inside_stoppable` finds in `path` (pid, start time and command checked before each signal, reap.stop),
    each with an unowned.reaped event. {"stopped": [descriptions], "error": why} (error only when a stop failed or
    the processes cannot be told)."""
    table = _table()
    found = inside_stoppable(store, path, table, _procs.working_dirs_or_none(os.getuid()))
    if found is None:
        return {"stopped": [], "error": "which processes work inside it cannot be told; nothing was stopped"}
    stopped: List[str] = []
    for proc in found:
        outcome = _reap.stop([proc], grace)
        done = outcome["result"] in _reap.STOPPED
        _events.write(store.root, "unowned.reaped", {"type": "process", "id": str(proc.pid), "pid": proc.pid},
                      actor or _events.caller(), {"cmd": proc.cmd, "start": proc.start,
                                                  "reason": f"{reason}; working inside {path}",
                                                  "result": outcome["result"], "pids": [proc.pid]},
                      None if done else "error")
        if not done:
            return {"stopped": stopped, "error": f"pid {proc.pid} ({proc.cmd[:80]}) could not be stopped: "
                                                 f"{outcome.get('error') or outcome['result']}"}
        stopped.append(f"pid {proc.pid} ({proc.cmd[:80]})")
    return {"stopped": stopped}


def reap_target(store: Store, entry: Entry, reason: str, grace: float, actor: Optional[Dict[str, Any]],
                table: Optional[_procs.Table], *, lifecycle: Optional[_reap.Lifecycle] = None,
                evidence: Optional[Mapping[str, Any]] = None, stopping: bool = False) -> _reap.Target:
    """Target adapter for a resource entry (`reap.reap` drives dry-run, claim, teardown, end_reap and events).
    `evidence` ({bases, disposable, override}) and `stopping` (--stop-inside) apply to worktrees and dirs."""
    recorded = entry.get("resource") or {}
    entry_id = str(entry["id"])
    resource: Dict[str, Any] = {**recorded, "_exclude": ending_owner(entry, lifecycle, table)}
    folder = str(resource.get("kind")) in FOLDERS
    if folder and evidence is not None:
        from .worktrees import Evidence
        resource["_evidence"] = Evidence.of(evidence)
    kind = KINDS.get(str(resource.get("kind")))
    how = describe(resource)
    path = str(resource.get("id"))
    stops = folder and stopping

    def alive() -> Optional[bool]:
        return Snapshot(_table()).alive(resource)

    def row(result: str, why: str, **more: Any) -> Dict[str, Any]:
        return {"id": entry_id, "result": result, "reason": why, "pids": [], "how": how,
                "resource": {"kind": resource.get("kind"), "id": resource.get("id")}, **more}

    def preview() -> Dict[str, Any]:
        state = alive()
        if state is False:
            return row("gone", reason)
        if state is None:
            return row("would-fail", f"{reason}; whether it runs cannot be told")
        would: List[_procs.Proc] = []
        checked = resource
        if stops:
            current = _table()
            found = inside_stoppable(store, path, current, _procs.working_dirs_or_none(os.getuid()))
            if found is None:
                return row("would-fail", f"{reason}; which processes work inside it cannot be told")
            would = found
            checked = {**resource, "_exclude": [*as_list(resource.get("_exclude")), *(p.pid for p in found)]}
        refused = kind.check(checked) if kind is not None else None
        stopping_too = {"stops": [f"pid {p.pid} ({p.cmd[:80]})" for p in would]} if would else {}
        return row("would-fail" if refused else "would-stop", f"{reason}; {refused}" if refused else reason,
                   **stopping_too)

    def teardown(current: Entry) -> _reap.ReapOutcome:
        first: Dict[str, Any] = {"stopped": []}
        pre_error: Optional[str] = None
        if stops and alive() is True:
            current_table = _table()
            found = inside_stoppable(store, path, current_table, _procs.working_dirs_or_none(os.getuid()))
            if found is None:
                pre_error = "which processes work inside it cannot be told"
            else:
                checked = {**resource, "_exclude": [*as_list(resource.get("_exclude")), *(p.pid for p in found)]}
                pre_error = kind.check(checked) if kind is not None else None
            if pre_error is None:
                first = stop_inside(store, path, grace, actor, reason)
        if pre_error is not None:
            outcome: Dict[str, Any] = {"result": "failed", "steps": [], "error": pre_error}
        elif first.get("error"):
            outcome = {"result": "failed", "steps": [], "error": first["error"]}
        else:
            outcome = stop(resource, grace, alive)
        if first.get("stopped"):
            outcome["stopped"] = first["stopped"]
        result = outcome["result"]
        done = result in _reap.STOPPED

        def emit(finished: bool) -> None:
            stopped_event(store, subject(current), actor, reason, resource, outcome, finished, current.get("owner"))

        evidence_now = resource.get("_evidence")
        lifted = list(getattr(evidence_now, "lifted", []) or [])
        return _reap.ReapOutcome(
            result=result, how=how, pids=[],
            end={"reason": reason, "result": result, "how": how, "pids": [], "steps": outcome["steps"],
                 **({"stopped": outcome["stopped"]} if outcome.get("stopped") else {}),
                 **({"lifted": lifted} if lifted else {})},
            error={"reason": reason, "left": [], "error": outcome.get("error"), "steps": outcome["steps"]},
            reason=reason if done else f"{reason}; {outcome.get('error')}",
            emit=emit,
            extra={"resource": {"kind": resource.get("kind"), "id": resource.get("id")},
                   **({"stopped": outcome["stopped"]} if outcome.get("stopped") else {}),
                   **({"lifted": lifted} if lifted else {})},
        )

    return _reap.Target(how=how, dry_run=preview, teardown=teardown)


def reap_worktree(store: Store, given: str, reason: str, grace: float, evidence: Mapping[str, Any],
                  stopping: bool, dry_run: bool, actor: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """`reap --worktree PATH`: a requester's one-shot removal of a worktree no entry owns (Lanefather's gc), through
    the same guarded removal an owned one's stop uses, with the requester's evidence. The one result row; Refused
    when it is no linked worktree (6) or an entry owns it (3: reap that entry by its owner)."""
    from . import worktrees
    found = KINDS["worktree"].resolve(given, None)
    holder = holder_of(store, found)
    if holder is not None:
        from .owner import describe as who
        raise _errors.Refused(f"refusing {describe(found)}: entry {holder['id']} owns it for "
                              f"{who(holder.get('owner') or {})}; reap it through that owner", _errors.REFUSED)
    resource: Dict[str, Any] = {**found, "_evidence": worktrees.Evidence.of(evidence)}
    path = str(found["id"])
    how = describe(found)
    row: Dict[str, Any] = {"id": key(found), "result": "", "reason": reason, "pids": [], "how": how,
                           "resource": {"kind": "worktree", "id": path}}
    if dry_run:
        checked = resource
        would: List[_procs.Proc] = []
        if stopping:
            current = _table()
            listed = inside_stoppable(store, path, current, _procs.working_dirs_or_none(os.getuid()))
            if listed is None:
                return {**row, "result": "would-fail", "reason": f"{reason}; which processes work inside it cannot "
                                                                    "be told"}
            would = listed
            checked = {**resource, "_exclude": [p.pid for p in listed]}
        refused = KINDS["worktree"].check(checked)
        return {**row, "result": "would-fail" if refused else "would-stop",
                "reason": f"{reason}; {refused}" if refused else reason,
                **({"stops": [f"pid {p.pid} ({p.cmd[:80]})" for p in would]} if would else {})}
    first: Dict[str, Any] = {"stopped": []}
    pre_error: Optional[str] = None
    if stopping:
        current = _table()
        listed = inside_stoppable(store, path, current, _procs.working_dirs_or_none(os.getuid()))
        if listed is None:
            pre_error = "which processes work inside it cannot be told"
        else:
            checked = {**resource, "_exclude": [p.pid for p in listed]}
            pre_error = KINDS["worktree"].check(checked)
        if pre_error is None:
            first = stop_inside(store, path, grace, actor, reason)
    if pre_error is not None:
        outcome: Dict[str, Any] = {"result": "failed", "steps": [], "error": pre_error}
    elif first.get("error"):
        outcome = {"result": "failed", "steps": [], "error": first["error"]}
    else:
        outcome = stop(resource, grace, Snapshot(None).bound(resource))
    if first.get("stopped"):
        outcome["stopped"] = first["stopped"]
    done = outcome["result"] in _reap.STOPPED
    subject = {"type": "resource", "id": key(found), "kind": "worktree",
               **({"label": str(found["branch"])} if found.get("branch") else {})}
    stopped_event(store, subject, actor, reason, resource, outcome, done)
    lifted = list(resource["_evidence"].lifted)
    return {**row, "result": outcome["result"], "reason": reason if done else f"{reason}; {outcome.get('error')}",
            **({"stopped": outcome["stopped"]} if outcome.get("stopped") else {}),
            **({"lifted": lifted} if lifted else {})}


def step_text(step: Mapping[str, Any]) -> str:
    """One stop step for an event (an argv list would nest past the schema's depth): its command and exit."""
    text = f"{shlex.join(str(word) for word in as_list(step.get('argv')))}: exit {step.get('exit')}"
    return text + (f" ({step['error']})" if step.get("error") else "")


# The sweep's report

def unowned(snapshot: Snapshot, owned: Mapping[str, str]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Running resources of the reporting kinds that no entry owns, and the kinds that could not be listed."""
    found: List[Dict[str, Any]] = []
    unknown: List[str] = []
    for kind in KINDS.values():
        if not kind.reports:
            continue
        if not tool({"simulator": "xcrun", "docker": "docker",
                     "tailscale-serve": "tailscale"}.get(kind.name, kind.name)):
            continue  # not installed here: nothing of it can run
        listed = snapshot.listing(kind.name)
        if listed is None:
            unknown.append(kind.name)
            continue
        for ident, info in sorted(listed.items()):
            resource = {"kind": kind.name, "id": ident, **info}
            item = _safety.unowned_resource(resource, key(resource), owned)
            if item is not None:
                found.append(item)
    return found, unknown
