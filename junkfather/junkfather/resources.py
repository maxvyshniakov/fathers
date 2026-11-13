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

A tailscale-serve id is `PROTO:PORT` or `PROTO:PORT/PATH` (`https:6475`, `https:443/docs`, `tcp:5432`); stopping one
removes that route only. An unowned resource is only reported (by the sweep), never stopped.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, TextIO, Tuple

from . import entry as _entry
from . import events as _events
from . import procs as _procs
from . import safety as _safety
from .defaults import CREATE_TIMEOUT, RESOURCE_TIMEOUT, SETTLE, STOP_TIMEOUT
from .entry import Entry
from .state import Store, as_dict, as_list
from .text import duration

EXTRA_PATH = ("/opt/homebrew/bin", "/usr/local/bin", "/Applications/Docker.app/Contents/Resources/bin", "/usr/bin")
UDID = re.compile(r"\A[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\Z")
CONTAINER = re.compile(r"\A[0-9a-f]{64}\Z")
ROUTE = re.compile(r"\A(https|http|tcp|tls-terminated-tcp):(\d{1,5})(/\S*)?\Z")
STP_BUNDLE = "com.apple.SafariTechnologyPreview"
STP_MAIN = re.compile(r"Safari Technology Preview\.app/Contents/MacOS/Safari Technology Preview(\s|$)")

Listing = Optional[Dict[str, Dict[str, Any]]]


class Ran:
    """One command's outcome: code None when it could not run or timed out (`error` says which)."""

    def __init__(self, code: Optional[int], out: str = "", err: str = "", error: str = ""):
        self.code, self.out, self.err, self.error = code, out, err, error

    def why(self) -> str:
        return self.error or (self.err.strip() or self.out.strip())[:300] or f"exit {self.code}"


def tool(name: str) -> Optional[str]:
    """A command on PATH, else in the usual install places (launchd's PATH can be short)."""
    return shutil.which(name) or shutil.which(name, path=os.pathsep.join(EXTRA_PATH))


def run(argv: List[str], timeout: float = RESOURCE_TIMEOUT) -> Ran:
    """argv as given, never through a shell, with a timeout; no stdin."""
    found = tool(argv[0]) if "/" not in argv[0] else argv[0]
    if not found:
        return Ran(None, error=f"{argv[0]}: not found")
    try:
        done = subprocess.run([found, *argv[1:]], capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return Ran(None, error=f"{argv[0]} did not answer in {timeout:.0f} s")
    except OSError as error:
        return Ran(None, error=f"{argv[0]}: {error}")
    return Ran(done.returncode, done.stdout, done.stderr)


# Kinds

class Kind:
    name = ""
    reports = True  # whether the sweep reports unowned resources of this kind

    def listing(self, table: Optional[_procs.Table]) -> Listing:
        return None

    def alive(self, resource: Mapping[str, Any], listed: Listing, table: Optional[_procs.Table]) -> Optional[bool]:
        if listed is None:
            return None
        return str(resource.get("id")) in listed

    def resolve(self, given: str, listed: Listing) -> Tuple[Optional[Dict[str, Any]], str]:
        """The resource record for an id given on the command line, or None and why not."""
        if listed is not None and given in listed:
            return {"kind": self.name, "id": given, **listed[given]}, ""
        return None, f"no running {self.name} {given}"

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

    def resolve(self, given: str, listed: Listing) -> Tuple[Optional[Dict[str, Any]], str]:
        if not UDID.match(given):
            return None, f"not a simulator UDID: {given!r} (xcrun simctl list devices shows them)"
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

    def resolve(self, given: str, listed: Listing) -> Tuple[Optional[Dict[str, Any]], str]:
        ran = run(["docker", "inspect", "--type", "container", "--format", "{{.Id}}\t{{.Name}}", given])
        if ran.code != 0:
            return None, f"docker inspect {given}: {ran.why()}"
        full, _, name = ran.out.strip().partition("\t")
        if not CONTAINER.match(full):
            return None, f"docker inspect {given} gave no container id"
        if listed is None:
            return None, "running containers cannot be listed (docker ps failed)"
        if full not in listed:
            return None, f"container {given} is not running"
        return {"kind": self.name, "id": full, "name": name.lstrip("/") or None,
                **({"image": listed[full].get("image")} if listed else {})}, ""

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

    def resolve(self, given: str, listed: Listing) -> Tuple[Optional[Dict[str, Any]], str]:
        if not ROUTE.match(given):
            return None, f"not a serve route: {given!r} (PROTO:PORT or PROTO:PORT/PATH, like https:6475)"
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
            for path, value in as_dict(as_dict(site).get("Handlers")).items():
                handler = as_dict(value)
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
                for p in table.procs.values() if p.uid == os.getuid() and STP_MAIN.search(p.cmd)}

    def alive(self, resource: Mapping[str, Any], listed: Listing, table: Optional[_procs.Table]) -> Optional[bool]:
        if table is None:
            return None
        return table.alive(resource.get("pid"), resource.get("start"))

    def resolve(self, given: str, listed: Listing) -> Tuple[Optional[Dict[str, Any]], str]:
        if listed is not None and given in (STP_BUNDLE, "safari-tp"):
            if len(listed) != 1:
                return None, f"{len(listed)} instances run; name one by its pid"
            given = next(iter(listed))
        if not given.isdigit():
            return None, f"safari-tp takes --id PID (of its main process) or {STP_BUNDLE}"
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

    def alive(self, resource: Mapping[str, Any], listed: Listing, table: Optional[_procs.Table]) -> Optional[bool]:
        argv = resource.get("alive")
        if not isinstance(argv, list) or not argv:
            return None
        ran = run([str(word) for word in as_list(argv)])
        return True if ran.code == 0 else False if ran.code == 1 else None

    def stop(self, resource: Mapping[str, Any], grace: float) -> List[List[str]]:
        return [[str(word) for word in as_list(resource.get("stop"))]]


