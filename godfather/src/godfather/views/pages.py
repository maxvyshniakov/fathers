"""The pages one key away from the overview (fathers#85, fathers#86), and the summaries that lead to them.

A page reuses the overview's frames (layout.page()): its tool's table on the left with the row it has selected
marked as QUEUE marks its own, the selected row's details on the right as a scrolling document of label and value
rows, and an EVENTS strip under the table with that tool's events for the selected row. Links (a lease, a lane, a
process) are click targets (Canvas.target) the reducer follows. SLOTS' bottom border carries each tool's summary,
led by its page key: lanes on the left, procs on the right; a click opens its page.

Nothing here runs a command: every value comes from the snapshots the sources polled (state.Tool)."""
from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any, Callable, Optional, cast

from .. import entries as _entries
from .. import layout as _layout
from ..adapters import junkfather as _junkfather
from ..adapters import lanefather as _lanefather
from ..events import Filter
from ..layout import Rect
from ..text import clip, dashboard_text, minutes, span, text_width, tilde
from ..tui import Canvas, safe_link
from ..widgets import Panel, Part, Spec, Table
from . import common as _common
from . import details as _details
from . import events as _events
from .common import Model

Fact = tuple[str, list[Part]]
Link = Optional[tuple[Any, ...]]  # a document row's target, or none
PAGE_KEYS = {"lanes": "2", "processes": "3"}
TITLES = {"lanes": "LANES", "processes": "PROCESSES"}
TOOL_NAMES = {"lanes": "Lanefather", "processes": "Junkfather"}


# The summaries in SLOTS' bottom border (SPEC 13)

def lanes_summary(tool: Any, marks: _common.Marks) -> list[tuple[str, list[Part]]]:
    """The lanes summary as (what it is, its parts): the count first, then what a narrow border drops in order."""
    found = _lanefather.summary(tool.snapshot)
    parts: list[tuple[str, list[Part]]] = [("count", [("2", "key"), (" lanes ", "dim"),
                                                      (f"{found['active']} active", "text")])]
    if found["planned"]:
        parts.append(("planned", [(" · ", "dim"), (f"{found['planned']} planned", "text")]))
    if found["reviews"]:
        parts.append(("reviews", [(" · ", "dim"),
                                  (f"{found['reviews']} review{'s' if found['reviews'] != 1 else ''}", "text")]))
    if found["red"]:
        parts.append(("red", [(" · ", "dim"), (marks.health["hot"] + " gate red", "bad")]))
    return parts


DROPS = ("planned", "reviews", "unowned", "expired")  # what a narrow border gives way, in order (SPEC 13)


def draw_summaries(canvas: Canvas, rect: Rect, tools: dict[str, Any],
                   summaries: dict[str, Callable[[Any, _common.Marks], list[tuple[str, list[Part]]]]]) -> None:
    """Each present, readable tool's summary in the bottom border of `rect` (SLOTS): lanes on the left, procs on the
    right, the parts DROPS names going first where they do not fit; a stale one dimmed; each a click target that
    opens its page."""
    marks = _common.marks_of(canvas)
    found: dict[str, list[tuple[str, list[Part]]]] = {}
    for page, build in summaries.items():
        tool = tools.get("lanefather" if page == "lanes" else "junkfather")
        if tool is not None and tool.readable:
            found[page] = build(tool, marks)
    if not found:
        return
    room = rect.width - 4 - 2  # the corners and a rule each side, and two cells between the two

    def width(page: str, dropped: tuple[str, ...]) -> int:
        return sum(text_width(text) for name, parts in found.get(page, []) if name not in dropped
                   for text, _ in parts) + 2 * bool(found.get(page))

    dropped: tuple[str, ...] = ()
    for step in range(len(DROPS) + 1):
        dropped = DROPS[:step]
        if width("lanes", dropped) + width("processes", dropped) <= room:
            break
    y = rect.y + rect.height - 1
    for page, parts in found.items():
        tool = tools["lanefather" if page == "lanes" else "junkfather"]
        shown: list[Part] = [(" ", "text")] + [(text, "dim" if tool.state == "stale" else style)
                                               for name, items in parts if name not in dropped
                                               for text, style in items] + [(" ", "text")]
        size = width(page, dropped)
        start = rect.x + 2 if page == "lanes" else rect.x + rect.width - 2 - size
        if size > rect.width - 4:
            continue
        canvas.segments(y, start, shown, size)
        canvas.target(y, start, start + size, (page,))


