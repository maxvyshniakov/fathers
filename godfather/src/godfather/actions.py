"""Row actions (fathers#88, SPEC Decisions: actions table): which actions a row of QUEUE, PROCESSES or LANES offers,
and the exact command each runs. Every one is the owner tool's CLI, or for opening a ticket, a worktree or a log the
platform's opener or the pager; nothing here runs anything (app.py and sources.act() do).

    QUEUE      one of your requests          Reprioritize to interactive, normal or batch   slotfather reprioritize
               a running yieldable lease of a lower class than a waiter   Ask to yield     slotfather ask-yield
    PROCESSES  an orphan or expired entry    Reap (confirmed)                               junkfather reap --id
               an unowned process            Reap (confirmed)                               junkfather reap --pid --force
               a running owned entry         Extend TTL by 1 h, 4 h or 1 d                  junkfather extend ID --ttl
    LANES      a lane with a ticket          Open the ticket                                the platform opener
               a lane with a log             Open the latest log                            $PAGER (less -R +G)
               a lane with a worktree        Open the worktree                              the platform opener
               an active lane                Park, with a reason (confirmed)                lanefather park ID --reason

An owner tool's action is offered only where its capabilities list it in `actions`; land, promote, done, sync,
review and sweep never are, whatever a tool advertises. A live owned entry is never reaped from here."""
from __future__ import annotations

import os
import shlex
import sys
from dataclasses import dataclass
from typing import Any, Optional, cast

from .adapters import junkfather as _junkfather
from .adapters import lanefather as _lanefather

NEVER = frozenset({"land", "promote", "done", "sync", "review", "sweep"})
PRIORITIES = ("interactive", "normal", "batch")
RANK = {"interactive": 0, "normal": 1, "batch": 2}
EXTEND = (("1 h", "1h"), ("4 h", "4h"), ("1 d", "1d"))
PAGER = "less -R +G"


@dataclass(frozen=True)
class Action:
    """One action a row offers. `tool` runs `argv` after its executable (`open`: argv is the whole command, `pager`:
    the file to page); `confirm` asks first; `reason` asks for a reason first (park), which goes after `--reason`;
    `target` is what the confirmation names; `identity` what must still match in a fresh snapshot: (page, the row's
    key, and for a request its pid and start)."""
    tool: str
    name: str
    argv: tuple[str, ...]
    kind: str = "run"  # run · open · pager
    confirm: bool = False
    reason: bool = False
    target: tuple[tuple[str, str], ...] = ()
    identity: tuple[Any, ...] = ()

    def command(self, reason: str = "") -> tuple[str, ...]:
        """The exact argument vector, its executable named as the user types it."""
        argv = (*self.argv, reason) if self.reason else self.argv
        if self.kind == "open":
            return argv
        if self.kind == "pager":
            return (*pager(), *argv)
        return (self.tool, *argv)

    def shown(self, reason: str = "") -> str:
        return shlex.join(self.command(reason))


def offered(advertised: Any) -> frozenset[str]:
    """What a tool's capabilities advertise, less what Godfather never offers."""
    if not isinstance(advertised, (list, tuple, set, frozenset)):
        return frozenset()
    names = {str(name) for name in cast(list[Any], advertised)}
    return frozenset(names - NEVER)


def pager() -> tuple[str, ...]:
    return tuple(shlex.split(os.environ.get("PAGER") or PAGER)) or tuple(shlex.split(PAGER))


def opener() -> str:
    return "open" if sys.platform == "darwin" else "xdg-open"


def class_of(item: dict[str, Any]) -> str:
    found = str(item.get("served") or item.get("priority") or "normal")
    return found if found in RANK else "normal"


