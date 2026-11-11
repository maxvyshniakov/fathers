#!/usr/bin/env python3
"""The dashboard against real queues: `dashboard --once` prints the same canvas the live view draws, so these tests
read its text. Log history is checked on slotfather-dashboard itself."""
from __future__ import annotations
import sys as _path_sys
from pathlib import Path as _Path
_path_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "src"))
from slotfather import config as sf_config


import contextlib
import datetime
import fcntl
import importlib.machinery
import importlib.util
import os
import pathlib
import pty
import re
import json
import select
import shutil
import struct
import subprocess
import tempfile
import termios
import time
import unittest
from unittest import mock
from typing import Any, Callable, cast

from test_slotfather import TOOL, SlotCase


def load_dashboard():
    import importlib
    return importlib.reload(importlib.import_module("slotfather.dashboard"))


class DashboardTests(SlotCase):
    def snapshot(self, width=140, height=40):
        result = self.tool("dashboard", "--once", "--width", str(width), "--height", str(height))
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout

    def test_an_all_lease_shows_all_above_eight_slots_and_its_cells_when_they_fit(self):
        def queue_row(label: str) -> tuple[dict[str, Any], str]:
            with mock.patch.dict(os.environ, self.env, clear=True):
                tool = load_dashboard()
                model = tool.dashboard_model()
                canvas = tool.Canvas(140, 10)
                tool.draw_requests(canvas, 0, 0, 10, 140, model, 0, datetime.datetime.now(datetime.timezone.utc))
            item = next(h for h in model["holders"] if h["label"] == label)
            return item, next(line for line in canvas.render(False).splitlines() if re.search(rf"[ ▶]{label} ", line))

        self.assertEqual(0, self.tool("slots", "10").returncode)
        whole = self.request("whole", "all")
        self.started("whole")
        item, row = queue_row("whole")
        self.assertEqual((10, True), (item["count"], item["all"]))
        self.assertRegex(row, r"whole +all +■")
        self.release("whole")
        self.finish(whole)
        self.assertEqual(0, self.tool("slots", "4").returncode)
        fits = self.request("fits", "all")
        self.started("fits")
        item, row = queue_row("fits")
        self.assertEqual((4, True), (item["count"], item["all"]))
        self.assertRegex(row, r"fits +▪▪▪▪ +■")
        self.release("fits")
        self.finish(fits)

    def test_table_headers_share_the_border_and_empty_messages_start_under_them(self):
        text = self.snapshot(width=220, height=50)
        lines = text.splitlines()
        for title, message in (("QUEUE", "No requests"), ("EVENTS", "No grants")):
            row = next(i for i, line in enumerate(lines) if title in line)
            self.assertEqual(lines[row].index(title), lines[row + 2].index(message))
            self.assertIn("╭", lines[row])
            left, right = lines[row].index("╭"), lines[row].index("╮")
            self.assertFalse(lines[row + 1][left + 1:right].strip(), "top content padding")
            bottom = next(i for i in range(row + 1, len(lines)) if lines[i][left:left + 1] == "╰")
            self.assertFalse(lines[bottom - 1][left + 1:right].strip(), "bottom content padding")
            if title != "EVENTS":
                self.assertIn("pid", lines[row], "column names belong on the top border")

    def test_small_terminal_fallback_preserves_its_footer(self):
        for height in (5, 7):
            with self.subTest(height=height):
                text = self.snapshot(width=40, height=height)
                self.assertIn("slotfather", text.splitlines()[-1])
                self.assertIn("live", text.splitlines()[-1])

    def test_admission_is_available_slots_not_worker_advice_and_honours_memory_gates(self):
        self.env["SLOTFATHER_LOADAVG"] = "0"
        def model():
            with mock.patch.dict(os.environ,self.env,clear=True):
                return load_dashboard().dashboard_model()
        self.assertEqual(4,model()["admission"])
        self.memory.write_text("128 1\n")
        self.assertEqual(4,model()["admission"],"an idle host may still start its first lease")
        holder=self.request("memory-gate-owner",1)
        self.started("memory-gate-owner")
        state=model()
        self.assertEqual(3,state["free"])
        self.assertEqual(0,state["admission"],"low swap blocks additional work despite free slots")
        self.assertEqual("3", self.tool("available").stdout.strip(),
                         "available reports queue space before memory policy; admission includes that policy")
        self.assertEqual(3,state["advice"]["jobs"],"three free worker slots are advice, not memory admission permission")
        self.memory.write_text("8000 1\n")
        self.assertEqual(3,model()["admission"])
        self.release("memory-gate-owner")
        self.finish(holder)

    def test_an_idle_queue_reads_open_with_every_slot_free(self):
        text = self.snapshot()
        self.assertIn("SLOTS", text.splitlines()[0])
        self.assertIn("free", text)
        self.assertIn("slotfather", text.splitlines()[-1])
        self.assertNotIn("slotfather", "\n".join(text.splitlines()[:-1]))
        self.assertNotIn(str(self.state), text)
        self.assertNotIn("OPEN", text)
        self.assertIn("No requests · all 4 slots free", text)
        self.assertNotIn("\x1b[", text, "a snapshot to a pipe is plain text")

    def test_a_blocked_head_explains_why_a_smaller_request_behind_it_waits(self):
        holder = self.request("holder", 2)
        self.started("holder")
        gate = self.request("gate", "all")
        self.wait_for(lambda: "waiting: 4 slot(s) gate" in self.status(), "the gate to queue")
        small = self.request("small", 1)
        self.wait_for(lambda: "waiting: 1 slot(s) small" in self.status(), "the small request to queue")
        text = self.snapshot()
        self.assertNotIn("QUEUED", text)
        self.assertNotIn("next is", text)
        self.assertNotIn("next:", text)
        self.assertNotIn("2/4 held", text)
        self.assertIn("no admission",text)
        self.assertIn("─ QUEUE ", text)
        self.assertNotIn("REQUESTS", text, "the request table is the QUEUE panel")
        self.assertNotIn("RUNNING", text)
        self.assertIn("│ ▶holder", text)
        self.assertEqual(1, text.count("▶"), "only the selected row has a cursor")
        self.assertRegex(text, r"gate +▪▪▪▪ +waiting #1 ")
        self.assertRegex(text, r"small +▪ +waiting #2 ")
        for label in ("holder", "gate", "small"):
            self.release(label)
        for process in (holder, gate, small):
            self.finish(process)

    def test_within_the_overtaking_window_smaller_requests_pass_a_blocked_head(self):
        self.env["SLOTFATHER_OVERTAKE_WINDOW"] = "600"
        holder = self.request("holder", 2)
        self.started("holder")
        gate = self.request("gate", "all")
        self.wait_for(lambda: "waiting: 4 slot(s) gate" in self.status(), "the gate to queue")
        text = self.snapshot()
        self.assertNotIn("PASSING", text)
        self.assertIn("waiting #1", text)
        with mock.patch.dict(os.environ, self.env, clear=True):
            dashboard=load_dashboard()
            model=dashboard.dashboard_model()
            canvas=dashboard.Canvas(160,40)
            dashboard.dashboard_draw(canvas,model,selected=len(model["holders"]))
            self.assertIn("pass until",canvas.render(False))
        for label in ("holder", "gate"):
            self.release(label)
        for process in (holder, gate):
            self.finish(process)

    def test_every_terminal_size_draws_without_error(self):
        holder = self.request("a-rather-long-label-for-a-holder", 1)
        self.started("a-rather-long-label-for-a-holder")
        for width, height in ((20, 10), (49, 13), (50, 14), (80, 24), (109, 30), (110, 30), (200, 60)):
            text = self.snapshot(width, height)
            self.assertEqual(height, len(text.splitlines()), f"{width}x{height}")
            self.assertTrue(all(len(line) <= width for line in text.splitlines()), f"{width}x{height}")
        self.release("a-rather-long-label-for-a-holder")
        self.finish(holder)

    def test_control_bytes_in_labels_never_reach_the_terminal(self):
        holder = self.request("evil\x1b[2Jlabel", 1)
        self.wait_for(lambda: "evil" in self.status(), "the holder to start")
        text = self.snapshot()
        self.assertNotIn("\x1b", text)
        self.assertIn("evil [2Jlabel", text)
        self.release("evil\x1b[2Jlabel")
        self.finish(holder)

    def test_clicking_a_queue_row_updates_selected_in_the_live_terminal(self):
        holder = self.request("click-owner", "all")
        self.started("click-owner")
        waiter = self.request("click-target", 1)
        self.wait_for(lambda: "waiting: 1 slot(s) click-target" in self.status(), "the click target to queue")
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 160, 0, 0))
        process = subprocess.Popen([TOOL, "dashboard", "--interval", "60"], stdin=slave, stdout=slave,
                                   stderr=slave, env=self.env, start_new_session=True)
        self.processes.append(process)
        os.close(slave)
        screen = bytearray()
        def read_until(predicate: Callable[[str], object]) -> None:
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                if select.select([master], [], [], .1)[0]:
                    try:
                        screen.extend(os.read(master, 1024))  # PTYs can split a frame before the request table.
                    except OSError:
                        # Linux reports PTY EIO just before waitpid observes the exit.
                        if process.poll() is None:
                            continue
                plain = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", screen.decode("utf-8", "replace"))
                if predicate(plain):
                    return
            self.fail("live terminal did not render the expected selection")
        read_until(lambda text: re.search(r"label\s+click-owner", text) and re.search(r"status\s+running", text))
        # QUEUE starts at row 5: holder at 7, gap at 8, waiter at 9 (zero-based).
        os.write(master, b"\x1b[<0;10;10M\x1b[<0;10;10m")
        read_until(lambda text: re.search(r"label\s+click-target",text) and re.search(r"status\s+waiting",text))
        os.write(master, b"m")
        read_until(lambda text: "text selection" in text)
        self.assertIn(b"\x1b[?1006l\x1b[?1000l", screen)
        os.write(master, b"m")
        read_until(lambda text: screen.count(b"\x1b[?1000h\x1b[?1006h") >= 2)

        self.release("click-owner")
        self.started("click-target")
        screen.clear()
        os.write(master, b"r")
        # A split PTY read can contain the details before the selected request row.
        read_until(lambda text: re.search(r"label\s+click-target",text) and re.search(r"status\s+running",text)
                   and "▶click-target" in text)
        selected = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", screen.decode("utf-8", "replace"))
        self.assertIn("▶click-target", selected,
                      "the same lease stays selected after moving into the active group")
        os.write(master, b"q")
        read_until(lambda text: process.poll() is not None)
        self.assertEqual(0, process.wait(timeout=3))
        self.assertIn(b"\x1b[?1006l\x1b[?1000l", screen)
        for label in ("click-owner", "click-target"):
            self.release(label)
        for request in (holder, waiter):
            self.finish(request)

    def test_options_are_checked(self):
        for args in (["--interval", "0"], ["--width", "4"], ["--height", "x"], ["--bogus"]):
            result = self.tool("dashboard", *args)
            self.assertEqual(2, result.returncode, args)
        result = self.tool("dashboard")
        self.assertEqual(2, result.returncode)
        self.assertIn("dashboard --once prints one snapshot", result.stderr)