# The page frame

def stale_note(tool: Any) -> str:
    since = tool.stale_since
    return f"stale since {since.astimezone().strftime('%H:%M')}" if isinstance(since, datetime.datetime) else ""


def draw_notice(canvas: Canvas, page: str, lines: list[str]) -> None:
    """A page that cannot be drawn: its tool is unsupported, or has not answered yet."""
    rect = Rect(0, 0, min(canvas.height - 1, len(lines) + 4), canvas.width)
    if rect.height < 3 or rect.width < 12:
        canvas.put(0, 1, lines[0], "bad", max(0, canvas.width - 2))
        return
    panel = Panel(canvas, rect, PAGE_TABLE[page]).frame(TITLES[page])
    canvas.tables[PAGE_TABLE[page]] = dict(x=rect.x, y=rect.y, width=rect.width, height=rect.height,
                                           top=panel.inner.y, visible=0, count=0, offset=0, base=None, row_items=None)
    for row, line in enumerate(lines[:panel.inner.height]):
        panel.put(row, line, "text" if row == 0 else "dim")


PAGE_TABLE = {"lanes": "lanes", "processes": "procs"}
MARK = 4  # a row's state mark and the gap after it


def draw_table(canvas: Canvas, rect: Rect, name: str, title: str, spec: list[Spec], rows: list[dict[str, Any]],
               selected: Optional[int], values: Callable[[dict[str, Any]], dict[str, Any]],
               heading: Callable[[dict[str, Any]], Part], note: str = "") -> None:
    """A page's table: its columns in the top border, a heading row for each unselectable row (a project, a
    separator), the selected row marked as QUEUE's is; clicks reach rows through row_items."""
    panel = Panel(canvas, rect, name)
    mark = spec[0][0] == "state"  # the state mark first, as QUEUE's, before the shared columns
    table = Table.fit(panel.inner.width - MARK * mark, spec[mark:]).shifted(MARK * mark)
    table.header(panel, title)
    if note and rect.width > 40:
        canvas.right(rect.y, rect.x + rect.width - 2, f" {note} ", "dim")
    row_items: list[Optional[int]] = [index if row.get("key") is not None else None for index, row in enumerate(rows)]
    offset, visible = panel.viewport(len(rows), selected, row_items=row_items)
    if not rows:
        panel.put(0, "Nothing to show", "dim")
    for line, position in panel.rows(offset, visible, len(rows)):
        row = rows[position]
        if row.get("key") is None:
            text, style = heading(row)
            canvas.put(line, panel.inner.x, text, style, panel.inner.width)
            continue
        shown = values(row)
        if mark:
            canvas.segments(line, panel.inner.x, [shown.pop("state")], MARK - 1)
        table.row(canvas, line, panel.inner.x, shown)
        if position == selected:
            panel.mark(line, "text")


def draw_strip(canvas: Canvas, rect: Rect, model: Optional[Model], source: str, tag: str,
               matches: Callable[[dict[str, Any]], bool], moment: Optional[datetime.datetime]) -> None:
    """The EVENTS strip under a page's table: its tool's log, only the rows `matches` takes (the selected row's)."""
    rows = [row for row in cast(list[dict[str, Any]], (model or {}).get("events") or [])
            if row.get("source") == source and matches(row)]
    strip: dict[str, Any] = {"events": rows, "sources": [(source, tag)]}
    _events.draw_events(canvas, rect, strip, moment, Filter(source))


def long(seconds: float) -> str:
    """An age to the minute, in days past one (`9d`, `2d03h`)."""
    return minutes(seconds) if seconds < 86400 else span(seconds).removesuffix("00h")


def age(moment: Optional[datetime.datetime], since: Any) -> str:
    if moment is None or not isinstance(since, datetime.datetime):
        return "—"
    return long((moment - since).total_seconds())


def file_url(path: Any) -> str:
    if not isinstance(path, str) or not path.startswith("/"):
        return ""
    url = Path(path).as_uri()
    return url if safe_link(url) else ""


def path_part(path: Any, width: int = 0) -> Part:
    """A path as a muted folder or file link, `~` for home."""
    text = tilde(dashboard_text(path))
    url = file_url(path)
    return (clip(text, width) if width else text, "url:" + url if url else "dim")


# The Lanes page (fathers#85)

