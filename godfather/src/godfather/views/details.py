"""DETAILS: the selected request's facts as a scrolling document of label and value rows, its lane's when it has
one, and its process tree; a spinner while its slow facts load, and why they could not be (fathers#23)."""
from __future__ import annotations

import datetime
import textwrap
from pathlib import Path
from typing import Any, Optional, cast

from ..adapters import junkfather as _junkfather
from ..adapters import lanefather as _lanefather
from ..layout import Rect
from ..tui import SPINNER, Canvas, safe_link
from ..text import clip, clock, clusters, cluster_width, dashboard_text, megabytes, minutes, short, span, text_width, tilde
from ..widgets import Panel, Part, Table, label_column
from .common import (Item, Marks, Model, bar, expected_run, marks_of, meta_text, priority_mark, priority_of, share,
                     share_style, ticket_of)

Fact = tuple[str, list[Part]]
Link = Optional[tuple[Any, ...]]  # a document row's target (state.Target), or none
BUILTIN_CONTEXT = {"class", "priority", "ticket", "lane", "phase", "kind", "check", "sha", "on", "session",
                   "lanefather.state"}
PROCESS_COLUMNS = [("name", "process", 0, False, 0), ("cpu", "cpu", 6, True, 1), ("rss", "memory", 8, True, 2),
                   ("pid", "pid", 6, True, 0)]


def detail_ticket(item: Item, lane: dict[str, Any]) -> tuple[str, str]:
    ticket: dict[str, Any] = lane.get("ticket") or {}
    value = ticket_of(item.get("ticket")) or ticket_of(ticket.get("number"))
    if not value:
        return "", ""
    url = value if value.startswith("https://") and safe_link(value) else ""
    repository = str(lane.get("repository") or "")
    if value.isdigit() and repository.startswith("https://") and safe_link(repository):
        url = repository.rstrip("/") + "/issues/" + value
    elif value == str(ticket.get("number")):
        candidate = str(ticket.get("url") or "")
        if candidate.startswith("https://") and safe_link(candidate):
            url = candidate
    return ("#" + value if value.isdigit() else value), url


def detail_title(item: Optional[Item]) -> str:
    """A request holding slots or waiting on a lock under a lease has LEASE DETAILS."""
    if item is not None and (item["kind"] != "waiting" or item.get("lock")):
        return "LEASE DETAILS"
    return "REQUEST DETAILS"


def queue_policy(model: Model, moment: Optional[datetime.datetime] = None) -> dict[str, str]:
    """Admission estimates and deadlines for the head, exposed as table fields; times off `moment`'s day say it."""
    head = next((item for item in model["waiters"] if not item.get("lock")), None)
    if head is None or head["count"] <= model["free"]:
        return {}
    fields: dict[str, str] = {}
    since = head.get("since")
    if model["free"] and head.get("passable") and since:
        fields["pass until"] = clock(since + datetime.timedelta(seconds=float(model.get("overtake_window") or 0)),
                                     moment)
    if model.get("order") == "backfill" and model.get("start") is not None and since:
        fields["start est"] = clock(model["start"], moment)
        promised = model.get("reserved")  # the start it is promised: nothing that passes it ends later
        if promised is not None:
            fields["reserved"] = clock(promised, moment)
        if model.get("limit"):
            fields["pass until"] = clock(since + datetime.timedelta(seconds=model["limit"]), moment)
    return fields