KINDS: Dict[str, Kind] = {k.name: k for k in (Simulator(), Docker(), TailscaleServe(), SafariTP(), Command())}
WRAPPABLE = ("simulator", "docker", "tailscale-serve", "safari-tp")


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

    def alive(self, resource: Mapping[str, Any]) -> Optional[bool]:
        kind = KINDS.get(str(resource.get("kind")))
        if kind is None:
            return None
        return kind.alive(resource, None if kind.name == "command" else self.listing(kind.name), self.table)


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
           owner: Dict[str, Any], cwd: str) -> Tuple[Optional[Entry], str]:
    """A running resource entry, or None and why not: one entry per resource."""
    entry, holder = _entry.resource_owned(store, _entry.new(store, argv, ttl, keep, label, owner, "resource", cwd),
                                          resource, lambda e: key(e.get("resource") or {}) == key(resource))
    if entry is None:
        return None, f"already owned by {holder}"
    _events.write(store.root, "resource.owned", subject(entry), _events.actor_of(entry.get("owner")),
                  {"resource": resource, "argv": argv, "cwd": cwd, "ttl": ttl, "keep": keep})
    return entry, ""


def announce(entry: Entry, out: Optional[TextIO] = None) -> None:
    from .owner import describe as who
    keep = "" if entry["keep"] == "owner" else f", kept for the {entry['keep']}"
    note = f" ({entry.get('claim_note')})" if entry.get("claim_note") else ""
    what = describe(entry.get("resource") or {})
    print(f"junkfather: {entry['id']} owns {what} for {who(entry['owner'])}{note}, ttl "
          f"{duration(entry['ttl'])}{keep}; stop it: junkfather reap --id {entry['id']}", file=out or sys.stdout,
          flush=True)


def own_resource(store: Store, kind: str, given: str, ttl: float, keep: str, label: Optional[str],
                 owner: Dict[str, Any], alive: Optional[List[str]] = None, stop: Optional[List[str]] = None,
                 remove: bool = False) -> int:
    """`own-resource`: record a resource that runs now."""
    found = KINDS[kind]
    if kind == "safari-tp":
        print("junkfather own-resource: refusing safari-tp: an instance that runs cannot be told from a person's own "
              "browser; launch it through junkfather own --kind safari-tp -- open -g -a 'Safari Technology Preview'",
              file=sys.stderr)
        return 1
    if kind == "command":
        if not alive or not stop:
            print("junkfather own-resource: --kind command needs --alive CMD and --stop CMD", file=sys.stderr)
            return 2
        resource: Optional[Dict[str, Any]] = {"kind": kind, "id": given, "alive": alive, "stop": stop}
        state = found.alive(resource, None, None)
        if state is not True:
            print(f"junkfather own-resource: refusing {given}: its --alive command says "
                  f"{'it is gone' if state is False else 'nothing (exit 0 alive, 1 gone)'}", file=sys.stderr)
            return 1
    else:
        table = _table()
        resource, why = found.resolve(given, found.listing(table))
        if resource is None:
            print(f"junkfather own-resource: refusing {given}: {why}", file=sys.stderr)
            return 1
    if remove:
        resource["remove"] = True
    entry, why = record(store, resource, [], ttl, keep, label, owner, os.getcwd())
    if entry is None:
        print(f"junkfather own-resource: refusing {describe(resource)}: {why}", file=sys.stderr)
        return 1
    announce(entry)
    return 0


