"""The pages one key away from the overview (fathers#85, fathers#86), and the summaries that lead to them.

A page reuses the overview's frames (layout.page()): its tool's table on the left with the row it has selected
marked as QUEUE marks its own, the selected row's details on the right as a scrolling document of label and value
rows, and an EVENTS strip under the table with that tool's events for the selected row. Links (a lease, a lane, a
process) are click targets (Canvas.target) the reducer follows. SLOTS' bottom border carries each tool's summary,
led by its page key: lanes on the left, spawns on the right; a click opens its page.

Nothing here runs a command: every value comes from the snapshots the sources polled (state.Tool)."""
from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any, Callable, Optional, Sequence, TypeVar, cast

from .. import actions as _actions
from .. import entries as _entries
from .. import layout as _layout
from ..adapters import lanefather as _lanefather
from ..adapters import spawnfather as _spawnfather
from ..events import Filter
from ..layout import Rect
from ..model import Entry, Lane, LaneRow, LaneTableRow, ProcessRow, ProcessTableRow
from ..text import clip, dashboard_text, minutes, span, text_width, tilde
from ..tui import Canvas, safe_link
from ..widgets import Panel, Part, Spec, Table
from . import common as _common
from . import details as _details
from . import events as _events
from .common import Model

Fact = tuple[str, list[Part]]
Link = Optional[tuple[Any, ...]]  # a document row's target, or none
PAGE_KEYS = {"lanes": "2", "spawns": "3"}
TITLES = {"lanes": "LANES", "spawns": "SPAWNS"}
TOOL_NAMES = {"lanes": "Lanefather", "spawns": "Spawnfather"}

# A page table's row (model.py): LANES' lanes under their project headings, SPAWNS' entries, unowned processes
# and resources around their separator
Row = TypeVar("Row", LaneTableRow, ProcessTableRow)


# The top header bar: app name and clickable tool tabs (row 0)

TAB_META: tuple[tuple[str, str, str, Optional[str]], ...] = (
    ("overview", "1", "Overview", None),
    ("lanes", "2", "Lanes", "lanefather"),
    ("spawns", "3", "Spawns", "spawnfather"),
)
HEADER_DROPS = ("reviews", "unowned", "expired", "orphan", "red", "active")
TAB_GAP = 3


def lanes_badge(tool: Any, marks: _common.Marks) -> list[tuple[str, list[Part]]]:
    """The Lanes tab badge parts: active count, open reviews, red gate — each only when non-zero."""
    found = _lanefather.summary(tool.snapshot)
    parts: list[tuple[str, list[Part]]] = []
    if found["active"]:
        parts.append(("active", [(str(found["active"]), "text")]))
    if found["reviews"]:
        parts.append(("reviews", [(f"{found['reviews']} review{'s' if found['reviews'] != 1 else ''}", "text")]))
    if found["red"]:
        parts.append(("red", [(marks.health["hot"] + " gate red", "bad")]))
    return parts


def spawns_badge(tool: Any, marks: _common.Marks) -> list[tuple[str, list[Part]]]:
    """The Spawns tab badge parts: only actionable anomalies (orphan, expired, live unowned), never total or
    resource counts."""
    del marks
    found = _spawnfather.summary(tool.snapshot)
    parts: list[tuple[str, list[Part]]] = []
    for name, style in (("orphan", "warn"), ("expired", "warn"), ("unowned", "text")):
        if found[name]:
            parts.append((name, [(f"{found[name]} {name}", style)]))
    return parts


def _tab_parts(key: str, label: str, active: bool, badge: list[tuple[str, list[Part]]],
               dropped: tuple[str, ...], stale: bool) -> list[Part]:
    """Styled segments for one header tab, with any surviving badge items in parentheses."""
    parts: list[Part] = [(key, "accent" if active else "key"), (f" {label}", "title" if active else "dim")]
    kept = [items for name, items in badge if name not in dropped]
    if kept:
        parts.append((" (", "dim"))
        for index, items in enumerate(kept):
            if index:
                parts.append((" · ", "dim"))
            parts.extend((text, "dim" if stale else style) for text, style in items)
        parts.append((")", "dim"))
    return parts