class SeparationTests(SlotCase):
    def test_the_queue_runs_without_the_dashboard_beside_it(self):
        alone = self.tmp / "alone"
        alone.mkdir()
        shutil.copy(TOOL, alone / "slotfather")
        shutil.copytree(pathlib.Path(TOOL).parent / "src", alone / "src", ignore=shutil.ignore_patterns("__pycache__"))
        (alone / "src/slotfather/dashboard.py").unlink()
        run = lambda *args: subprocess.run([str(alone / "slotfather"), *args], env=self.env, capture_output=True,
                                           text=True, timeout=20)
        self.assertEqual(0, run("run", "--", "true").returncode)
        result = run("dashboard", "--once")
        self.assertEqual(2, result.returncode)
        self.assertIn("the dashboard is not installed", result.stderr)

    def test_plain_slotfather_opens_the_dashboard_in_a_terminal_and_prints_usage_without_one(self):
        result = self.tool()
        self.assertEqual(2, result.returncode)
        self.assertIn("usage: slotfather", result.stderr)
        pid, terminal = pty.fork()
        if pid == 0:
            # Ghostty's terminfo has REP, which the ncurses Python links sent as a lone last byte of █ or ─.
            os.execve(TOOL, [TOOL], {**self.env, "TERM": "xterm-ghostty"})
        fcntl.ioctl(terminal, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 120, 0, 0))
        screen, deadline = b"", time.monotonic() + 15
        while b"QUEUE" not in screen and time.monotonic() < deadline:
            if select.select([terminal], [], [], 0.1)[0]:
                with contextlib.suppress(OSError):
                    screen += os.read(terminal, 65536)
        os.write(terminal, b"q")
        status = None
        while status is None and time.monotonic() < deadline + 10:
            # Keep reading: a dashboard blocked on a full terminal never reads the q.
            if select.select([terminal], [], [], 0.1)[0]:
                with contextlib.suppress(OSError):
                    screen += os.read(terminal, 65536)
            done, code = os.waitpid(pid, os.WNOHANG)
            status = code if done else None
        os.close(terminal)
        if status is None:
            os.kill(pid, 9)
            os.waitpid(pid, 0)
            self.fail("the dashboard did not quit on q")
        self.assertIn(b"QUEUE", screen)
        self.assertIn(b"\x1b[?1000h\x1b[?1006h", screen)
        self.assertIn(b"\x1b[?1006l\x1b[?1000l", screen)
        self.assertEqual(0, os.waitstatus_to_exitcode(status))
        text = screen.decode("utf-8")  # strict: a stray byte of a multibyte character fails here
        self.assertIn("■", text)
        self.assertNotRegex(text, r"\x1b\[\d+b", "no REP: a terminal repeats bytes, not characters")
        self.assertTrue(text.rstrip().endswith("\x1b[?1049l"), "quitting leaves the alternate screen")

    def test_the_queue_code_holds_no_terminal_code(self):
        source = pathlib.Path(TOOL).read_text()
        for word in ("curses", "lsof", "Canvas"):
            self.assertNotIn(word, source)