LANE_COLUMNS: list[Spec] = [("state", "", 2, False, 0), ("lane", "lane", 0, False, 0), ("ticket", "ticket", 6, True, 3),
                            ("stage", "stage", 12, False, 2), ("receipt", "receipt", 13, False, 4),
                            ("review", "review", 14, False, 5), ("claims", "claims", 6, True, 6),
                            ("age", "age", 6, True, 1)]


def lane_values(row: dict[str, Any], marks: _common.Marks, moment: Optional[datetime.datetime]) -> dict[str, Any]:
    ticket = row["ticket"].get("number")
    receipt = row["receipt"]["status"]
    review = row["review"]
    return {"state": (marks.rows.get(row["status"], "·"), "text"), "lane": (dashboard_text(row["id"]), "text"),
            "ticket": (f"#{ticket}" if ticket else "", "dim"), "stage": (_lanefather.stage(row), "text"),
            "receipt": (_lanefather.receipt_text(row),
                        "bad" if receipt in ("red", "failed") else "ok" if receipt in ("green", "passed") else "dim"),
            "review": (_lanefather.review_text(row), "warn" if review["open"] else "dim"),
            "claims": (str(len(row["claims"])), "dim"), "age": (age(moment, row.get("created")), "dim")}


def project_heading(row: dict[str, Any]) -> Part:
    if row.get("error"):
        return f"{row['project']} · {dashboard_text(row['error'])}", "dim"
    of = f"{row['active']} of {row['cap']} lanes" if row.get("cap") is not None else \
        f"{row['count']} lane{'s' if row['count'] != 1 else ''}"
    return f"{row['project']} · {of}", "bold"


def lane_facts(lane: dict[str, Any], model: Optional[Model], extra: list[tuple[str, list[Part], Link]]) \
        -> list[tuple[str, list[Part], Link]]:
    """LANE DETAILS: project, ticket, branch and base, worktree, claims, after, receipt, review, the latest log, and
    the leases that run for it, each a link back to the overview."""
    facts: list[tuple[str, list[Part], Link]] = [("lane", [(dashboard_text(lane["id"]), "bold")], None),
                                                 ("project", [(dashboard_text(lane["project"]), "text")], None),
                                                 ("stage", [(_lanefather.stage(lane), "text")], None)]
    ticket = lane["ticket"]
    if ticket.get("number"):
        url = ticket.get("url") or ""
        facts.append(("ticket", [(f"#{ticket['number']}", "url:" + url if url.startswith("https://") and
                                  safe_link(url) else "text")], None))
    for name in ("branch", "base"):
        facts.append((name, [(dashboard_text(lane.get(name) or "—"), "text")], None))
    if lane.get("worktree"):
        facts.append(("worktree", [path_part(lane["worktree"])], None))
    facts.append(("claims", [(", ".join(dashboard_text(claim) for claim in lane["claims"]) or "—", "dim")], None))
    facts.append(("after", [(", ".join(dashboard_text(name) for name in lane["after"]) or "—", "dim")], None))
    facts.append(("receipt", [(_lanefather.receipt_text(lane), "text")], None))
    review = lane["review"]
    facts.append(("review", [(review["status"] + (f" · {review['open']} open of {review['findings']}"
                                                   if review["findings"] else ""),
                              "warn" if review["open"] else "text")], None))
    log = _lanefather.latest_log(lane)
    if log is not None:
        facts.append(("log", [(log[0] + " · ", "dim"), path_part(log[1])], None))
    facts.append(("", [], None))
    items = cast(list[dict[str, Any]], (model or {}).get("holders", []) + (model or {}).get("waiters", []))
    ids = _entries.entry_ids(items)
    leases = _lanefather.leases_of(lane, items)
    if not leases:
        facts.append(("leases", [("none", "dim")], None))
    for number, index in enumerate(leases):
        item = items[index]
        state = "running" if item["kind"] != "waiting" else "waiting"
        facts.append(("leases" if number == 0 else "",
                      [(dashboard_text(item["label"] or item["pid"]), "accent"), (f" · {state}", "dim")],
                      ("overview", ids[index])))
    return facts + extra