def own_wrapped(store: Store, kind: str, argv: List[str], ttl: float, keep: str, label: Optional[str],
                owner: Dict[str, Any], remove: bool = False, quiet: bool = False) -> int:
    """`own --kind KIND -- CMD`: run the command that makes the resource, then own what it made. Nothing it made
    already ran before, and only what it names or what newly appeared counts."""
    found = KINDS[kind]
    table = _table()
    before = found.listing(table)
    started = time.time()
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=CREATE_TIMEOUT, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        print(f"junkfather: {shlex.join(argv)} did not finish in {CREATE_TIMEOUT:.0f} s; nothing owned",
              file=sys.stderr)
        return 124
    except OSError as error:
        print(f"junkfather: cannot start {shlex.join(argv)}: {error}", file=sys.stderr)
        return 127
    sys.stdout.write(done.stdout)
    sys.stderr.write(done.stderr)
    sys.stdout.flush()
    if done.returncode != 0:
        print(f"junkfather: {shlex.join(argv)} exited {done.returncode}; nothing owned", file=sys.stderr)
        return done.returncode
    named = found.named(argv, done.stdout)
    made, why = made_by(found, before, named, started, table)
    if not made:
        print(f"junkfather: {shlex.join(argv)}: {why}; nothing owned (own one that runs: junkfather own-resource "
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
            print(f"junkfather: not owning {describe(resource)}: {why}", file=sys.stderr)
            code = 1
        elif not quiet:
            announce(entry, out=sys.stderr)
    return code


def made_by(found: Kind, before: Listing, named: List[str], started: float, table: Optional[_procs.Table]
            ) -> Tuple[List[str], str]:
    """What the command made, or [] and why not. What it names counts once it shows as new or changed; otherwise
    exactly one new or changed item, from one listing right after it (safari-tp: within SETTLE seconds, its main
    process started after the command did). Never everything that runs, never two unnamed items at once: another
    agent's or a person's resource could be among them."""
    deadline = time.monotonic() + SETTLE
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
        if (named or found.name == "safari-tp") and time.monotonic() < deadline:
            time.sleep(0.5)
            continue
        if before is None and not named:
            return [], f"the {found.name} listing could not be read before it ran, and it names nothing"
        return [], f"it made no new {found.name} that junkfather can see"


def _table() -> Optional[_procs.Table]:
    try:
        return _procs.Table.read()
    except _procs.ProcError:
        return None


# Stopping

def stop(resource: Mapping[str, Any], grace: float, alive: Callable[[], Optional[bool]],
         pause: Callable[[float], None] = time.sleep) -> Dict[str, Any]:
    """Run the kind's stop commands, then wait up to `grace` seconds (at least 5) for the resource to be gone.
    Returns {"result": stopped|gone|failed, "steps": [...], "error"}; failed when a step failed, timed out, or the
    resource still runs (or cannot be told) afterwards."""
    first = alive()
    if first is False:
        return {"result": "gone", "steps": []}
    if first is None:
        return {"result": "failed", "steps": [], "error": "whether it runs cannot be told; nothing was run"}
    try:
        commands = KINDS[str(resource.get("kind"))].stop(resource, grace)
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
    deadline = time.monotonic() + max(grace, 5.0)
    state = alive()
    while state is not False and time.monotonic() < deadline:
        pause(0.5)
        state = alive()
    if state is False:
        later = [step["error"] for step in steps[1:] if step.get("error")]
        return {"result": "stopped", "steps": steps, **({"warning": "; ".join(later)} if later else {})}
    return {"result": "failed", "steps": steps,
            "error": "it still runs after its stop command" if state else "whether it still runs cannot be told"}


def reap(store: Store, entry: Entry, reason: str, grace: float, dry_run: bool,
         actor: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """End one running resource entry through its kind's stop command (reap.reap hands resource entries here)."""
    from . import reap as _reap
    resource = entry.get("resource") or {}
    entry_id = str(entry["id"])

    def alive() -> Optional[bool]:
        return Snapshot(_table()).alive(resource)

    if dry_run:
        state = alive()
        return {"id": entry_id, "result": "gone" if state is False else "would-stop", "reason": reason, "pids": [],
                "how": describe(resource)}
    me = _entry.begin_reap(store, entry_id, reason)
    if me is None:
        return {"id": entry_id, "result": "skipped", "reason": "another reaper has it", "pids": [],
                "how": describe(resource)}
    outcome = stop(resource, grace, alive)
    result = outcome["result"]
    done = result in _reap.STOPPED
    _entry.end_reap(store, entry_id, me, done, {"reason": reason, "result": result, "how": describe(resource),
                                                "pids": [], "steps": outcome["steps"]},
                    {"reason": reason, "left": [], "error": outcome.get("error"), "steps": outcome["steps"]})
    _events.write(store.root, "resource.stopped" if done else "resource.stop_failed", subject(entry),
                  actor or _events.caller(), {"reason": reason, "result": result, "resource": resource,
                                              "steps": [step_text(step) for step in outcome["steps"]],
                                              **({"error": outcome["error"]} if outcome.get("error") else {}),
                                              **({"warning": outcome["warning"]} if outcome.get("warning") else {})},
                  ("warn" if outcome.get("warning") else None) if done else "error")
    return {"id": entry_id, "result": result, "reason": reason if done else f"{reason}; {outcome.get('error')}",
            "pids": [], "how": describe(resource)}


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
        if kind.name != "safari-tp" and not tool({"simulator": "xcrun", "docker": "docker",
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