class UnifiedLaneTests(unittest.TestCase):
    def test_running_and_waiting_share_a_viewport_with_an_unselectable_gap(self):
        tool=load_dashboard()
        now=datetime.datetime.now(datetime.timezone.utc)
        holder=dict(label="active",kind="held",count=1,pid=10,style="h0",cpu=150,since=now,granted=now)
        waiter=dict(label="pending",kind="waiting",count=2,pid=11,style="warn",cpu=0,since=now,granted=None)
        model=dict(holders=[holder],waiters=[waiter],slots=6,cores=12)
        canvas=tool.Canvas(130,8)
        tool.draw_requests(canvas,0,0,8,130,model,1,now)
        lines=canvas.render(False).splitlines()
        self.assertIn("QUEUE",lines[0])
        self.assertIn("active",lines[2])
        self.assertFalse(lines[3][1:-1].strip())
        self.assertIn("│ ▶pending",lines[4])
        column=lines[0].index("cpu use/share")+len("cpu use/share")
        self.assertTrue(lines[4][:column].rstrip().endswith("waiting #1"),lines[4])
        self.assertEqual(1,tool.mouse_action(canvas,("mouse",0,20,4,True),{},0))
        self.assertEqual(0,tool.mouse_action(canvas,("mouse",0,20,3,True),{},0))
        canvas=tool.Canvas(130,6)
        tool.draw_requests(canvas,0,0,6,130,model,1,now)
        lines=canvas.render(False).splitlines()
        self.assertIn("pending",lines[3],"selection must remain visible beyond the gap")
        self.assertIn("↑ 1 above",lines[1],"counts exclude the group separator")