def request_facts(item: Item, model: Model, position: int, moment: datetime.datetime, marks: Marks,
                  track: str) -> list[Fact]:
    """The facts of a request: label, status, priority, ticket, its context, then how it waits or runs; `marks` and
    `track` (a bar's empty cell) are the canvas's."""
    lane: dict[str, Any] = item.get("lane") or {}
    context: dict[str, str] = item.get("context") or {}
    ticket_text, ticket_url = detail_ticket(item, lane)
    lock = item.get("lock")
    waiting = item["kind"] == "waiting"
    ahead = [other for other in model["waiters"][:position] if not other.get("lock")] if waiting and not lock else []
    if lock:
        slots = f"{item['count']} slot{'s' if item['count'] != 1 else ''}"
        disposition = "slots released" if waiting else f"{slots} held"
        heading = f"waiting-for-lock · {disposition}"
    elif waiting:
        heading = f"waiting · #{len(ahead) + 1} · wants {item['count']}"
    else:
        heading = (("earlier queue" if item["kind"] == "legacy" else "running")
                   + f" · {item['count']} slot{'s' if item['count'] != 1 else ''}")
    facts: list[Fact] = [("label", [(item["label"] or "(no label)", "bold")]), ("status", [(heading, "text")])]
    if lock:
        facts.append(("lock", [(dashboard_text(lock), "text")]))
    priority = priority_of(item.get("served") or item.get("priority"))
    mark = priority_mark(item, marks)
    asked = priority_of(item.get("priority"))
    how = f" · aged from {asked}" if priority != asked else ""
    how += f" · reprioritized from {item.get('reprioritized')}" if item.get("reprioritized") else ""
    facts.append(("priority", ([(mark, "text"), (" ", "text")] if mark else []) + [(priority, "text"), (how, "dim")]))
    if ticket_text:
        facts.append(("ticket", [(ticket_text, "accent" if ticket_url else "text")]))
    for key, label in (("phase", "phase"), ("kind", "kind"), ("check", "check"), ("sha", "revision"),
                       ("on", "run on"), ("session", "session")):
        if context.get(key):
            facts.append((label, [(dashboard_text(context[key]), "dim" if key in ("sha", "session") else "text")]))
    if item["kind"] == "waiting" and lock:
        facts.extend(expected_facts(item))
    elif item["kind"] == "waiting":
        since = item["since"]
        facts.append(("queued", [(f"{clock(since, moment)} · waiting "
                                  f"{span((moment - since).total_seconds()) if since else '?'}", "text")]))
        if not ahead:
            facts.append(("free", [(f"{model['free']} of {model['slots']} slots", "text")]))
            facts.extend((name, [(value, "text")]) for name, value in queue_policy(model, moment).items())
            if model["used"] and model["memory_block"]:
                facts.append(("blocked", [(model["memory_block"], "warn")]))
        else:
            requests = "request" if len(ahead) == 1 else "requests"
            facts.append(("ahead", [(f"{len(ahead)} {requests} · {sum(w['count'] for w in ahead)} slots", "text")]))
            if item.get("est_start") is not None:  # schedule.forecast(): if nothing more comes
                facts.append(("start est", [(clock(item["est_start"], moment), "text")]))
        facts.extend(expected_facts(item))
    else:
        used, allotted = share(item, model)
        style = share_style(item, model)
        note = " · above its share" if style == "bad" else ""
        facts.append(("cpu", bar(used / allotted if allotted else 0, 10, style, track) +
                      [(f" {used:.1f} of {short(allotted)} cores{note}", "text")]))
        if item["granted"]:
            waited = f" · waited {span((item['granted'] - item['since']).total_seconds())}" if item["since"] else ""
            facts.append(("granted", [(f"{clock(item['granted'], moment)}{waited} · running "
                                       f"{span((moment - item['granted']).total_seconds())}", "text")]))
        facts.extend(expected_facts(item))
        facts.append(("memory", [(f"{megabytes(item['rss'] / 1024)} in {item['procs']} processes", "text")]))
    if item["kind"] != "waiting" and item.get("idle_since") is not None:  # #12: one definition (idle.idle_since())
        idle = cast(datetime.datetime, item["idle_since"])
        facts.append(("idle", [(f"{minutes((moment - idle).total_seconds())} since "
                                f"{idle.astimezone().strftime('%H:%M')}", "warn")]))
    if item.get("asked"):
        at = item.get("asked_at")
        facts.append(("yield", [(f"asked at {at.astimezone().strftime('%H:%M')} for {item['asked']}"
                                 if at else f"asked to yield for {item['asked']}", "warn")]))
    elif item.get("yieldable") or item.get("yields"):
        facts.append(("yield", [("yields between steps when asked" + (f" · yielded {item['yields']}×"
                                                                       if item.get("yields") else ""), "dim")]))
    facts.append(("owner pid", [(str(item["pid"]), "text")]))
    return facts