def draw_lanes(canvas: Canvas, tool: Any, selected: Optional[int], model: Optional[Model],
               moment: Optional[datetime.datetime], extra: Callable[[dict[str, Any]], list[Any]] = lambda _: []) -> None:
    """The Lanes page: LANES grouped by project, LANE DETAILS for the selected lane, its Lanefather events."""
    if tool.state == "unsupported":
        draw_notice(canvas, "lanes", [str(tool.problem), "The Lanes page needs a Lanefather with `status --json`."])
        return
    if tool.snapshot is None:
        draw_notice(canvas, "lanes", [f"Reading Lanefather… {tool.problem or ''}".strip()])
        return
    rows = _lanefather.page_rows(tool.snapshot)
    screen = _layout.page(canvas.width, canvas.height, max(1, len(rows)), "lanes")
    canvas.order = screen.order
    marks = _common.marks_of(canvas)
    lane = rows[selected] if selected is not None and 0 <= selected < len(rows) else None
    for name, rect in screen.panels.items():
        if name == "lanes":
            draw_table(canvas, rect, "lanes", "LANES", LANE_COLUMNS, rows, selected,
                       lambda row: lane_values(row, marks, moment), project_heading, stale_note(tool))
        elif name == "details":
            if lane is None:
                _details.draw_facts(canvas, rect, "LANE DETAILS", [("", [("No lanes", "dim")], None)])
            else:
                _details.draw_facts(canvas, rect, "LANE DETAILS", lane_facts(lane, model, extra(lane)))
        elif name == "events":
            draw_strip(canvas, rect, model, "lanefather", "lane",
                       lambda row: lane is not None and lane_event_of(row, lane), moment)
    if screen.arrangement == "small":
        canvas.put(0, 1, "Enlarge the terminal for the Lanes page", "dim", max(0, canvas.width - 2))
    if tool.state == "stale":
        for row in canvas.cells[:canvas.height - 1]:
            for cell in row:
                cell[1] = "dim"


def lane_event_of(row: dict[str, Any], lane: dict[str, Any]) -> bool:
    """A Lanefather event is the lane's when its subject is the lane's id in the lane's project: by the state
    directory it names, else by the project's name; never by the id alone where the event names another project."""
    subject: dict[str, Any] = row.get("subject") or {}
    if subject.get("type") != "lane" or subject.get("id") != lane["id"]:
        return False
    if subject.get("state_dir"):
        return _lanefather.same_dir(subject["state_dir"], lane["state_dir"])
    return subject.get("project") in (None, lane["project"])


# The Processes page (fathers#86)

def procs_summary(tool: Any, marks: _common.Marks) -> list[tuple[str, list[Part]]]:
    """The procs summary: owned entries running, then orphan, expired and unowned, each only when not zero."""
    found = _junkfather.summary(tool.snapshot)
    parts: list[tuple[str, list[Part]]] = [("count", [("3", "key"), (" procs ", "dim"), (str(found["count"]), "text")])]
    for name, style in (("orphan", "warn"), ("expired", "warn"), ("unowned", "text")):
        if found[name]:
            parts.append((name, [(" · ", "dim"), (f"{found[name]} {name}", style)]))
    return parts


PROCESS_COLUMNS: list[Spec] = [("state", "", 2, False, 0), ("label", "label", 0, False, 0), ("id", "id", 8, False, 4),
                               ("owner", "owner", 16, False, 3), ("lane", "lane", 14, False, 5),
                               ("age", "age", 6, True, 2), ("ttl", "ttl left", 8, True, 1),
                               ("pid", "pid", 6, True, 6), ("processes", "processes", 14, False, 7)]


def process_values(row: dict[str, Any], marks: _common.Marks, elapsed: float) -> dict[str, Any]:
    """A PROCESSES row: an owned entry, an unowned process or an unowned resource."""
    label = (dashboard_text(_junkfather.label_of(row)), "text")
    if row["kind"] == "unowned":
        return {"state": (marks.rows["unowned"], "text"), "id": ("", "dim"), "label": label,
                "owner": (dashboard_text(row["why"]), "warn"), "lane": ("", "dim"),
                "age": (minutes(row["age"] + elapsed) if row.get("age") is not None else "—", "dim"),
                "ttl": ("", "dim"), "pid": (str(row["pid"]), "dim"), "processes": ("", "dim")}
    if row["kind"] == "resource":
        return {"state": (marks.rows["resource"], "text"), "id": ("", "dim"), "label": label,
                "owner": (dashboard_text(row["why"]), "warn"), "lane": ("", "dim"),
                "age": (long(row["age"]) if row.get("age") is not None else "—", "dim"), "ttl": ("", "dim"),
                "pid": ("", "dim"), "processes": (resource_text(row["resource_kind"], row["id"]), "dim")}
    resource = row.get("resource")
    count = row.get("processes")
    return {"state": (marks.rows.get(row["state"], "·"), "text"), "id": (dashboard_text(row["id"]), "dim"),
            "label": label, "owner": (dashboard_text(row["owner"]), "dim" if row.get("owner_alive") else "warn"),
            "lane": (dashboard_text(row.get("lane") or ""), "dim"), "age": (minutes(row["age"] + elapsed), "dim"),
            "ttl": (_junkfather.ttl_text(row, elapsed), "warn" if row["state"] == "expired" else "text"),
            "pid": (str(row["pid"] or ""), "dim"),
            "processes": (resource_text(str(resource.get("kind")), str(resource.get("id"))) if resource
                          else str(count) if count is not None else "", "dim")}