def draw_header(canvas: Canvas, page: str, tools: dict[str, Any], name: str) -> None:
    """Row 0: `Godfather` on the left, followed by clickable page tabs when Lanefather or Spawnfather is present.
    Badge items drop in HEADER_DROPS order on narrow screens; clicking a tab switches to that page."""
    if canvas.height < 2 or canvas.width < 4:
        return
    marks = _common.marks_of(canvas)
    x = canvas.put(0, 1, name, "title", max(0, canvas.width - 2))
    present_extra = [meta for meta in TAB_META[1:] if meta[3] and tools.get(meta[3]) is not None
                     and tools[meta[3]].present]
    if not present_extra:
        return
    tabs = [TAB_META[0], *present_extra]
    badges: dict[str, list[tuple[str, list[Part]]]] = {}
    stales: dict[str, bool] = {}
    for tab_page, _, _, tool_name in tabs:
        tool = tools.get(tool_name) if tool_name else None
        stales[tab_page] = bool(tool is not None and tool.state == "stale")
        if tool is not None and tool.readable:
            badges[tab_page] = lanes_badge(tool, marks) if tab_page == "lanes" else spawns_badge(tool, marks)
        else:
            badges[tab_page] = []

    room = canvas.width - 1 - (x + TAB_GAP)
    if room <= 0:
        return

    def render_tabs(dropped: tuple[str, ...]) -> tuple[list[tuple[str, list[Part], int]], int]:
        built: list[tuple[str, list[Part], int]] = []
        for tab_page, key, label, _ in tabs:
            parts = _tab_parts(key, label, tab_page == page, badges[tab_page], dropped, stales[tab_page])
            size = sum(text_width(text) for text, _ in parts)
            built.append((tab_page, parts, size))
        total = sum(size for _, _, size in built) + TAB_GAP * max(0, len(built) - 1)
        return built, total

    chosen: list[tuple[str, list[Part], int]] = []
    for step in range(len(HEADER_DROPS) + 1):
        chosen, total = render_tabs(HEADER_DROPS[:step])
        if total <= room:
            break

    cursor = x + TAB_GAP
    limit = canvas.width - 1
    for tab_page, parts, _ in chosen:
        if cursor >= limit:
            break
        end = canvas.segments(0, cursor, parts, limit - cursor)
        pill_start = max(0, cursor - 1)
        pill_end = min(canvas.width, end + 1)
        if tab_page == page and end > cursor:
            canvas.select(0, pill_start, pill_end)
        canvas.target(0, pill_start, pill_end, (tab_page,))
        cursor = end + TAB_GAP


# The page frame

def stale_note(tool: Any) -> str:
    since = tool.stale_since
    return f"stale since {since.astimezone().strftime('%H:%M')}" if isinstance(since, datetime.datetime) else ""


def draw_notice(canvas: Canvas, page: str, lines: list[str]) -> None:
    """A page that cannot be drawn: its tool is unsupported, or has not answered yet."""
    top = _layout.HEADER_HEIGHT
    rect = Rect(top, 0, min(max(0, canvas.height - top - 1), len(lines) + 4), canvas.width)
    if rect.height < 3 or rect.width < 12:
        canvas.put(top, 1, lines[0], "bad", max(0, canvas.width - 2))
        return
    panel = Panel(canvas, rect, PAGE_TABLE[page]).frame(TITLES[page])
    canvas.tables[PAGE_TABLE[page]] = dict(x=rect.x, y=rect.y, width=rect.width, height=rect.height,
                                           top=panel.inner.y, visible=0, count=0, offset=0, base=None, row_items=None)
    for row, line in enumerate(lines[:panel.inner.height]):
        panel.put(row, line, "text" if row == 0 else "dim")


PAGE_TABLE = {"lanes": "lanes", "spawns": "spawns"}
MARK = 4  # a row's state mark and the gap after it


def draw_table(canvas: Canvas, rect: Rect, name: str, title: str, spec: list[Spec], rows: Sequence[Row],
               selected: Optional[int], values: Callable[[Row], dict[str, Any]],
               heading: Callable[[Row], Part], note: str = "") -> None:
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


def lane_values(row: LaneRow, marks: _common.Marks, moment: Optional[datetime.datetime]) -> dict[str, Any]:
    ticket = row["ticket"].get("number")
    receipt = row["receipt"]["status"]
    review = row["review"]
    return {"state": (marks.rows.get(row["status"], "·"), "text"), "lane": (dashboard_text(row["id"]), "text"),
            "ticket": (f"#{ticket}" if ticket else "", "dim"), "stage": (_lanefather.stage(row), "text"),
            "receipt": (_lanefather.receipt_text(row),
                        "bad" if receipt in ("red", "failed") else "ok" if receipt in ("green", "passed") else "dim"),
            "review": (_lanefather.review_text(row), "warn" if review["open"] else "dim"),
            "claims": (str(len(row["claims"])), "dim"), "age": (age(moment, row.get("created")), "dim")}