def key_rows(model: Model, moment: datetime.datetime, marks: Marks, track: str) -> int:
    """The rows of DETAILS' key fields (request_facts(), label to owner pid): the most any QUEUE entry has, so the
    panels stay where they are as the selection moves (gf-heights review); 0 with the queue empty."""
    items = model["holders"] + model["waiters"]
    holders = len(model["holders"])
    return max((len(request_facts(item, model, index - holders, moment, marks, track))
                for index, item in enumerate(items)), default=0)


def expected_facts(item: Item) -> list[Fact]:
    """`expected 18m · p90 31m · 23 runs` (fathers#108): its run type's p50 and p90 over its last runs, as Slotfather
    gives them; yellow once a lease has run past that p90."""
    found = expected_run(item)
    if found is None:
        return []
    runs = int(found["runs"])
    text = f"{minutes(found['p50'])} · p90 {minutes(found['p90'])} · {runs} run{'s' if runs != 1 else ''}"
    return [("expected", [(text, "warn" if item.get("overrun") else "text")])]


def lane_facts(context: dict[str, str], lane: dict[str, Any], value_width: int) -> list[Fact]:
    """The lane a request runs for, from Lanefather: its project, stage, branches, receipt, review and claims."""
    facts: list[Fact] = [("", []), ("lane", [(dashboard_text(context["lane"]), "bold")])]
    if lane.get("error") or not lane:
        facts.append(("details", [("unavailable" + (f" · {lane['error']}" if lane.get("error") else ""), "dim")]))
        return facts
    for name in ("project", "status", "branch", "base"):
        text = dashboard_text(lane.get(name) or "—")
        for index, line in enumerate(textwrap.wrap(text, value_width, break_on_hyphens=False)):
            facts.append(("stage" if name == "status" and index == 0 else name if index == 0 else "",
                          [(line, "text")]))
    checks: dict[str, Any] = lane.get("checks") or {}
    check_text = str(checks.get("status") or "unknown")
    if checks.get("current") is not None:
        check_text += " · current" if checks["current"] else " · stale"
    facts.append(("receipt", [(dashboard_text(check_text), "text")]))
    review: dict[str, Any] = lane.get("review") or {}
    review_text = str(review.get("status") or "unknown")
    if review.get("findings"):
        review_text += f" · {review.get('unresolved', '?')} unresolved of {review['findings']}"
    facts.append(("review", [(dashboard_text(review_text), "text")]))
    for name in ("claims", "after"):
        text = ", ".join(dashboard_text(value) for value in lane.get(name, [])) or "—"
        for index, line in enumerate(textwrap.wrap(text, value_width)):
            facts.append((name if index == 0 else "", [(line, "dim")]))
    return facts


def lock_path_lines(path: str, width: int) -> list[str]:
    """Wrap the entire path in terminal cells and keep a filename together whenever it fits."""
    if text_width(path) <= width:
        return [path]
    folder, separator, name = path.rpartition("/")
    lines: list[str] = []
    for part in (folder + separator, name):
        line, used = "", 0
        for glyph in clusters(part):
            cells = cluster_width(glyph)
            if line and used + cells > width:
                lines.append(line)
                line, used = "", 0
            line += glyph
            used += cells
        if line:
            lines.append(line)
    return lines