class QueueTableTests(unittest.TestCase):
    """The QUEUE table (owner request): no status column; a request that holds no slots says so in the cpu column;
    slot counts are drawn as cells in the row's colour."""

    def setUp(self) -> None:
        self.tool = load_dashboard()
        self.now = datetime.datetime.now(datetime.timezone.utc)

    def model(self, **extra: Any) -> dict[str, Any]:
        now = self.now
        holders = [dict(label="build", kind="held", count=2, pid=10, style="h0", cpu=150, since=now, granted=now),
                   dict(label="older", kind="legacy", count=1, pid=11, style="legacy", cpu=0, since=now, granted=now)]
        waiters = [dict(label="gate", kind="waiting", count=3, pid=12, style="text", cpu=0, since=now, granted=None),
                   dict(label="back", kind="waiting", count=1, pid=13, style="text", cpu=0, since=now, granted=None,
                        yields=1)]
        return {"holders": holders, "waiters": waiters, "slots": 6, "cores": 12, **extra}

    def draw(self, model: dict[str, Any], width: int = 120, height: int = 10) -> Any:
        canvas = self.tool.Canvas(width, height)
        self.tool.draw_requests(canvas, 0, 0, height, width, model, 0, self.now)
        return canvas

    def row(self, canvas: Any, label: str) -> int:
        return next(n for n, line in enumerate(canvas.render(False).splitlines()) if f" {label} " in line + " "
                    or f"▶{label}" in line)

    def styles(self, canvas: Any, y: int, text: str) -> set[str]:
        line = "".join(cell[0] for cell in canvas.cells[y])
        start = line.index(text)
        return {canvas.cells[y][x][1] for x in range(start, start + len(text))}

    def test_there_is_no_status_column_and_time_follows_the_cpu_column(self) -> None:
        canvas = self.draw(self.model())
        lines = canvas.render(False).splitlines()
        header = lines[0]
        self.assertNotIn("status", header)
        places = [header.index(title) for title in ("QUEUE", "slots", "cpu use/share", "time", "pid")]
        self.assertEqual(sorted(places), places)
        self.assertRegex(lines[self.row(canvas, "gate")], r"waiting #1 +0s +12 ")
        self.assertRegex(lines[self.row(canvas, "build")], r"■ 1\.5/4 +0s +10 ")

    def test_a_request_holding_no_slots_says_why_in_the_cpu_column_in_a_neutral_colour(self) -> None:
        canvas = self.draw(self.model())
        lines = canvas.render(False).splitlines()
        end = lines[0].index("cpu use/share") + len("cpu use/share")
        for label, state in (("gate", "waiting #1"), ("back", "yielded #2")):
            y = self.row(canvas, label)
            self.assertTrue(lines[y][:end].rstrip().endswith(state), lines[y])
            self.assertEqual({"text"}, self.styles(canvas, y, state), "white like the rest of the row")
            self.assertEqual({"text"}, self.styles(canvas, y, label), "a waiting label is not yellow")
            self.assertNotIn("warn", {cell[1] for cell in canvas.cells[y]})
        build = self.row(canvas, "build")
        self.assertRegex(lines[build], r"■+ 1\.5/4")

    def test_a_row_is_one_colour_the_slots_colour_running_and_white_waiting_but_for_the_bar(self) -> None:
        canvas = self.draw(self.model())
        for label, style in (("build", "h0"), ("older", "legacy"), ("gate", "text"), ("back", "text")):
            y = self.row(canvas, label)
            cells = canvas.cells[y][3:-3]
            text = {cell[1] for cell in cells if cell[0] not in (" ", "■", "")}
            self.assertEqual({style}, text, label)
        build = self.row(canvas, "build")
        self.assertEqual({"h0", "frame"}, {cell[1] for cell in canvas.cells[build] if cell[0] == "■"},
                         "the CPU bar fills in the row's colour; its empty cells keep the frame colour")
        self.assertTrue(all(cell[2] for cell in canvas.cells[build][3:-3]), "the selection highlight stays on top")

    def test_bars_and_states_start_at_one_column_and_the_numbers_line_up(self) -> None:
        now = self.now
        rows = [(1, 0.0, 12), (2, 12.3, 12), (8, 5.1, 24), (1, 0.5, 12), (2, 1.3, 12)]  # shares 1.5, 3, 12 ... 0.5
        holders = [dict(label=f"run{n}", kind="held", count=count, pid=20 + n, style=f"h{n}", cpu=cpu * 100,
                        since=now, granted=now) for n, (count, cpu, _) in enumerate(rows)]
        for slots, cores in ((8, 12), (2, 12), (24, 12), (8, 4)):
            with self.subTest(slots=slots, cores=cores):
                model = self.model(slots=slots, cores=cores)
                model["holders"] = holders
                canvas = self.draw(model, height=14)
                lines = canvas.render(False).splitlines()
                labels = [f"run{n}" for n in range(len(rows))]
                starts = {lines[self.row(canvas, label)].index("■", lines[0].index("cpu use/share")) for label in labels}
                self.assertEqual({lines[0].index("cpu use/share")}, starts, "every bar starts where the header does")
                slashes = {lines[self.row(canvas, label)].index("/", min(starts)) for label in labels}
                self.assertEqual(1, len(slashes), "use and share line up around the slash")
                states = {lines[self.row(canvas, label)].index(state) for label, state in (("gate", "waiting #1"),
                                                                                          ("back", "yielded #2"))}
                self.assertEqual(starts, states, "a waiting row's state starts where the bars do")

    def test_a_cpu_bar_fills_in_the_rows_colour_unless_it_warns(self) -> None:
        now, old = self.now, self.now - datetime.timedelta(minutes=10)
        model = self.model()
        model["holders"] = [
            dict(label="fair", kind="held", count=2, pid=10, style="h0", cpu=200, since=now, granted=now),
            dict(label="hog", kind="held", count=1, pid=11, style="h1", cpu=900, since=now, granted=now),
            dict(label="idle", kind="held", count=1, pid=12, style="h2", cpu=5, since=old, granted=old)]
        styles = [self.tool.queue_bar_style(item, model, now) for item in model["holders"]]
        self.assertEqual(["h0", "bad", "warn"], styles, "over its share stays red, nearly idle stays yellow")
        canvas = self.draw(model)
        cpu = canvas.render(False).splitlines()[0].index("cpu use/share")
        for label, filled in (("fair", {"h0", "frame"}), ("hog", {"bad"})):
            y = self.row(canvas, label)
            self.assertEqual(filled, {cell[1] for cell in canvas.cells[y][cpu:cpu + 10] if cell[0] == "■"}, label)

    def test_slots_are_countable_cells_in_the_rows_colour_alike_for_running_and_waiting(self) -> None:
        for form, two in (("spaced", "■ ■"), ("small", "▪▪")):
            with self.subTest(form=form), mock.patch.object(self.tool, "SLOT_CELL", form):
                one, three = two[0], self.tool.cells_text(3)
                self.assertEqual(3, three.count(one))
                canvas = self.draw(self.model())
                lines = canvas.render(False).splitlines()
                column = lines[0].index("slots")
                for label, cells, style in (("build", two, "h0"), ("older", one, "legacy"), ("gate", three, "text"),
                                            ("back", one, "text")):
                    y = self.row(canvas, label)
                    self.assertEqual(cells, lines[y][column:].split("  ")[0], label)
                    self.assertEqual({style}, self.styles(canvas, y, cells), label)
                self.assertNotRegex(lines[self.row(canvas, "gate")], r"\b3\b", "no digit for the count")

    def test_large_counts_are_capped_with_the_rest_counted_and_all_says_so(self) -> None:
        model = self.model(slots=24)
        model["holders"][0]["count"] = 20
        model["waiters"][0].update(count=24, all=True)
        canvas = self.draw(model)
        lines = canvas.render(False).splitlines()
        column = lines[0].index("slots")
        build = lines[self.row(canvas, "build")][column:].split("  ")[0]
        self.assertRegex(build, r"^▪+ \+\d+$")
        self.assertEqual(20, build.count("▪") + int(build.split("+")[1]))
        self.assertEqual("all", lines[self.row(canvas, "gate")][column:].split("  ")[0])

    def test_a_narrow_table_drops_columns_and_keeps_every_row_inside_its_frame(self) -> None:
        titles = ("slots", "cpu use/share", "time", "pid")
        for width, kept in ((110, titles), (59, titles), (52, titles[:3]), (44, titles[:2]), (30, titles[:1])):
            with self.subTest(width=width):
                lines = self.draw(self.model(), width=width).render(False).splitlines()
                self.assertEqual(kept, tuple(title for title in titles if title in lines[0]),
                                 "pid goes first, then time, then cpu use/share")
                for line in lines[:8]:
                    self.assertLessEqual(len(line.rstrip()), width)
                gate = next(line for line in lines if "gate" in line)
                self.assertIn("▪▪▪", gate, "the slot cells stay longest after the label")
                if "cpu use/share" in kept:
                    self.assertIn("waiting #1", gate, "the state outlasts the time and pid")
                self.assertEqual(gate.rstrip()[-1], "│")

    def test_a_wide_table_shares_its_width_out_in_proportion(self) -> None:
        model = self.model()
        model["holders"].append(dict(label="wide", kind="held", count=1, pid=14, style="h1", cpu=1230, since=self.now,
                                     granted=self.now))
        for width in (120, 160, 220):
            with self.subTest(width=width):
                layout = self.tool.queue_layout(width - 6, model)
                sizes = {key: size for key, _, _, size, _ in layout.placed}
                self.assertEqual(width - 6, sum(sizes.values()) + 2 * (len(sizes) - 1), "the table fills the panel")
                ratios = [sizes[key] / layout.natural[key] for key in sizes]
                self.assertLess(max(ratios) - min(ratios), 0.25, f"in proportion: {sizes} of {layout.natural}")
                self.assertTrue(all(sizes[key] >= layout.natural[key] for key in sizes))
                self.assertTrue(5 <= layout.bar <= 10, "the bar grows with its column, at most twice")
                canvas = self.draw(model, width=width)
                lines = canvas.render(False).splitlines()
                for line in lines[:9]:
                    self.assertLessEqual(len(line.rstrip()), width)
                cpu = lines[0].index("cpu use/share")
                starts = {lines[self.row(canvas, label)].index("■", cpu) for label in ("build", "older", "wide")}
                self.assertEqual({cpu}, starts, "bars start under the header, on every row")
                self.assertEqual({cpu}, {lines[self.row(canvas, "gate")].index("waiting #1")})
                slashes = {lines[self.row(canvas, label)].index("/", cpu) for label in ("build", "older", "wide")}
                self.assertEqual(1, len(slashes))
                pid_end = lines[0].index("pid") + len("pid")
                self.assertTrue(lines[self.row(canvas, "build")][:pid_end].endswith("10"), "pid under its title")

    def test_an_ascii_terminal_gets_pure_ascii_rows(self) -> None:
        model = self.model(slots=24)
        model["holders"][0]["count"] = 20
        for line in self.draw(model).lines(False, ascii_only=True):
            self.assertTrue(line.isascii(), line)

    def test_a_large_count_is_cut_without_trying_every_count_below_it(self) -> None:
        item = dict(kind="held", style="h0", count=100000)
        started = time.perf_counter()
        self.assertEqual([("▪", "h0"), (" +99999", "h0")], self.tool.slot_cells(item, 11), "at most 8 columns")
        self.assertLess(time.perf_counter() - started, 0.05)
        self.assertEqual([("▪▪▪▪", "h0"), (" +16", "h0")], self.tool.slot_cells(dict(item, count=20), 8))