def project_heading(row: LaneTableRow) -> Part:
    if row["kind"] != "project":
        return "", "dim"
    if row["error"]:
        return f"{row['project']} · {dashboard_text(row['error'])}", "dim"
    of = f"{row['active']} of {row['cap']} lanes" if row["cap"] is not None else \
        f"{row['count']} lane{'s' if row['count'] != 1 else ''}"
    return f"{row['project']} · {of}", "bold"


def lane_facts(lane: Lane, model: Optional[Model], extra: list[tuple[str, list[Part], Link]]) \
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
               moment: Optional[datetime.datetime], extra: Callable[[Lane], list[Any]] = lambda _: []) -> None:
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
    found = rows[selected] if selected is not None and 0 <= selected < len(rows) else None
    lane = found if found is not None and found["kind"] == "lane" else None
    for name, rect in screen.panels.items():
        if name == "lanes":
            draw_table(canvas, rect, "lanes", "LANES", LANE_COLUMNS, rows, selected,
                       lambda row: lane_values(row, marks, moment) if row["kind"] == "lane" else {}, project_heading,
                       stale_note(tool))
        elif name == "details":
            if lane is None:
                _details.draw_facts(canvas, rect, "LANE DETAILS", [("", [("No lanes", "dim")], None)])
            else:
                _details.draw_facts(canvas, rect, "LANE DETAILS", lane_facts(lane, model, extra(lane)))
        elif name == "events":
            draw_strip(canvas, rect, model, "lanefather", "lane",
                       lambda row: lane is not None and lane_event_of(row, lane), moment)
    if screen.arrangement == "small":
        canvas.put(_layout.HEADER_HEIGHT, 1, "Enlarge the terminal for the Lanes page", "dim", max(0, canvas.width - 2))
    if tool.state == "stale":
        for row in canvas.cells[_layout.HEADER_HEIGHT:canvas.height - 1]:
            for cell in row:
                cell[1] = "dim"


def lane_event_of(row: dict[str, Any], lane: Lane) -> bool:
    """A Lanefather event is the lane's when its subject is the lane's id in the lane's project: by the state
    directory it names, else by the project's name; never by the id alone where the event names another project."""
    subject: dict[str, Any] = row.get("subject") or {}
    if subject.get("type") != "lane" or subject.get("id") != lane["id"]:
        return False
    if subject.get("state_dir"):
        return _lanefather.same_dir(subject["state_dir"], lane["state_dir"])
    return subject.get("project") in (None, lane["project"])


# The Spawns page (fathers#86, fathers#170)

PROCESS_COLUMNS: list[Spec] = [("state", "", 2, False, 0), ("label", "label", 0, False, 0), ("id", "id", 8, False, 4),
                               ("owner", "owner", 16, False, 3), ("lane", "lane", 14, False, 5),
                               ("age", "age", 6, True, 2), ("ttl", "ttl left", 8, True, 1),
                               ("pid", "pid", 6, True, 6), ("processes", "processes", 14, False, 7)]


def process_values(row: ProcessRow, marks: _common.Marks, elapsed: float) -> dict[str, Any]:
    """A SPAWNS row: an owned entry, a live or dead unowned process, or an unowned resource."""
    label = (dashboard_text(_spawnfather.label_of(row)), "text")
    if row["kind"] == "unowned":
        gone = row.get("alive") is False
        seen = row["age"]
        shown_age = minutes(seen if gone else seen + elapsed) if seen is not None else "—"
        if gone:
            return {"state": (marks.rows["gone"], "dim"), "id": ("", "dim"),
                    "label": (label[0], "dim"), "owner": (dashboard_text(row["why"]), "dim"), "lane": ("", "dim"),
                    "age": (shown_age, "dim"), "ttl": ("gone", "dim"), "pid": (str(row["pid"]), "dim"),
                    "processes": ("", "dim")}
        return {"state": (marks.rows["unowned"], "text"), "id": ("", "dim"), "label": label,
                "owner": (dashboard_text(row["why"]), "warn"), "lane": ("", "dim"),
                "age": (shown_age, "dim"), "ttl": ("", "dim"), "pid": (str(row["pid"]), "dim"),
                "processes": ("", "dim")}
    if row["kind"] == "resource":
        idle = row["age"]
        shown_age = long(idle) if idle is not None else "—"
        return {"state": (marks.rows["resource"], "text"), "id": ("", "dim"), "label": label,
                "owner": (dashboard_text(row["why"]), "dim"), "lane": ("", "dim"),
                "age": (shown_age, "dim"), "ttl": ("", "dim"),
                "pid": ("", "dim"), "processes": (resource_text(row["resource_kind"], row["id"]), "dim")}
    resource = row.get("resource")
    count = row.get("processes")
    return {"state": (marks.rows.get(row["state"], "·"), "text"), "id": (dashboard_text(row["id"]), "dim"),
            "label": label, "owner": (dashboard_text(row["owner"]), "dim" if row.get("owner_alive") else "warn"),
            "lane": (dashboard_text(row.get("lane") or ""), "dim"), "age": (minutes(row["age"] + elapsed), "dim"),
            "ttl": (_spawnfather.ttl_text(row, elapsed), "warn" if row["state"] == "expired" else "text"),
            "pid": (str(row["pid"] or ""), "dim"),
            "processes": (resource_text(str(resource.get("kind")), str(resource.get("id"))) if resource
                          else str(count) if count is not None else "", "dim")}