def draw_detail(canvas: Canvas, rect: Rect, model: Model, selected: int, cwd: Optional[str],
                moment: datetime.datetime, failure: Optional[str] = None,
                lanes: Optional[dict[str, Any]] = None, procs: Optional[dict[str, Any]] = None) -> None:
    """The selected request's details. `lanes` is Lanefather's status snapshot, decoded: the lane the request joins
    to there (fathers#85) gives its lane facts and makes the `lane` row a link to the Lanes page; `procs` is
    Junkfather's, whose entries the request owns are `processes` rows, links to the Processes page (fathers#86)."""
    items = model["holders"] + model["waiters"]
    panel = Panel(canvas, rect, "details").frame(detail_title(items[selected] if items else None))
    if not items or canvas.loading is not None:  # drawn, so it can hold the focus, with nothing to scroll yet
        panel.viewport(0)
    if not items:
        panel.put(0, "Nothing to select · the queue is idle", "dim")
        return
    if canvas.loading is not None:  # loading in the background (#15): a spinner, centred, until they arrive
        panel.centre([(SPINNER[canvas.loading % len(SPINNER)], "dim"), ("loading", "dim")])
        return
    item = items[selected]
    lane: dict[str, Any] = item.get("lane") or {}
    context: dict[str, str] = item.get("context") or {}
    joined = _lanefather.lane_of(item, lanes, cwd)
    if joined is not None:  # the status snapshot knows its lane: no `show` needed
        lane = _lanefather.as_summary(joined)
    _, ticket_url = detail_ticket(item, lane)
    facts = request_facts(item, model, selected - len(model["holders"]), moment, marks_of(canvas),
                          canvas.theme.track)
    inner = panel.inner.width
    label_width, value_width = label_column([*(name for name, _ in facts), "command", "meta", "cwd"], inner)
    if item.get("lock"):
        row = next(index for index, (name, _) in enumerate(facts) if name == "lock")
        path = facts[row][1][0][0]
        lines = lock_path_lines(path, value_width)
        facts[row:row + 1] = [("lock" if index == 0 else "", [(line, "text")])
                              for index, line in enumerate(lines)]
    where = clip_start(tilde(item["queue"] or cwd or ""), value_width)
    worktree_rows: set[int] = set()
    worktree_url = ""
    if lane.get("worktree"):
        folder = Path(str(lane["worktree"]))
        if folder.is_absolute():
            worktree_url = folder.as_uri()
        for index, line in enumerate(textwrap.wrap(tilde(dashboard_text(lane["worktree"])), value_width,
                                                   break_on_hyphens=False)):
            worktree_rows.add(len(facts))
            facts.append(("worktree" if index == 0 else "", [(line, "dim")]))
    blocks = [("command", str(item["cmd"] or "—"))]
    metadata = str(item.get("meta", ""))
    if context:
        metadata = dashboard_text(meta_text({key: value for key, value in context.items()
                                             if key not in BUILTIN_CONTEXT}))
    if metadata:
        blocks.append(("meta", metadata))
    for name, text in blocks:
        lines = textwrap.wrap(text, value_width, break_on_hyphens=False) or ["—"]
        for index, line in enumerate(lines[:3]):
            if index == 2 and len(lines) > 3:
                line = clip(line + " …", value_width)
            facts.append((name if index == 0 else "", [(line, "dim")]))
    if where:
        facts.append(("cwd", [(where, "dim")]))
    lane_row = -1
    if context.get("lane"):
        found = lane_facts(context, lane, value_width)
        if joined is not None:  # `710-speech-paste-phase · Straylight`, a link to the Lanes page
            lane_row = len(facts) + 1
            found[1] = ("lane", [(dashboard_text(f"{joined['id']} · {joined['project']}"), "accent")])
        facts.extend(found)
    links: dict[int, Any] = {}
    if procs is not None:  # its owned processes (fathers#86): label, state and TTL left, each a link
        owned = _junkfather.of_request(item, procs, joined)
        elapsed = _junkfather.elapsed(procs, moment)
        facts.append(("", []))
        if not owned:
            facts.append(("processes", [("none owned", "dim")]))
        for number, entry in enumerate(owned):
            links[len(facts)] = ("processes", entry["key"])
            facts.append(("processes" if number == 0 else "",
                          [(dashboard_text(_junkfather.label_of(entry)), "accent"),
                           (f" · {entry['state']} · {_junkfather.ttl_text(entry, elapsed)}", "dim")]))
    if failure:  # the slow facts could not be read (fathers#23): said, and tried again
        facts.extend([("", []), ("details", [(f"not loaded · {failure} · retrying", "bad")])])
    tree = item["tree"]
    content_height = len(facts) + (3 + len(tree) if tree else 0)
    document = Canvas(rect.width, content_height)
    column = panel.left  # the document is the panel's width, its content at the panel's columns
    values = column + label_width
    for row, (name, parts) in enumerate(facts):
        document.put(row, column, name, "dim")
        if row in worktree_rows and worktree_url:
            document.hyperlink(row, values, str(parts[0][0]), worktree_url, value_width, "dim")
        elif name == "ticket" and ticket_url:
            document.hyperlink(row, values, str(parts[0][0]), ticket_url, value_width)
        else:
            end = document.segments(row, values, parts, value_width)
            if row == lane_row and joined is not None:
                document.target(row, values, end, ("lanes", joined["key"]))
            elif row in links:
                document.target(row, values, end, links[row])
    if tree:  # under the facts, a blank row, its header, a blank row, then a row per process
        processes = Table.fit(inner, PROCESS_COLUMNS)
        processes.row(document, len(facts) + 1, column, {key: (title, "dim") for key, title, *_ in processes.placed})
        for process_row, (pid, cpu, rss, name) in enumerate(tree, len(facts) + 3):
            processes.row(document, process_row, column, {"pid": str(pid), "cpu": f"{cpu:.0f}%",
                                                           "rss": megabytes(rss / 1024), "name": name})
    panel.document(document, content_height)