class EventDetailTests(unittest.TestCase):
    """EVENTS' detail column, from the history line of each request (#queue-table review): it was meant to say why a
    request left (gave up, owner exited) but queue.log alone cannot tell a waiter that gave up from a lease that
    ended once the log is longer than the part read, so on a long-running queue it was always empty."""

    LOG = ("2026-11-11T02:00:00Z granted 1 to build (pid 101)\n"
           "2026-11-11T02:00:05Z granted 2 to verify (pid 102)\n"
           "2026-11-11T02:04:00Z released 1 (0000000001-101.json)\n"
           "2026-11-11T02:05:00Z released 1 (0000000003-103.json)\n"
           "2026-11-11T02:06:00Z reaped held lease of 2 for verify (pid 102 exited)\n"
           "2026-11-11T02:06:30Z granted 1 to live (pid 104)\n")

    def history(self) -> list[dict[str, Any]]:
        def line(label: str, pid: int, queued: str, granted: str | None, ended: str, outcome: str,
                 waited: int, ran: int | None, status: int | None) -> dict[str, Any]:
            return {"v": 2, "label": label, "count": 1, "pid": pid, "queued": queued, "granted": granted,
                    "ended": ended, "outcome": outcome, "waited": waited, "ran": ran, "status": status}
        return [line("build", 101, "2026-11-11T01:57:00Z", "2026-11-11T02:00:00Z", "2026-11-11T02:04:00Z",
                     "released", 180, 240, 1),
                line("gate", 103, "2026-11-11T01:50:00Z", None, "2026-11-11T02:05:00Z", "left", 900, None, None),
                line("verify", 102, "2026-11-11T02:00:05Z", "2026-11-11T02:00:05Z", "2026-11-11T02:06:00Z",
                     "reaped", 0, 355, None)]

    def events(self, padding: int = 0) -> list[dict[str, Any]]:
        tool = load_dashboard()
        directory = pathlib.Path(tempfile.mkdtemp(prefix="slotfather-events-"))
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        filler = "".join(f"2026-11-11T01:00:00Z sweep note {n:06d}\n" for n in range(padding))
        (directory / "queue.log").write_text(filler + self.LOG)
        (directory / "history.jsonl").write_text("".join(json.dumps(item) + "\n" for item in self.history()))
        live = [{"pid": 104, "since": "2026-11-11T02:05:30Z", "granted": "2026-11-11T02:06:30Z", "state": "held"}]
        events = tool.queue_events(directory)
        tool.describe_events(events, directory, live)
        return events

    def test_each_event_says_what_its_history_line_knows(self) -> None:
        for padding in (0, 20000):  # 20000 lines: longer than the part of queue.log the dashboard reads
            with self.subTest(padding=padding):
                found = [(e["verb"], e["label"], e["detail"], e["delta"]) for e in self.events(padding)]
                self.assertEqual([("granted", "build", "waited 3m00s", 1), ("granted", "verify", "no wait", 2),
                                  ("released", "build", "exit 1, ran 4m00s", -1),
                                  ("left", "gate", "gave up after 15m00s", 0),
                                  ("reaped", "verify", "owner exited, ran 5m55s", -2),
                                  ("granted", "live", "waited 1m00s", 1)], found)

    def test_slots_are_cells_filled_when_taken_and_hollow_when_handed_back(self) -> None:
        tool = load_dashboard()
        events = self.events()
        events[1]["count"] = 20  # verify's grant: wider than the column
        canvas = tool.Canvas(120, 12)
        tool.draw_events(canvas, 0, 0, 12, 120, {"events": events})
        lines = canvas.render(False).splitlines()
        column = lines[0].index("slots")
        expected = {("granted", "build"): ("▪", "ok"), ("released", "build"): ("▫", "accent"),
                    ("left", "gate"): ("▫", "dim"), ("reaped", "verify"): ("▫▫", "bad"),
                    ("granted", "verify"): ("▪▪▪▪ +16", "ok")}
        for (verb, label), (cells, style) in expected.items():
            row = next(n for n, line in enumerate(lines) if f" {verb} " in line and f" {label} " in line + " ")
            self.assertEqual(cells, lines[row][column:].split("  ")[0], (verb, label))
            self.assertEqual(style, canvas.cells[row][column][1], (verb, label))
        for line in canvas.lines(False, ascii_only=True):
            self.assertTrue(line.isascii(), line)
        self.assertIn("o", canvas.lines(False, ascii_only=True)[next(
            n for n, line in enumerate(lines) if " released " in line)][column:column + 2])

    def test_the_columns_are_time_event_slots_detail_label_pid(self) -> None:
        tool = load_dashboard()
        canvas = tool.Canvas(120, 12)
        tool.draw_events(canvas, 0, 0, 12, 120, {"events": self.events()})
        lines = canvas.render(False).splitlines()
        places = [lines[0].index(title) for title in ("EVENTS", "event", "slots", "detail", "label", "pid")]
        self.assertEqual(sorted(places), places)
        row = next(line for line in lines if " gate " in line)
        self.assertRegex(row, r"\d\d:\d\d:\d\d +left +▫ +gave up after 15m00s +gate +103")
        narrow = tool.Canvas(48, 12)
        tool.draw_events(narrow, 0, 0, 12, 48, {"events": self.events()})
        header = narrow.render(False).splitlines()[0]
        self.assertEqual(["event", "slots"], [t for t in ("event", "slots", "detail", "pid") if t in header],
                         "pid and detail drop first, the event and the cells stay")

    def test_the_detail_column_shows_it_dim(self) -> None:
        tool = load_dashboard()
        model = {"events": self.events()}
        canvas = tool.Canvas(120, 12)
        tool.draw_events(canvas, 0, 0, 12, 120, model)
        lines = canvas.render(False).splitlines()
        row = next(n for n, line in enumerate(lines) if "gave up after 15m00s" in line)
        start = lines[row].index("gave up")
        self.assertEqual("dim", canvas.cells[row][start][1])
        self.assertTrue(any("exit 1, ran 4m00s" in line for line in lines))