def resource_text(kind: str, ident: str) -> str:
    """A resource as the page names it: its kind and its id cut short (`simulator 1A2B…`)."""
    return f"{kind} {_spawnfather.short_id(ident, 9)}"


def separator(row: ProcessTableRow) -> Part:
    return (row["label"], "dim") if row["kind"] == "separator" else ("", "dim")


def action_summary(row: ProcessRow, available: Sequence[_actions.Action]) -> list[Part]:
    """What `actions` in SPAWN DETAILS says: the available action(s) and their key, or why none apply."""
    if available:
        by_cmd = {"extend": "extend ttl", "reap": "reap"}
        names: list[str] = []
        for action in available:
            cmd = action.argv[0] if action.argv else ""
            name = by_cmd.get(cmd, action.name.lower())
            if name not in names:
                names.append(name)
        return [(", ".join(names), "text"), (" · a / enter", "dim")]
    if row["kind"] == "resource":
        return [("none · read-only resource", "dim")]
    if row["kind"] == "unowned" and row.get("alive") is False:
        return [("none · process gone", "dim")]
    return [("none", "dim")]


def process_facts(row: ProcessRow, model: Optional[Model], lanes: Optional[dict[str, Any]],
                  elapsed: float, advertised: Any = ()) -> list[tuple[str, list[Part], Link]]:
    """SPAWN DETAILS: an owned entry's command, directory, log, owner and whether it lives, keep, mode, its
    resource, its TTL, and its lease and lane as links; an unowned process's command and why; an unowned resource's
    kind, id, name and why (a worktree's branch, repository, age and size); and the row's available actions."""
    available = _actions.for_process(row, advertised)
    facts: list[tuple[str, list[Part], Link]] = [("label", [(dashboard_text(_spawnfather.label_of(row)), "bold")],
                                                  None)]
    if row["kind"] == "resource":
        facts += [("kind", [(row["resource_kind"], "text")], None), ("id", [(dashboard_text(row["id"]), "text")], None)]
        if row.get("name"):
            facts.append(("name", [(dashboard_text(row["name"]), "text")], None))
        facts.append(("why", [(dashboard_text(row["why"]), "dim")], None))
        for name in ("branch", "repo"):
            if row.get(name):
                facts.append((name, [(dashboard_text(row[name]), "text")], None))
        idle, size = row["age"], row["size"]
        if idle is not None:
            facts.append(("idle", [(long(idle), "text")], None))
        if size is not None:
            facts.append(("size", [(f"{size / 1e9:.1f} GB", "text")], None))
        facts += [("", [], None), ("actions", action_summary(row, available), None)]
        return facts
    if row["kind"] == "unowned":
        gone = row.get("alive") is False
        facts += [("command", [(dashboard_text(row["cmd"]), "dim")], None), ("pid", [(str(row["pid"]), "text")], None),
                  ("why", [(dashboard_text(row["why"]), "dim" if gone else "warn")], None)]
        running = row["age"]
        if running is not None:
            facts.append(("age", [(minutes(running if gone else running + elapsed), "text")], None))
        for name in ("cwd", "root"):
            if row.get(name):
                facts.append(("directory" if name == "cwd" else "root", [path_part(row[name])], None))
        if gone:
            facts.append(("alive", [("gone since the sweep", "dim")], None))
        facts += [("", [], None), ("actions", action_summary(row, available), None)]
        return facts
    facts.append(("id", [(dashboard_text(row["id"]), "text")], None))
    facts.append(("state", [(row["state"] + (f" · {row['phase']}" if row["phase"] != "running" else ""),
                             "warn" if row["state"] != "running" else "text")], None))
    facts.append(("ttl left", [(_spawnfather.ttl_text(row, elapsed), "text")], None))
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
    lease = _spawnfather.lease_of(row, items)
    if lease is not None:
        facts.append(("lease", [(dashboard_text(items[lease]["label"] or items[lease]["pid"]), "accent")],
                      ("overview", _entries.entry_ids(items)[lease])))
    lane = _spawnfather.lane_of(row, lanes)
    if lane is not None:
        facts.append(("lane", [(dashboard_text(f"{lane['id']} · {lane['project']}"), "accent")],
                      ("lanes", lane["key"])))
    elif row.get("lane"):
        facts.append(("lane", [(dashboard_text(row["lane"]), "dim")], None))
    facts.append(("actions", action_summary(row, available), None))
    return facts