def resource_text(kind: str, ident: str) -> str:
    """A resource as the page names it: its kind and its id cut short (`simulator 1A2B…`)."""
    return f"{kind} {_junkfather.short_id(ident, 9)}"


def separator(row: dict[str, Any]) -> Part:
    return ("unowned · the last sweep's", "dim") if row.get("kind") == "separator" else ("", "dim")


def process_facts(row: dict[str, Any], model: Optional[Model], lanes: Optional[dict[str, Any]],
                  elapsed: float) -> list[tuple[str, list[Part], Link]]:
    """PROCESS DETAILS: an owned entry's command, directory, log, owner and whether it lives, keep, mode, its
    resource, its TTL, and its lease and lane as links; an unowned process's command and why; an unowned resource's
    kind, id, name and why (a worktree's branch, repository, age and size)."""
    facts: list[tuple[str, list[Part], Link]] = [("label", [(dashboard_text(_junkfather.label_of(row)), "bold")],
                                                  None)]
    if row["kind"] == "resource":
        facts += [("kind", [(row["resource_kind"], "text")], None), ("id", [(dashboard_text(row["id"]), "text")], None)]
        if row.get("name"):
            facts.append(("name", [(dashboard_text(row["name"]), "text")], None))
        facts.append(("why", [(dashboard_text(row["why"]), "warn")], None))
        for name in ("branch", "repo"):
            if row.get(name):
                facts.append((name, [(dashboard_text(row[name]), "text")], None))
        if row.get("age") is not None:
            facts.append(("idle", [(long(row["age"]), "text")], None))
        if row.get("size") is not None:
            facts.append(("size", [(f"{row['size'] / 1e9:.1f} GB", "text")], None))
        return facts
    if row["kind"] == "unowned":
        facts += [("command", [(dashboard_text(row["cmd"]), "dim")], None), ("pid", [(str(row["pid"]), "text")], None),
                  ("why", [(dashboard_text(row["why"]), "warn")], None)]
        if row.get("age") is not None:
            facts.append(("age", [(minutes(row["age"] + elapsed), "text")], None))
        for name in ("cwd", "root"):
            if row.get(name):
                facts.append(("directory" if name == "cwd" else "root", [path_part(row[name])], None))
        if row.get("alive") is False:
            facts.append(("alive", [("gone since the sweep", "dim")], None))
        return facts
    facts.append(("id", [(dashboard_text(row["id"]), "text")], None))
    facts.append(("state", [(row["state"] + (f" · {row['phase']}" if row["phase"] != "running" else ""),
                             "warn" if row["state"] != "running" else "text")], None))
    facts.append(("ttl left", [(_junkfather.ttl_text(row, elapsed), "text")], None))
    resource = row.get("resource")
    if resource:
        facts += [("resource", [(f"{resource.get('kind')} {dashboard_text(resource.get('id'))}", "text")], None)]
        if resource.get("name"):
            facts.append(("name", [(dashboard_text(resource["name"]), "text")], None))
    if row.get("argv"):
        facts.append(("command", [(dashboard_text(" ".join(row["argv"])), "dim")], None))
    if row.get("cwd"):
        facts.append(("directory", [path_part(row["cwd"])], None))
    if row.get("log"):
        facts.append(("log", [path_part(row["log"])], None))
    alive = row.get("owner_alive")
    facts.append(("owner", [(dashboard_text(row["owner"]), "text"),
                            (" · lives" if alive else " · gone" if alive is False else "", "warn" if alive is False
                             else "dim")], None))
    for name in ("keep", "mode"):
        if row.get(name):
            facts.append((name, [(dashboard_text(row[name]), "text")], None))
    if row.get("pid"):
        facts.append(("pid", [(str(row["pid"]) + (f" · {row['processes']} processes" if row.get("processes")
                                                  else ""), "text")], None))
    facts.append(("", [], None))
    items = cast(list[dict[str, Any]], (model or {}).get("holders", []) + (model or {}).get("waiters", []))
    lease = _junkfather.lease_of(row, items)
    if lease is not None:
        facts.append(("lease", [(dashboard_text(items[lease]["label"] or items[lease]["pid"]), "accent")],
                      ("overview", _entries.entry_ids(items)[lease])))
    lane = _junkfather.lane_of(row, lanes)
    if lane is not None:
        facts.append(("lane", [(dashboard_text(f"{lane['id']} · {lane['project']}"), "accent")],
                      ("lanes", lane["key"])))
    elif row.get("lane"):
        facts.append(("lane", [(dashboard_text(row["lane"]), "dim")], None))
    return facts