class MouseInputTests(unittest.TestCase):
    def test_click_and_wheel_follow_the_visible_rows_of_an_overflowing_table(self):
        tool = load_dashboard()
        now = datetime.datetime.now(datetime.timezone.utc)
        holders = [dict(label=f"job-{i}", kind="held", count=1, pid=100+i, style="h0",
                        cpu=0, since=now, granted=now) for i in range(8)]
        model = dict(holders=holders, waiters=[], slots=8, cores=8)
        scroll = {}
        def render():
            canvas = tool.Canvas(100, 7)
            canvas.scroll = scroll
            tool.draw_requests(canvas, 0, 0, 7, 100, model, 0, now)
            return canvas
        canvas = render()
        self.assertEqual(1, tool.mouse_action(canvas, ("mouse", 0, 20, 3, True), scroll, 0))
        self.assertEqual(0, tool.mouse_action(canvas, ("mouse", 65, 20, 3, True), scroll, 0),
                         "wheel scrolls the viewport without changing selection")
        canvas = render()
        lines = canvas.render(False).splitlines()
        self.assertIn("job-3", lines[2])
        self.assertIn("job-5", lines[4], "counters must not take a data row")
        self.assertIn("↑ 3 above", lines[1])
        self.assertIn("↓ 2 more", lines[5])
        self.assertNotIn("more", lines[-1], "overflow belongs inside the table")
        self.assertEqual(3, tool.mouse_action(canvas, ("mouse", 0, 20, 2, True), scroll, 0))
        self.assertEqual(0, tool.mouse_action(canvas, ("mouse", 0, 20, 1, True), scroll, 0),
                         "clicking padding must not select a hidden row")
        self.assertEqual(0, tool.mouse_action(canvas, ("mouse", 0, 20, 2, False), scroll, 0))

    def test_a_delayed_mouse_release_cannot_become_the_copy_shortcut(self):
        tool = load_dashboard()
        read, write = os.pipe()
        try:
            os.write(write, b"\x1b[<0;14;10")
            self.assertEqual([], tool.read_keys(read))
            os.write(write, b"m")
            self.assertEqual([("mouse", 0, 13, 9, False)], tool.read_keys(read))
        finally:
            os.close(read)
            os.close(write)

    def test_sgr_mouse_reports_do_not_leak_into_keyboard_shortcuts(self):
        tool = load_dashboard()
        read, write = os.pipe()
        try:
            os.write(write, b"\x1b[<0;14;10M\x1b[<0;14;10m\x1b[<65;22;18Mq")
            self.assertEqual([("mouse", 0, 13, 9, True), ("mouse", 0, 13, 9, False),
                              ("mouse", 65, 21, 17, True), "q"], tool.read_keys(read))
        finally:
            os.close(read)
            os.close(write)


class IdleStatusTests(unittest.TestCase):
    def test_failed_process_sample_is_not_returned_as_zero_cpu(self):
        tool=load_dashboard()
        failure=subprocess.CompletedProcess(["ps"],1,stdout="",stderr="unavailable")
        with mock.patch.object(tool.subprocess,"run",return_value=failure):
            with self.assertRaises(OSError):
                tool.process_usage()


    def test_idle_duration_tracks_low_usage_and_resets_on_activity_or_a_new_grant(self):
        tool=load_dashboard()
        now=datetime.datetime.now(datetime.timezone.utc)
        item=dict(key=(12345,"lease"),label="build",kind="held",count=1,pid=12345,style="h0",cpu=10,
                  rss=1024,procs=1,granted=now-datetime.timedelta(minutes=10),since=now-datetime.timedelta(minutes=10),
                  queue="",cmd="build",tree=[])
        model=dict(holders=[item],waiters=[],slots=6,cores=12,updated=now)
        tracked={}
        tool.dashboard_idle(model,tracked)
        model["updated"]=now+datetime.timedelta(seconds=92)
        tool.dashboard_idle(model,tracked)
        canvas=tool.Canvas(90,24)
        tool.draw_detail(canvas,0,0,24,90,model,0,None,model["updated"])
        text=canvas.render(False)
        self.assertRegex(text, r"status\s+running · 1 slot · idle for 1m 32s")
        self.assertNotIn("idle while holding slots",text)
        item["cpu"]=50
        tool.dashboard_idle(model,tracked)
        self.assertIsNone(item["idle_since"])
        item["cpu"]=10
        model["updated"]=now+datetime.timedelta(seconds=100)
        tool.dashboard_idle(model,tracked)
        self.assertEqual(model["updated"],item["idle_since"])
        item["granted"]=now+datetime.timedelta(seconds=110)
        model["updated"]=item["granted"]
        tool.dashboard_idle(model,tracked)
        self.assertEqual(item["granted"],item["idle_since"])
        model["holders"]=[]
        tool.dashboard_idle(model,tracked)
        self.assertFalse(tracked)
        model["holders"]=[item]
        tool.dashboard_idle(model,tracked)
        tool.clear_idle(model,tracked)
        self.assertFalse(tracked)
        self.assertNotIn("idle_since",item)
        model["updated"]=now+datetime.timedelta(seconds=200)
        tool.dashboard_idle(model,tracked)
        self.assertEqual(model["updated"],item["idle_since"])


class DetailLayoutTests(unittest.TestCase):
    def test_waiting_facts_share_one_value_column_with_two_spaces_after_the_longest_label(self) -> None:
        tool = load_dashboard()
        moment = datetime.datetime(2026, 11, 5, 10).astimezone()
        item: dict[str, Any] = dict(label="first", kind="waiting", count=6, pid=12345,
                                   style="warn", since=moment, queue="", cmd="python -m work",
                                   tree=[], passable=True)
        model: dict[str, Any] = dict(holders=[], waiters=[item], free=4, used=2, slots=6,
                                    memory_block=None, order="window", start=None, limit=0)
        canvas = tool.Canvas(90, 24)
        with mock.patch.object(tool, "today", return_value=moment.date()):
            tool.draw_detail(canvas, 0, 0, 24, 90, model, 0, None, moment)
        lines = canvas.render(False).splitlines()
        for label, value in (("label", "first"), ("status", "waiting"), ("queued", "10:00:00"),
                             ("free", "4 of 6 slots"), ("pass until", "10:10:00"),
                             ("owner pid", "12345"), ("command", "python -m work")):
            line = next(row for row in lines if re.search(r"│  " + re.escape(label) + r"\s", row))
            self.assertEqual(15, line.index(value), line)
            self.assertTrue(line[line.index(label) + len(label):line.index(value)].startswith("  "), line)

    def test_process_table_shares_the_details_scroll_after_long_text(self):
        tool = load_dashboard()
        now = datetime.datetime.now(datetime.timezone.utc)
        item = dict(label="build", kind="held", count=2, pid=12345, style="h0", cpu=150,
                    rss=102400, procs=1, granted=now, since=now, queue="",
                    cmd="verify.sh " + "a-very-long-check-name," * 80,
                    meta="lane=build,ticket=42,phase=check," * 12,
                    tree=[(12345, 150, 102400, "worker")])
        canvas = tool.Canvas(70, 20)
        tool.draw_detail(canvas, 0, 0, 20, 70, {"holders": [item], "waiters": [], "cores": 12, "slots": 6},
                         0, "/project", now)
        lines = canvas.render(False).splitlines()
        self.assertIn("LEASE DETAILS", lines[0], "a running request's lease holds slots")
        lane = next(i for i,line in enumerate(lines) if re.search(r"label\s+build", cast(str, line)))
        self.assertRegex(lines[lane + 1], r"status\s+running")
        command = next(i for i,line in enumerate(lines) if "command" in line)
        meta = next(i for i,line in enumerate(lines) if "meta" in line)
        cwd = next(i for i,line in enumerate(lines) if "cwd" in line)
        self.assertLessEqual(meta-command, 3)
        self.assertLessEqual(cwd-meta, 3)
        self.assertIn("\x1b[0m 1.5 of 4 cores", canvas.lines(True)[next(i for i, line in enumerate(lines) if re.search(r"cpu\s", line))])
        self.assertNotIn("processes", canvas.tables)
        canvas = tool.Canvas(70, 20)
        canvas.scroll = {"details": 1000}
        tool.draw_detail(canvas, 0, 0, 20, 70, {"holders": [item], "waiters": [], "cores": 12, "slots": 6},
                         0, "/project", now)
        lines = canvas.render(False).splitlines()
        header = next(line for line in lines if "process" in line and "cpu" in line)
        self.assertEqual(3, header.index("process"))
        self.assertNotIn("processes", header)
        self.assertLess(header.index("process"), header.index("cpu"))
        self.assertLess(header.index("memory"), header.index("pid"))
        row = next(line for line in lines if "worker" in line)
        self.assertEqual(3, row.index("worker"))
        self.assertTrue(row.rstrip(" │").endswith("12345"))
        item["tree"] = [(1000+i, 0, 1024, f"worker{i}") for i in range(10)]
        canvas = tool.Canvas(70, 20)
        canvas.scroll = {"details": 1000}
        tool.draw_detail(canvas, 0, 0, 20, 70, {"holders": [item], "waiters": [], "cores": 12, "slots": 6},
                         0, "/project", now)
        text = canvas.render(False)
        self.assertIn("worker9", text)
        self.assertIn("above", text)
        self.assertNotIn("more", text)
        self.assertNotIn("more", text.splitlines()[-1])