def for_request(item: dict[str, Any], waiters: list[dict[str, Any]], advertised: Any) -> list[Action]:
    """QUEUE: reprioritize a request to another class; ask a running yieldable lease of a lower class than a waiter
    to yield. Both need the holder's process start (status --json `start`), which the tool checks again."""
    start = item.get("start")
    pid = item.get("pid")
    if not isinstance(start, str) or not start or not isinstance(pid, int):
        return []
    tools = offered(advertised)
    label = str(item.get("label") or pid)
    target = (("request", label), ("pid", str(pid)), ("started", start))
    identity = ("overview", item.get("key"), pid, start)
    found: list[Action] = []
    if "reprioritize" in tools:
        current = str(item.get("priority") or "normal")
        found += [Action("slotfather", f"Reprioritize to {name}", ("reprioritize", str(pid), name, "--start", start),
                         target=target, identity=identity) for name in PRIORITIES if name != current]
    if "ask-yield" in tools and item.get("kind") == "held" and item.get("yieldable") and not item.get("asked"):
        mine = RANK[class_of(item)]
        if any(RANK[class_of(waiter)] < mine for waiter in waiters):
            found.append(Action("slotfather", "Ask to yield", ("ask-yield", str(pid), "--start", start),
                                target=target, identity=identity))
    return found


def for_process(row: dict[str, Any], advertised: Any) -> list[Action]:
    """PROCESSES: reap an orphan or expired entry, or an unowned process the last sweep reported (confirmed); extend
    a running entry's TTL. A running entry whose owner lives is never reaped from here."""
    tools = offered(advertised)
    label = _junkfather.label_of(row)
    if row.get("kind") == "unowned":
        if "reap" not in tools:
            return []
        target = (("process", label), ("pid", str(row["pid"])), ("why", str(row.get("why") or "")))
        return [Action("junkfather", "Reap", ("reap", "--pid", str(row["pid"]), "--force", "--reason",
                                              "godfather: unowned"), confirm=True, target=target,
                       identity=("processes", row["key"]))]
    if row.get("kind") != "entry":
        return []
    target = (("entry", label), ("id", str(row["id"])), ("pid", str(row.get("pid") or "—")),
              ("owner", str(row.get("owner") or "?")))
    identity = ("processes", row["key"])
    if row["state"] in ("orphan", "expired"):
        return [Action("junkfather", "Reap", ("reap", "--id", str(row["id"]), "--reason",
                                              f"godfather: {row['state']}"), confirm=True, target=target,
                       identity=identity)] if "reap" in tools else []
    if "extend" not in tools:
        return []
    return [Action("junkfather", f"Extend TTL by {words}", ("extend", str(row["id"]), "--ttl", ttl), target=target,
                   identity=identity) for words, ttl in EXTEND]


def for_lane(lane: dict[str, Any], advertised: Any) -> list[Action]:
    """LANES: open its ticket, latest log or worktree (no owner command); park an active lane, with a reason
    (confirmed), where Lanefather advertises park."""
    found: list[Action] = []
    identity = ("lanes", lane["key"])
    target = (("lane", str(lane["id"])), ("project", str(lane["project"])))
    url = lane["ticket"].get("url")
    if isinstance(url, str) and url.startswith("https://"):
        found.append(Action("", "Open the ticket", (opener(), url), kind="open", target=target, identity=identity))
    log = _lanefather.latest_log(lane)
    if log is not None and log[1].startswith("/"):
        found.append(Action("", "Open the latest log", (log[1],), kind="pager", target=target, identity=identity))
    if isinstance(lane.get("worktree"), str) and str(lane["worktree"]).startswith("/"):
        found.append(Action("", "Open the worktree", (opener(), str(lane["worktree"])), kind="open", target=target,
                            identity=identity))
    if "park" in offered(advertised) and lane.get("status") == "active":
        found.append(Action("lanefather", "Park", ("park", str(lane["id"]), "--reason"), confirm=True, reason=True,
                            target=target, identity=identity))
    return found


def still(action: Action, snapshot: Optional[dict[str, Any]]) -> bool:
    """Whether the row an action was offered for is still there with the same identity in a fresh snapshot: a request
    by its pid and start, an entry by its id, an unowned process by its pid and start, a lane by its key."""
    page = action.identity[0] if action.identity else ""
    if page == "overview":
        items = list((snapshot or {}).get("holders") or []) + list((snapshot or {}).get("waiters") or [])
        _, key, pid, start = action.identity
        return any(item.get("key") == key and item.get("pid") == pid and item.get("start") == start
                   for item in items)
    if page == "processes":
        return _junkfather.find(snapshot, action.identity[1]) is not None
    if page == "lanes":
        return _lanefather.find(snapshot, action.identity[1]) is not None
    return False