def process_links(entries: list[dict[str, Any]], elapsed: float, label: str = "processes") \
        -> list[tuple[str, list[Part], Link]]:
    """`processes` rows: each owned entry's label, state and TTL left, a link to the Processes page; `none owned`."""
    if not entries:
        return [(label, [("none owned", "dim")], None)]
    return [(label if number == 0 else "",
             [(dashboard_text(_junkfather.label_of(entry)), "accent"),
              (f" · {entry['state']} · {_junkfather.ttl_text(entry, elapsed)}", "dim")], ("processes", entry["key"]))
            for number, entry in enumerate(entries)]


def draw_processes(canvas: Canvas, tool: Any, selected: Optional[int], model: Optional[Model],
                   lanes: Optional[dict[str, Any]], moment: Optional[datetime.datetime]) -> None:
    """The Processes page: owned entries, a separator, the last sweep's unowned processes and resources; PROCESS
    DETAILS for the selection; its Junkfather events."""
    if tool.state == "unsupported":
        draw_notice(canvas, "processes", [str(tool.problem),
                                          "The Processes page needs a Junkfather with `status --json`."])
        return
    if tool.snapshot is None:
        draw_notice(canvas, "processes", [f"Reading Junkfather… {tool.problem or ''}".strip()])
        return
    rows = _junkfather.page_rows(tool.snapshot)
    elapsed = _junkfather.elapsed(tool.snapshot, moment)
    screen = _layout.page(canvas.width, canvas.height, max(1, len(rows)), "procs")
    canvas.order = screen.order
    marks = _common.marks_of(canvas)
    row = rows[selected] if selected is not None and 0 <= selected < len(rows) else None
    for name, rect in screen.panels.items():
        if name == "procs":
            draw_table(canvas, rect, "procs", "PROCESSES", PROCESS_COLUMNS, rows, selected,
                       lambda found: process_values(found, marks, elapsed), separator, stale_note(tool))
        elif name == "details":
            nothing: list[tuple[str, list[Part], Link]] = [("", [("Nothing owned or reported", "dim")], None)]
            facts = process_facts(row, model, lanes, elapsed) if row else nothing
            _details.draw_facts(canvas, rect, "PROCESS DETAILS", facts)
        elif name == "events":
            draw_strip(canvas, rect, model, "junkfather", "junk", lambda event: row is not None and
                       process_event_of(event, row), moment)
    if screen.arrangement == "small":
        canvas.put(0, 1, "Enlarge the terminal for the Processes page", "dim", max(0, canvas.width - 2))
    if tool.state == "stale":
        for line in canvas.cells[:canvas.height - 1]:
            for cell in line:
                cell[1] = "dim"


def process_event_of(event: dict[str, Any], row: dict[str, Any]) -> bool:
    """A Junkfather event is the row's when its subject is the entry, the unowned pid or the resource."""
    subject: dict[str, Any] = event.get("subject") or {}
    if row["kind"] == "entry":
        return subject.get("type") == "entry" and subject.get("id") == row["id"]
    if row["kind"] == "unowned":
        return subject.get("pid") == row["pid"] or subject.get("id") == str(row["pid"])
    return subject.get("type") == "resource" and str(subject.get("id")) in (row["id"], f"{row['resource_kind']}:"
                                                                           f"{row['id']}")