class QueuePolicyTests(unittest.TestCase):
    def test_backfill_policy_is_structured_as_deadlines_not_a_sentence(self):
        tool = load_dashboard()
        tool.today = lambda: datetime.date(2026, 11, 5)
        since = datetime.datetime(2026, 11, 5, 10).astimezone()
        model = dict(holders=[], waiters=[dict(count=3, since=since, passable=False, label="head", pid=100,
                     kind="waiting",style="warn",cmd="work",queue="",tree=[])], free=1,used=1,slots=4,memory_block=None,
                     order="backfill", start=since+datetime.timedelta(minutes=30), limit=1200)
        self.assertEqual({"start est": "10:30:00", "pass until": "10:20:00"},
                         tool.queue_policy(model))
        canvas = tool.Canvas(100,24)
        tool.draw_detail(canvas,0,0,24,100,model,0,None,since)
        lines = canvas.render(False).splitlines()
        for title, value in (("start est", "10:30:00"), ("pass until", "10:20:00")):
            line=next(line for line in lines if title in line)
            self.assertRegex(line,re.escape(title)+r"\s+"+re.escape(value))
        model["waiters"] = []
        self.assertEqual({}, tool.queue_policy(model))


class SlotBarTests(unittest.TestCase):
    def test_free_label_is_right_aligned_and_leaves_room_for_the_last_owner(self):
        tool = load_dashboard()
        canvas = tool.Canvas(120, 3)
        tool.draw_slots(canvas, 0, 2, 116, dict(slots=6, used=1, waiters=[],
                        holders=[dict(count=1, kind="held", style="h0", label="lane-624-flows-are-graphs")]))
        lines = canvas.render(False).splitlines()
        self.assertIn("lane-624-flows-are-graphs", lines[1])
        self.assertEqual(lines[0].rindex("🮅") + 1, lines[1].index("free") + 4)

    def test_each_slot_is_solid_with_only_a_gap_between_slots(self):
        tool = load_dashboard()
        canvas = tool.Canvas(120, 3)
        tool.draw_slots(canvas, 0, 2, 116, dict(slots=3, used=2, waiters=[dict(count=3)],
                        holders=[dict(count=2, kind="held", style="h0", label="build")]))
        line = canvas.render(False).splitlines()[0]
        self.assertEqual("🮅" * 38 + "  " + "🮅" * 37 + "  " + "🮅" * 37, line[2:118])
        self.assertNotIn("▉", line)


class MachineStatusTests(unittest.TestCase):
    def test_memory_gate_first_lease_exception_is_consumed_by_a_waiter_ahead(self):
        tool=load_dashboard()
        settings=dict(sf_config.ORDER_DEFAULTS)
        waiter=dict(count=1,label="waiting",cmd="work",since=sf_config.now(),state="waiting")
        self.assertEqual(0,tool.dashboard_admission([waiter],[],6,0,"memory pressure",None,settings))
        self.assertEqual(6,tool.dashboard_admission([],[],6,0,"memory pressure",None,settings))
        self.assertEqual(5,tool.dashboard_admission([waiter],[],6,0,None,None,settings))
        waiter["priority"]="batch"
        self.assertEqual(6,tool.dashboard_admission([waiter],[],6,0,"memory pressure",None,settings),
                         "a normal probe ranked first may use the idle-host exemption")


    def test_compact_status_keeps_admission_visible(self):
        tool=load_dashboard()
        model=dict(admission=0,advice=dict(unleased_load=4.5),
                   memory=dict(level=2,swap_total=1024,swap_free=128))
        for width in (44,46):
            parts=tool.machine_status(model,width)
            text="".join(part for part,_ in parts)
            self.assertLessEqual(tool.text_width(text),width)
            self.assertIn("no admission",text)
            self.assertIn("swap low",text)


    def test_status_row_separates_memory_swap_and_real_admission_then_gaps_before_bars(self):
        tool=load_dashboard()
        now=datetime.datetime.now(datetime.timezone.utc)
        for level,free,admission,memory_word,swap_word in ((1,800,2,"memory ok","swap ok"),
                                                        (2,800,0,"memory low","swap ok"),
                                                        (1,128,0,"memory ok","swap low")):
            with self.subTest(level=level,free=free):
                canvas=tool.Canvas(110,24)
                model=dict(slots=6,cores=12,averages=(4,4,4),admission=admission,
                           advice=dict(unleased_load=4.5,jobs=1),memory_block=None,
                           memory=dict(level=level,used=768,total=1024,swap_total=1024,swap_free=free),
                           history=[(now-datetime.timedelta(hours=1),now,1)])
                tool.draw_machine(canvas,0,0,24,110,model)
                lines=canvas.lines(False)
                self.assertIn(memory_word,lines[2]);self.assertIn(swap_word,lines[2])
                self.assertIn("unleased load: 4.5",lines[2])
                self.assertIn(f"admission: {admission}" if admission else "no admission",lines[2])
                self.assertFalse(lines[3][1:-1].strip())
                self.assertIn("load 1m",lines[4])
                self.assertNotIn("advise",canvas.render(False))
                self.assertNotIn("memory_block",canvas.render(False))
                self.assertIn("38;5;255m",canvas.lines(True)[2])
                if not admission:self.assertIn("38;5;167m",canvas.lines(True)[2])