def clip_start(path: str, width: int) -> str:
    """A path cut at its start to `width` cells, the end kept: …/worktrees/lane."""
    return path if text_width(path) <= width else "…" + path[-max(0, width - 1):]


def enrich_details(item: Item, capabilities: Optional[dict[str, Any]] = None) -> None:
    """A request's lane summary from `lanefather show`, asked only of a Lanefather whose capabilities advertise it
    (fathers#85); `lanefather too old` otherwise."""
    context: dict[str, str] = item.get("context") or {}
    if context.get("lane"):
        item["lane"] = _lanefather.fetch(context, item.get("workdir") or "", capabilities)


# An event or run (fathers#152): DETAILS while EVENTS was focused last

TOOLS = {"slotfather": "Slotfather", "lanefather": "Lanefather", "junkfather": "Junkfather"}
SUBJECT_SHOWN = ("project", "ticket", "lease_id", "state_dir")  # the subject's fields worth a row of their own


def event_title(row: dict[str, Any]) -> str:
    return "RUN DETAILS" if row.get("kind") == "run" else "EVENT DETAILS"


def joined(parts: Any) -> str:
    found = cast(list[Part], parts or [])
    return "".join(str(part[0]) for part in found)


def mark_facts(row: dict[str, Any], marks: Marks) -> list[Fact]:
    """Each mark on a run, explained: ▲ an outlier against its run type's p90; ↗ ↘ its run type's recent median
    against the one before, and whether its plan changed."""
    facts: list[Fact] = []
    outlier: dict[str, Any] = row.get("outlier") or {}
    if row.get("run") == "outlier" or outlier:
        words = ["outlier"]
        if isinstance(outlier.get("ran"), (int, float)):
            words.append(f"ran {span(float(outlier['ran']))}")
        if isinstance(outlier.get("p90"), (int, float)):
            words.append(f"its run type's p90 {span(float(outlier['p90']))}")
        facts.append(("mark", [(marks.run["outlier"] + " ", "warn"), (" · ".join(words), "text")]))
    trend: dict[str, Any] = row.get("trend") or {}
    if trend:
        verdict = str(trend.get("verdict") or "changed")
        ratio = trend.get("ratio")
        change = f" {round((float(ratio) - 1) * 100):+d}%" if isinstance(ratio, (int, float)) and ratio > 0 else ""
        mark = marks.run.get(verdict, "")
        text = f"{verdict}{change} than its run type's p50"
        why = str(trend.get("why") or ("plan changed" if trend.get("plan_change") else ""))
        facts.append(("trend" if facts else "mark", ([(mark + " ", "warn" if verdict == "slower" else "dim")]
                                                     if mark else []) + [(text, "text")]))
        if why:
            facts.append(("", [(dashboard_text(why), "dim")]))
    return facts