def process_links(entries: Sequence[Entry], elapsed: float, label: str = "spawns") \
        -> list[tuple[str, list[Part], Link]]:
    """`spawns` rows: each owned entry's label, state and TTL left, a link to the Spawns page; `none owned`."""
    if not entries:
        return [(label, [("none owned", "dim")], None)]
    return [(label if number == 0 else "",
             [(dashboard_text(_spawnfather.label_of(entry)), "accent"),
              (f" · {entry['state']} · {_spawnfather.ttl_text(entry, elapsed)}", "dim")], ("spawns", entry["key"]))
            for number, entry in enumerate(entries)]


spawn_links = process_links


def draw_spawns(canvas: Canvas, tool: Any, selected: Optional[int], model: Optional[Model],
                lanes: Optional[dict[str, Any]], moment: Optional[datetime.datetime]) -> None:
    """The Spawns page: owned entries, a separator, the last sweep's unowned processes and resources; SPAWN
    DETAILS for the selection; its Spawnfather events."""
    if tool.state == "unsupported":
        draw_notice(canvas, "spawns", [str(tool.problem),
                                       "The Spawns page needs a Spawnfather with `status --json`."])
        return
    if tool.snapshot is None:
        draw_notice(canvas, "spawns", [f"Reading Spawnfather… {tool.problem or ''}".strip()])
        return
    rows = _spawnfather.page_rows(tool.snapshot)
    elapsed = _spawnfather.elapsed(tool.snapshot, moment)
    screen = _layout.page(canvas.width, canvas.height, max(1, len(rows)), "spawns")
    canvas.order = screen.order
    marks = _common.marks_of(canvas)
    found = rows[selected] if selected is not None and 0 <= selected < len(rows) else None
    row = found if found is not None and found["kind"] != "separator" else None
    for name, rect in screen.panels.items():
        if name == "spawns":
            draw_table(canvas, rect, "spawns", "SPAWNS", PROCESS_COLUMNS, rows, selected,
                       lambda shown: process_values(shown, marks, elapsed) if shown["kind"] != "separator" else {},
                       separator, stale_note(tool))
        elif name == "details":
            nothing: list[tuple[str, list[Part], Link]] = [("", [("Nothing owned or reported", "dim")], None)]
            facts = process_facts(row, model, lanes, elapsed, tool.actions) if row else nothing
            _details.draw_facts(canvas, rect, "SPAWN DETAILS", facts)
        elif name == "events":
            draw_strip(canvas, rect, model, "spawnfather", "spwn", lambda event: row is not None and
                       process_event_of(event, row), moment)
    if screen.arrangement == "small":
        canvas.put(_layout.HEADER_HEIGHT, 1, "Enlarge the terminal for the Spawns page", "dim", max(0, canvas.width - 2))
    if tool.state == "stale":
        for line in canvas.cells[_layout.HEADER_HEIGHT:canvas.height - 1]:
            for cell in line:
                cell[1] = "dim"


draw_processes = draw_spawns


def process_event_of(event: dict[str, Any], row: ProcessRow) -> bool:
    """A Spawnfather event is the row's when its subject is the entry, the unowned pid or the resource."""
    subject: dict[str, Any] = event.get("subject") or {}
    if row["kind"] == "entry":
        return subject.get("type") == "entry" and subject.get("id") == row["id"]
    if row["kind"] == "unowned":
        return subject.get("pid") == row["pid"] or subject.get("id") == str(row["pid"])
    return subject.get("type") == "resource" and str(subject.get("id")) in (row["id"], f"{row['resource_kind']}:"
                                                                           f"{row['id']}")