class MemoryBarTests(unittest.TestCase):
    def test_machine_has_usage_bars_and_a_chart_spanning_the_inner_width(self):
        tool = load_dashboard()
        now = datetime.datetime(2026, 1, 1, 12, tzinfo=datetime.timezone.utc)
        canvas = tool.Canvas(90, 17)
        tool.draw_machine(canvas, 0, 0, 17, 90, dict(slots=4, cores=12, averages=(1,2,3),
                          advice=dict(unleased_load=0, jobs=2), memory_block=None,
                          memory=dict(level=1, used=768, total=1024, swap_total=512, swap_free=128),
                          history=[(now-datetime.timedelta(hours=1), now, 4)]))
        lines = canvas.render(False).splitlines()
        text = "\n".join(lines)
        self.assertIn("used 768 MB of 1.0 GB", text)
        self.assertIn("used 384 MB of 512 MB", text)
        self.assertNotIn("load avg", text)
        self.assertNotIn("pressure", text)
        self.assertEqual(5,sum("█" in line for line in lines))
        first_chart = next(i for i, line in enumerate(lines) if "█" in line)
        self.assertFalse(lines[first_chart - 1][1:-1].strip(), "gap above chart")
        self.assertFalse(lines[-4][1:-1].strip(), "one gap row before legend")
        chart = lines[first_chart]
        self.assertEqual("█" * 84, chart[3:87])
        self.assertEqual("  ", chart[1:3])
        self.assertEqual("  ", chart[87:89])
        self.assertIn("held/4", lines[-3])
        self.assertIn("s/col", lines[-3])
        self.assertIn("avg 100%", lines[-3])
        self.assertIn("peak 4/4", lines[-3])
        self.assertIn("full 100%", lines[-3])
        self.assertFalse(lines[-2][1:-1].strip(), "one padding row after legend")
        self.assertIn("╰", lines[-1])


    def test_memory_bar_colour_reflects_pressure_without_a_separate_pressure_row(self):
        tool = load_dashboard()
        now = datetime.datetime.now(datetime.timezone.utc)
        for level, colour in ((1,75), (2,167), (4,167)):
            with self.subTest(level=level):
                canvas = tool.Canvas(90,24)
                tool.draw_machine(canvas,0,0,24,90,dict(slots=4,cores=12,averages=(1,2,3),
                                  advice=dict(unleased_load=0,jobs=2),memory_block=None,
                                  memory=dict(level=level,used=768,total=1024,swap_total=512,swap_free=128),
                                  history=[(now-datetime.timedelta(hours=1),now,4)]))
                row = next(i for i,line in enumerate(canvas.lines(False)) if "memory" in line and "used" in line)
                self.assertIn(f"\x1b[0;38;5;{colour}m", canvas.lines(True)[row])
                self.assertNotIn("pressure", canvas.render(False))

    def test_full_capacity_is_orange_even_in_a_one_row_histogram(self):
        tool=load_dashboard()
        now=datetime.datetime.now(datetime.timezone.utc)
        canvas=tool.Canvas(90,11)
        tool.draw_machine(canvas,0,0,11,90,dict(slots=4,cores=12,averages=(1,2,3),
                          advice=dict(unleased_load=0,jobs=2),memory_block=None,
                          memory=dict(level=1,used=768,total=1024,swap_total=512,swap_free=128),
                          history=[(now-datetime.timedelta(hours=1),now,4)]))
        rows=[i for i,line in enumerate(canvas.lines(False)) if "█" in line]
        self.assertEqual(1,len(rows))
        self.assertIn("38;2;255;175;95m",canvas.lines(True)[rows[0]])

    def test_macos_memory_counts_active_wired_and_compressor_physical_pages(self):
        tool = load_dashboard()
        sample = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages active: 100.
Pages inactive: 1000.
Pages wired down: 50.
Pages occupied by compressor: 25.
"""
        self.assertEqual(2.734375, tool.macos_used_memory(sample))
        self.assertIsNone(tool.macos_used_memory("no vm statistics"))


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tool = load_dashboard()

    def test_oldest_clock_bucket_retains_its_peak_until_the_next_scroll(self):
        seconds = int(datetime.datetime(2026,1,1,12,tzinfo=datetime.timezone.utc).timestamp())
        edge = datetime.datetime.fromtimestamp(seconds // 43 * 43, datetime.timezone.utc)
        oldest = edge-datetime.timedelta(seconds=3612)
        events = [dict(when=oldest+datetime.timedelta(seconds=5),delta=4),
                  dict(when=oldest+datetime.timedelta(seconds=6),delta=-4)]
        earlier = self.tool.held_history(events,0,edge-datetime.timedelta(seconds=10))
        later = self.tool.held_history(events,0,edge-datetime.timedelta(seconds=5))
        self.assertEqual(self.tool.history_buckets(earlier,84),self.tool.history_buckets(later,84))
        self.assertEqual(4,self.tool.history_buckets(later,84)[0])
        self.assertEqual(0,self.tool.history_summary(later,4)["peak"],
                         "the trailing-hour summary must exclude the older retained chart peak")

    def test_a_historical_peak_keeps_its_bucket_between_clock_boundaries(self):
        base = datetime.datetime(2026, 1, 1, 10, tzinfo=datetime.timezone.utc)
        rise = base + datetime.timedelta(seconds=601.2)
        fall = base + datetime.timedelta(seconds=601.4)
        def buckets(seconds):
            now = base + datetime.timedelta(seconds=seconds)
            steps = [(now - datetime.timedelta(hours=1), rise, 0),
                     (rise, fall, 4), (fall, now, 0)]
            return self.tool.history_buckets(steps, 6)
        self.assertEqual(buckets(3601), buckets(3602),
                         "refreshing within one time bucket must not move or widen a past peak")
        self.assertEqual([4, 0, 0, 0, 0, 0], buckets(3601))

    def test_held_slots_are_walked_back_from_now_and_a_waiter_giving_up_holds_nothing(self):
        directory = pathlib.Path(tempfile.mkdtemp(prefix="slotfather-history-"))
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        (directory / "queue.log").write_text(
            "2026-01-01T10:00:00Z granted 2 to build (pid 10)\n"
            "2026-01-01T10:10:00Z granted 1 to tests (pid 11)\n"
            "2026-01-01T10:20:00Z released 1 (0000000003-12.json)\n"
            "2026-01-01T10:30:00Z released 2 (0000000001-10.json)\n"
            "2026-01-01T10:40:00Z reaped held lease of 1 for tests (pid 11 exited)\n")
        events = self.tool.queue_events(directory)
        self.assertEqual(["granted", "granted", "left", "released", "reaped"], [e["verb"] for e in events])
        self.assertEqual("build", events[3]["label"], "a release is named by the grant to its pid")
        now = datetime.datetime(2026, 1, 1, 10, 50, tzinfo=datetime.timezone.utc)
        steps = self.tool.held_history(events, 0, now, window=3600)
        self.assertEqual([0, 2, 3, 1, 0], [level for _, _, level in steps])
        summary = self.tool.history_summary(steps, 4)
        self.assertEqual(3, summary["peak"])
        self.assertAlmostEqual((2 + 3 + 3 + 1) * 600 / 3600 / 4, summary["average"])
        self.assertEqual([0, 2, 3, 3, 1, 0], self.tool.history_buckets(steps, 6))


if __name__ == "__main__":
    unittest.main()