def scalar_text(value: Any) -> Optional[str]:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, (int, str)):
        return dashboard_text(value)
    if isinstance(value, list) and all(isinstance(item, (int, str, float)) for item in cast(list[Any], value)):
        return ", ".join(dashboard_text(item) for item in cast(list[Any], value))
    return None


def field_facts(details: dict[str, Any], skip: tuple[str, ...] = ()) -> list[Fact]:
    """A kind's structured fields as rows, objects one level in (`meta.lane`), nulls and prose left out."""
    facts: list[Fact] = []
    for key, value in details.items():
        if key in skip or key == "message" or value is None:
            continue
        if isinstance(value, dict):
            for inner, item in cast(dict[str, Any], value).items():
                text = scalar_text(item)
                if text is not None:
                    facts.append((f"{key}.{inner}", [(text, "text")]))
            continue
        text = scalar_text(value)
        if text is not None:
            facts.append((str(key), [(text, "text")]))
    return facts


def event_facts(row: dict[str, Any], moment: Optional[datetime.datetime], marks: Marks) -> list[Fact]:
    """The facts of an EVENTS row: a run's label, slots, wait, run time, outcome, pid and its marks explained; any
    other event's tool, kind, time, subject and structured fields."""
    when = cast(Optional[datetime.datetime], row.get("when"))
    tool = TOOLS.get(str(row.get("source")), str(row.get("source") or "?"))
    if row.get("kind") == "run":
        start = cast(Optional[datetime.datetime], row.get("start"))
        count = int(row.get("count") or 0)
        facts: list[Fact] = [("label", [(dashboard_text(row.get("label") or "?"), "bold")])]
        if row.get("running"):
            held = span((moment - start).total_seconds()) if moment and start else ""
            facts.append(("status", [(f"running {held}".strip(), "text")]))
        else:
            facts.append(("outcome", [(joined(row.get("detail")) or str(row.get("ended") or "ended"), "text")]))
        facts.append(("slots", [(f"{count} slot{'s' if count != 1 else ''}", "text")]))
        wait = joined(row.get("wait"))
        if wait:
            facts.append(("waited", [(wait, "text")]))
        granted: dict[str, Any] = row.get("granted") or {}
        if granted.get("priority"):
            facts.append(("priority", [(dashboard_text(granted["priority"]), "text")]))
        if start is not None:
            facts.append(("granted", [(clock(start, moment), "text")]))
        details: dict[str, Any] = row.get("details") or {}
        if not row.get("running"):
            ran = details.get("ran")
            if isinstance(ran, (int, float)) and not isinstance(ran, bool):
                facts.append(("ran", [(span(float(ran)), "text")]))
            elif start is not None and when is not None:
                facts.append(("ran", [(span((when - start).total_seconds()), "text")]))
            if when is not None:
                facts.append(("ended", [(clock(when, moment), "text")]))
        facts.extend(mark_facts(row, marks))
        if row.get("pid"):
            facts.append(("pid", [(str(row["pid"]), "text")]))
        if row.get("lease"):
            facts.append(("lease", [(dashboard_text(row["lease"]), "dim")]))
        return facts
    subject: dict[str, Any] = row.get("subject") or {}
    level = str(row.get("level") or "info")
    facts = [("event", [(dashboard_text(row.get("kind") or "?"), "bold")]),
             ("tool", [(tool, "text")]),
             ("time", [(clock(when, moment), "text")])]
    if level != "info":
        facts.append(("level", [(level, "bad" if level == "error" else "warn")]))
    facts.append(("subject", [(dashboard_text(" ".join(str(part) for part in (subject.get("type"), subject.get("id"))
                                                         if part)), "text")]))
    if row.get("label") and row.get("label") != subject.get("id"):
        facts.append(("label", [(dashboard_text(row["label"]), "text")]))
    for key in SUBJECT_SHOWN:
        if subject.get(key) is not None:
            facts.append((key.replace("_", " "), [(dashboard_text(subject[key]), "text")]))
    if row.get("pid"):
        facts.append(("pid", [(str(row["pid"]), "text")]))
    shown = joined(row.get("detail"))
    if shown:
        facts.append(("detail", [(shown, "text")]))
    fields_of = cast(dict[str, Any], row.get("details") or {})
    fields = field_facts(fields_of)
    if fields:
        facts += [("", []), *fields]
    message = fields_of.get("message")
    if isinstance(message, str) and message:
        facts.append(("message", [(dashboard_text(message), "dim")]))
    return facts


def draw_facts(canvas: Canvas, rect: Rect, title: str, facts: list[tuple[str, list[Part], Link]],
                  note: str = "") -> None:
    """A details panel (an event's, fathers#152; a page's row's, fathers#85): label and value rows, long values
    wrapped, each row's link (state.Target) a click target, and a part styled `url:…` an OSC 8 link."""
    panel = Panel(canvas, rect, "details").frame(title, note)
    label_width, value_width = label_column([name for name, _, _ in facts] or [""], panel.inner.width)
    lines: list[tuple[str, list[Part], Link]] = []
    for name, parts, link in facts:
        text = "".join(text for text, _ in parts)
        if len(parts) != 1 or text_width(text) <= value_width or parts[0][1].startswith("url:"):
            lines.append((name, parts, link))
            continue
        for index, line in enumerate(textwrap.wrap(text, value_width, break_on_hyphens=False) or [""]):
            lines.append((name if index == 0 else "", [(line, parts[0][1])], link if index == 0 else None))
    document = Canvas(rect.width, len(lines))
    column, values = panel.left, panel.left + label_width
    for row, (name, parts, link) in enumerate(lines):
        document.put(row, column, name, "dim")
        end = values
        for text, style in parts:  # a part styled `url:…` is an OSC 8 link: a folder or file muted, a URL accent
            room = values + value_width - end
            if room <= 0:
                break
            if style.startswith("url:"):
                url = style[4:]
                document.hyperlink(row, end, text, url, room, "dim" if url.startswith("file:") else "accent")
                end += min(room, text_width(text))
            else:
                end = document.put(row, end, text, style, room)
        if link is not None:
            document.target(row, values, end, link)
    panel.document(document, len(lines))



def draw_event_detail(canvas: Canvas, rect: Rect, row: Optional[dict[str, Any]],
                      moment: Optional[datetime.datetime]) -> None:
    """DETAILS for EVENTS' selected row: its facts as a scrolling document, long values wrapped."""
    if row is None:
        draw_facts(canvas, rect, "EVENT DETAILS", [("", [("No events logged yet", "dim")], None)])
        return
    draw_facts(canvas, rect, event_title(row),
               [(name, parts, None) for name, parts in event_facts(row, moment, marks_of(canvas))])
