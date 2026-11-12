# Godfather spec

Status: agreed plan, not built. Decisions from the owner's grilling session of 2026-11-12; the rest are marked
**(chosen here)** and can be revisited on the map issue.

## Problem

Max runs three small tools on his Mac: Slotfather (machine-wide capacity leases), Lanefather (parallel agent lanes:
claims, receipts, landing, review) and junkfather (owns the long-lived processes agents start and ends them). Only
Slotfather has a dashboard, and its terminal framework (frames, colours, overlays, hotkeys, mouse) lives inside
Slotfather. To see a lane, its leases and the processes it left behind, Max opens three CLIs and joins them by eye.
Anything a second tool wants on screen has to be built into Slotfather, which is the wrong owner, and every
Slotfather install, including the CI runner (VM 110), carries terminal code nobody there uses.

## Solution

Godfather is a separate package: one terminal dashboard for the three tools. It owns the TUI framework, moved out
of Slotfather unchanged. The tools become data sources behind four contracts: an append-only event log in a shared
schema, a `status --json` snapshot, a `settings` CLI, and a `capabilities --json` answer that says which of those a
tool supports. Godfather renders and asks; each tool keeps its state, validation and every mutation. The main screen
stays today's Slotfather dashboard; lanes and processes appear on it as border summaries and in the details, with
full pages one key away.

## Goals

- One dashboard for Slotfather, Lanefather and junkfather on the local machine.
- The TUI framework leaves Slotfather with no visible change: same layout, keys, colours, overlays and media.
- Every tool keeps working on its own at every step, with or without Godfather installed.
- Tools stay the owners: Godfather never writes their state files, never reimplements their validation, and only
  runs their CLIs.
- A tool that is absent, old or broken hides or greys its panels; it never breaks the screen.
- `host` is in every event from day one, so remote hosts can come later without a schema change.

## Non-goals

- Remote hosts (future work, below), a web UI, notifications, Windows.
- Land, promote, sync, done, review start or any other orchestrator step. Godfather never offers them.
- Editing a project's tracked configuration (`.lanefather.toml`), or any file a tool owns.
- Replacing the tools' CLIs or their text output; `slotfather stats` stays the place for history analysis.
- Installing Godfather on VM 110 or any CI runner.

## Behavior

### Install and start

1. `python3 install.py` from a checkout installs `godfather` into `~/.local/bin` as a thin entry point into an
   immutable release under `~/.local/lib/godfather/releases/`, the same pattern as Slotfather: Python 3.9 or later,
   standard library only, the interpreter that ran the installer, refused inside a virtual environment. A reinstall
   replaces the entry point atomically; a running dashboard keeps its release and shows `↻` in the footer.
2. `godfather` opens the live dashboard in a terminal; without a terminal it prints usage and exits 2.
   `godfather --once [--page overview|lanes|processes] [--width N] [--height N] [--color]` prints one frame and
   exits, as `slotfather dashboard --once` does today. `--interval S` sets Refresh every for this run.
3. `slotfather dashboard [ARGS]`, and a bare `slotfather` in a terminal, exec `godfather` with the same arguments
   when `godfather` is on `PATH`. Without it they print
   `slotfather: the dashboard is part of godfather now; install it from https://github.com/maxvyshniakov/godfather`
   and exit 2. **(Chosen here: a message, not a fallback copy.)** The Slotfather release that makes this switch ships
   only after Godfather is installed on the Mac, so no machine where someone looks at a dashboard loses it, and no
   second copy of the TUI has to be kept in step. VM 110 gets the message, which nobody there reads.
4. With no tool found, Godfather shows one panel naming the tools it looked for and how each was looked up.

### Discovery

5. At start, and again when a tool's resolved executable changes (checked every 10 s) or every 60 s, Godfather runs
   `<tool> capabilities --json` for `slotfather`, `lanefather` and `junkfather` found on `PATH`, with a 2 s timeout.
6. A tool that is not on `PATH` is absent: its panels, summaries, pages, Settings section, event source and actions
   are hidden, with no error.
7. A tool that is present but whose capabilities fail, time out, lack a `dashboard` block or name only contract
   versions Godfather does not know is **unsupported**: hidden like an absent tool, and Settings' STATUS says why
   (`lanefather 0.3.0: no dashboard contract; update lanefather`, or `needs a newer godfather`).
8. A tool upgraded while Godfather runs is picked up by the next discovery without a restart.

### Snapshots

9. Each source has one background worker that runs its snapshot command, one call at a time, newest result wins.
   Slotfather is polled at Refresh every (default 1 s, as today) **(chosen here: the main screen's CPU bars and
   timers need today's cadence; the 5–10 s rule applies to the other tools)**; Lanefather and junkfather every
   5 s (their capabilities may ask for up to 10 s). An event from a source triggers that source's next poll at once,
   debounced by 250 ms.
10. A snapshot call that fails, times out (5 s) or returns an unknown version leaves the last good snapshot on
    screen, dimmed, with `stale since 04:25` in the panel's border; the error is in Settings' STATUS. The screen
    never goes blank because of one source.
11. Selecting, scrolling, switching pages and opening overlays never run a mutating command.

### Overview (the main screen)

12. The overview is today's Slotfather layout and polish, unchanged: SLOTS, QUEUE, DETAILS, EVENTS and MACHINE, the
    wide and one-column layouts, focus, Tab order, scrollbars, emoji and ASCII fallbacks, colours and theme modes,
    the footer, help, changelog and Settings overlays, any keyboard layout, mouse and Shift+drag selection.
13. SLOTS' bottom border carries the summaries of the other tools, each led by its page key:
    `2 lanes 3 active · 2 reviews · 🔥 gate red` on the left and `3 procs 7 · 1 orphan` on the right.
    - lanes: `N active` (lanes in status active), `N planned` when any, `N reviews` (lanes whose review runs or has
      unresolved findings), `🔥 gate red` when the newest batch gate of any project is red.
    - procs: `N` running owned entries, then `N orphan` (owner gone), `N expired` (TTL passed, not reaped yet),
      `N unowned` (the last sweep's report), each only when not zero.
    - A narrow border drops, in order: planned, reviews, unowned, expired; the leading count and any 🔥 stay. An
      absent tool's summary is not drawn; a stale one is dimmed.
    - A click on a summary opens its page.
14. DETAILS for a selected request keeps today's rows and lane context, and adds:
    - `lane` with the lane id and project, a link to the Lanes page with that lane selected;
    - `processes`: one row per junkfather entry that belongs to the lease (below), with label, state and TTL left,
      each a link to the Processes page with that entry selected; `none owned` when there are none.
15. EVENTS becomes one feed of every source (slice 3): columns time, source, event, slots, detail, label and pid.
    - The source is a four-cell tag from the tool's capabilities (`slot`, `lane`, `junk`) in a colour of its own.
    - `slots` is filled only for Slotfather events, as today (`▪` taken, `▫` handed back or never held).
    - The detail comes from the event itself; Godfather no longer joins `history.jsonl`.
    - `f` cycles the source filter (all, then each present source); `/` edits a text filter matched against kind,
      subject, label and detail; Esc in the field clears it. The active filter shows in EVENTS' top border.
    - Newest first; it stays on the newest entry while scrolled to the top and counts new ones (`↑ 3 new`) while
      scrolled down, as today. Events with the same time keep their file order.
    - On a narrow panel the pid goes first, then slots, then the source shrinks to a one-cell coloured mark; the
      label is shortened before any of them.

### Pages

16. `1` shows the overview, `2` the Lanes page, `3` the Processes page; Esc on a page returns to the overview.
    A page key whose tool is absent does nothing, and its hint is not shown. Opening a page from the overview selects
    the row linked to the selected request, when there is one.
17. Lanes page: a LANES table, grouped by project under a project row that is not selectable (`Straylight · 3 of 4
    lanes`), with columns state, lane, ticket, stage, receipt, review, claims, age; LANE DETAILS for the selection
    (project, ticket link, branch and base, worktree link, claims, after, receipt and tree match, review and finding
    counts, latest log, its leases from the Slotfather snapshot and its processes from junkfather, each a link); and
    an EVENTS strip filtered to that lane.
18. Processes page: a PROCESSES table of owned entries (running, orphan, expired), then a separator and the unowned
    processes the last sweep reported; columns state, id, label, owner, lane, age, TTL left, pid, processes;
    PROCESS DETAILS (command, directory, log, owner and whether it lives, keep, mode, its lease and lane as links);
    an EVENTS strip filtered to that entry.
19. Joins, all read-only and computed by Godfather from snapshots:
    - request ↔ lane: the request's `meta.lane` and `meta.lanefather.state` match a lane id in the project whose state
      directory that is; a request with only `meta.lane` matches by its working directory inside the lane's
      worktree; otherwise no link (never by id alone across projects);
    - request ↔ entry: an entry whose leader or a known member pid is in the request's process tree, or whose lane
      matches the request's lane;
    - lane ↔ entry: the entry's lane equals the lane id and its directory is inside the lane's worktree.

### Settings

20. `s` opens Settings, with today's interaction (pending value, one-second settle, Enter, Esc, `d`, `D` twice, the
    STATUS block, click and wheel rules). In slice 1 it shows exactly today's rows, read and written through
    `slotfather settings`. From slice 6 it has one section per present tool plus this dashboard's own (Refresh every,
    Colour theme); `D` twice resets the settings of the selected section only **(chosen here)**.
21. Every value shown comes from the tool's `settings --json`; every change goes through `settings set`. A value the
    tool marks read-only (set by the environment, a project file or another user's queue) shows its source and
    cannot be edited. A tool's refusal, conflict or invalid value is shown in red under the row and nothing is saved.
22. Godfather's own settings (Refresh every, Colour theme) live in `~/.config/godfather/settings.json`.
    `GODFATHER_THEME` sets the starting theme; `SLOTFATHER_THEME`, `SLOTFATHER_TERMINAL_BG` and `_FG` keep working
    as fallbacks for `GODFATHER_THEME`, `GODFATHER_TERMINAL_BG` and `_FG`.

### Actions

23. Enter or `a` on a selected row, or a right-click, opens the row's action menu: only the actions the owner tool
    advertises in its capabilities and that apply to the row's state. No menu opens when none apply.
24. An action runs the owner tool's CLI with an argument vector (never a shell), a 30 s timeout and
    `GODFATHER_ACTOR=godfather/<version>` in its environment. The result is one footer line: the tool's own message
    on success, its first stderr line in red on failure. Godfather never retries and never reports success for a
    non-zero exit. The source is polled right after.
25. An irreversible action opens a confirmation overlay naming the action, the target (label, id, pid, owner) and
    the exact command; Esc or `n` cancels, `y` confirms; nothing is preselected to confirm. A park asks for a reason
    first.
26. The actions table is in Decisions. Land, promote, done and sync are never offered, whatever a tool advertises.

### Events (tools' side)

27. Each tool appends one JSON line per event to its own log (schema below) after the change it describes has
    happened. An event that cannot be written never fails or alters the action; an action that did not happen never
    gets an event.
28. Slotfather keeps writing `queue.log` and `history.jsonl`; Lanefather keeps writing `ledger.log`. The event log
    mirrors them at the same call sites. Retiring the text logs is not part of this plan.

## Decisions

### Package and modules

Godfather is one Python package, standard library only, Python 3.9+, installed by its own `install.py`. Its modules:

- **tui**: the framework moved from Slotfather's dashboard: canvas, frames and titles set into borders, tables and
  column sharing, scrollbars, overlays (help, changelog, Settings, confirmation, action menu, text field), the footer
  and hint dropping, key reading with the kitty keyboard protocol and layout table, mouse (SGR 1000/1006), full-screen
  setup and restore, the background loader. It knows nothing about any tool.
- **theme**: Slotfather's theme module moved whole: tokens, terminal colour queries (OSC 10/11/4, mode 2031), muted
  and surface mixing, holder fills and inks, the chart ramp, the three modes. Source colours for the EVENTS tag are
  new tokens chosen by the same rules as holder fills.
- **sources**: discovery, and one `Source` per tool: capabilities, the snapshot worker, the event tailer, the
  settings client and the action runner. All tool I/O is here; everything else reads `Source` state.
- **events**: the schema version, the tail reader (inode and offset, rotation to `.1`, partial last lines), the
  merged feed and per-source formatters that turn a kind and its details into the event and detail columns.
- **eventcheck**: the validator, one file with no imports outside the standard library, meant to be vendored.
- **adapters**: one per tool; each checks the snapshot versions it supports, turns a snapshot into the render model
  and computes its border summary and its side of the joins.
- **views**: overview (SLOTS, QUEUE, DETAILS, EVENTS, MACHINE renderers, moved), lanes, processes.
- **fixtures**: `GODFATHER_FIXTURES=DIR` replaces discovery and every tool call with recorded capabilities,
  snapshots, event logs and settings replies, and records the argv of actions instead of running them. Tests,
  `--once` goldens and the README media use it.

```python
class Source(Protocol):
    name: str                      # "slotfather"
    tag: str                       # "slot": the EVENTS source tag
    state: str                     # "absent" | "unsupported" | "ok" | "stale"
    problem: str | None            # why unsupported or stale, for Settings' STATUS
    snapshot: dict | None          # the last good snapshot, as the tool printed it
    snapshot_at: float | None
    def events_since(self, cursor: Cursor) -> tuple[list[Event], Cursor]: ...
    def settings(self) -> dict: ...                       # settings --json
    def check(self, key: str, value: str) -> dict: ...    # settings check
    def set(self, key: str, value: str, revision: str | None) -> dict: ...
    def actions(self, row: dict) -> list[Action]: ...     # advertised and applicable
    def run(self, action: Action) -> Result: ...
```

Slotfather keeps building the dashboard's model, because it is scheduler logic (plan, admission, reserved start,
served class, draining, occupancy); it prints it as JSON. Godfather only renders it. Rejected: Godfather importing
Slotfather's modules from its installed release (a private coupling to another package's internals, and it breaks
when the two are upgraded apart); Godfather reading the tools' state files (it would have to reimplement locking and
validation).

### Event schema, version 1

The canonical description is the JSON Schema and the validator in this repository; this is the draft.

One UTF-8 JSON object per line, at most 16 KiB with its newline.

| Field | Required | Rule |
| --- | --- | --- |
| `v` | yes | `1`. |
| `time` | yes | When it happened, RFC 3339 UTC with milliseconds: `2026-11-12T02:24:16.204Z`. |
| `host` | yes | The machine's short host name, lowercase (`socket.gethostname()` up to the first dot). |
| `source` | yes | The tool's name: `slotfather`, `lanefather`, `junkfather` (`[a-z][a-z0-9-]{0,31}`). |
| `kind` | yes | `noun.verb`, lowercase, dotted, at most 64 characters, from the tool's documented list. |
| `subject` | yes | What it is about: an object with `type` and `id` (strings), optionally `label`, `project`, `pid` and other scalars. |
| `actor` | yes | Who caused it: an object with `type` (`agent`, `process`, `user` or `tool`), optionally `session`, `agent`, `pid`, `lane`, `name`, `via`. |
| `details` | yes | An object, possibly empty, specific to the kind; nesting at most four levels. |
| `level` | no | `info` (default), `warn` or `error`. |
| `id` | no | A unique id within the source, at most 64 characters. |

- Strings carry no control characters (no escape sequences reach a terminal). No environment contents, tokens or
  secrets; a command line is cut to 400 characters.
- Readers ignore unknown fields and skip lines with an unknown `v` or that fail validation, counting them; the
  validator's strict mode, which the tools' tests use, also refuses unknown top-level fields.
- Writers append each line with one `write` on a file opened with `O_APPEND`, mode 0600. Past 4 MiB the log is
  renamed to `events.jsonl.1`, replacing the previous one, under an `flock` on `events.lock` beside it. Readers
  follow the rename by inode.
- Each log sits in the tool's state directory as `events.jsonl`; the tool's capabilities give its absolute path.
  Lanefather writes one machine-wide log for every project **(chosen here)**, with `subject.project`, so Godfather
  tails one file per tool.
- Actions that Godfather runs carry `actor.via = "godfather"` where the tool reads `GODFATHER_ACTOR`.

Example events, one per line in a real log, wrapped here:

```json
{"v": 1, "time": "2026-11-12T02:24:16.204Z", "host": "mbp", "source": "slotfather", "kind": "lease.granted",
 "subject": {"type": "request", "id": "0000009701-51587", "label": "lane-skeleton-parity", "pid": 51587},
 "actor": {"type": "process", "pid": 51587, "lane": "skeleton-parity"},
 "details": {"count": 1, "priority": "interactive", "waited": 0, "meta": {"lane": "skeleton-parity", "phase": "check"}}}

{"v": 1, "time": "2026-11-12T02:24:38.611Z", "host": "mbp", "source": "slotfather", "kind": "lease.released",
 "subject": {"type": "request", "id": "0000009701-51587", "label": "lane-skeleton-parity", "pid": 51587},
 "actor": {"type": "process", "pid": 51587},
 "details": {"count": 1, "exit": 0, "ran": 22, "cpu": 4.23, "rss_peak_mb": 150.9}}

{"v": 1, "time": "2026-11-12T03:10:02.007Z", "host": "mbp", "source": "slotfather", "kind": "request.reprioritized",
 "subject": {"type": "request", "id": "0000009720-60111", "label": "data-grid", "pid": 60111},
 "actor": {"type": "process", "pid": 70210, "via": "godfather"},
 "details": {"from": "batch", "to": "interactive"}}

{"v": 1, "time": "2026-11-12T02:05:38.000Z", "host": "mbp", "source": "lanefather", "kind": "lane.post_merge",
 "subject": {"type": "lane", "id": "710-speech-paste-phase-review-fixes", "project": "Straylight", "ticket": "710"},
 "actor": {"type": "agent", "session": "4bceaaaf-85b1-4d87-9eda-b82cdaea5c12", "pid": 81234},
 "details": {"status": "green", "scope": "narrowed", "jobs": 2, "suites": ["tooling", "native", "desktop"],
             "merge": "cedf8b972dd8ed11d8af0725f0a9bcb5b2d0ba35"}}

{"v": 1, "time": "2026-11-12T04:06:19.512Z", "host": "mbp", "source": "lanefather", "kind": "gate.finished",
 "level": "error",
 "subject": {"type": "commit", "id": "cedf8b972dd8ed11d8af0725f0a9bcb5b2d0ba35", "project": "Straylight"},
 "actor": {"type": "agent", "session": "4bceaaaf-85b1-4d87-9eda-b82cdaea5c12", "pid": 88917},
 "details": {"status": "red", "jobs": 1, "seconds": 1177}}

{"v": 1, "time": "2026-11-12T04:40:00.120Z", "host": "mbp", "source": "junkfather", "kind": "entry.owned",
 "subject": {"type": "entry", "id": "j3f9a1c", "label": "vite dev", "pid": 90412},
 "actor": {"type": "agent", "session": "4bceaaaf-85b1-4d87-9eda-b82cdaea5c12", "agent": "a71c", "lane": "710-speech-paste-phase"},
 "details": {"argv": ["bun", "run", "dev"], "ttl": 7200, "keep": "owner", "mode": "detached"}}

{"v": 1, "time": "2026-11-12T06:40:00.884Z", "host": "mbp", "source": "junkfather", "kind": "entry.reaped",
 "subject": {"type": "entry", "id": "j3f9a1c", "label": "vite dev", "pid": 90412},
 "actor": {"type": "tool", "name": "junkfather", "via": "sweep"},
 "details": {"reason": "ttl expired", "result": "stopped", "signals": ["TERM"]}}
```

First kinds per tool (each tool documents and owns its list):

- Slotfather: `lease.granted`, `lease.released`, `request.left`, `lease.reaped`, `request.reaped`,
  `request.reprioritized`, `request.waiting_long`, `lease.yield_asked`, `lease.yielded`, `capacity.changed`,
  `order.changed`.
- Lanefather: `lane.started`, `lane.adopted`, `claims.added`, `lane.checked`, `lane.landed`, `lane.conflict`,
  `lane.post_merge`, `lane.done`, `lane.parked`, `review.started`, `review.finished`, `review.marked`,
  `gate.finished`, `promote.step`, `sync.finished`, `perf.finished`, `load.waited`, `landing.lock_taken`,
  `override` (every `--force-reason`, level warn).
- junkfather: `entry.owned`, `entry.adopted`, `entry.exited`, `entry.extended`, `entry.handed`, `entry.reaped`,
  `sweep.finished`, `unowned.reported`, `hook.failed`.

**Validator.** `eventcheck.validate(obj, strict=False) -> list[str]` returns the problems, empty when valid;
`python3 eventcheck.py [--strict] FILE...` checks logs and exits 1 on any problem. The tools' tests import this one
file from `common/eventcheck.py` in the fathers repository (until the move they vendored it with its SHA-256). The
file carries an MIT header, so another project may copy it unchanged.

### Capabilities contract

Every tool answers `<tool> capabilities --json` with an object that has a `dashboard` block. Lanefather already has
`capabilities --json` and adds the block; Slotfather and junkfather add the command.

```json
{"v": 1, "tool": "slotfather", "version": "0.2.0",
 "dashboard": {"v": 1, "tag": "slot",
   "snapshot": {"argv": ["status", "--json", "--full"], "versions": [2], "poll": 1},
   "events": {"path": "/Users/me/.local/state/slotfather/events.jsonl", "versions": [1]},
   "settings": {"versions": [1]},
   "actions": ["reprioritize", "ask-yield"],
   "changelog": "/Users/me/.local/lib/slotfather/releases/0.2.0-1a2b3c/CHANGELOG.md"}}
```

- `argv` runs after the executable Godfather found; `poll` is a hint in seconds that Godfather clamps (1–10 for
  Slotfather, 5–10 for the others).
- Missing `events`, `settings`, `actions` or `changelog` means unsupported; the matching feature is hidden for that
  tool.
- The changelog overlay gets one tab per tool that names a changelog, after Godfather's own **(chosen here: the
  footer now says Godfather, and Slotfather's release notes stay one key away)**.

### Snapshot contract

Common to every tool: `<tool> <argv>` prints one JSON object on stdout and exits 0, needs no terminal, never
prompts, takes no lock longer than its tool's own `status` does, and stays under 4 MiB. The object has `v` (the
tool's response version), `host` and `time`. Anything else is the source's error.

**Slotfather**: `slotfather status --json --full` is today's `status --json` (version 2, unchanged for older readers)
plus a `dashboard` block holding what the main screen shows: capacity and its basis, the slots in use, admission,
unleased load and memory facts; per request the process rows (pid, name, cpu, rss) and totals, the yield asked and by
whom, the served class and reserved start; the queue order settings; occupancy steps for the chart; and, until
Godfather reads events (slice 3), the last 60 described events. The block carries its own `v`. Like `status`, it may
reap dead entries; nothing else changes state.

**Lanefather**: `lanefather status --json` is new and machine-wide: every project in a registry Lanefather keeps under
its default state directory, which each command that loads a project config updates (state directory, config path,
repository).

```json
{"v": 1, "host": "mbp", "time": "2026-11-12T04:26:00Z",
 "projects": [
   {"project": "Straylight", "state_dir": "/Users/me/.local/state/straylight-orchestration/lanes",
    "repository": "maxvyshniakov/straylight", "cap": 4, "cap_source": "derived", "active": 3,
    "gate": {"sha": "cedf8b97…", "status": "red", "at": "2026-11-12T04:06:19Z"},
    "lanes": [{"id": "710-speech-paste-phase", "status": "active", "ticket": {"number": "710", "url": "…"},
               "branch": "lane/710-speech-paste-phase", "base": "integrate/queue-347", "worktree": "/…",
               "claims": ["desktop/src/**"], "after": [], "created": "…",
               "receipt": {"status": "green", "tree_match": true},
               "review": {"status": "running", "open": 2},
               "logs": {"check": "/…/check-20261112T040619.log"},
               "outcome": null}]},
   {"project": "data-grid", "state_dir": "/…", "error": "config not found at /…/.lanefather.toml"}]}
```

A project that cannot be read gives an `error` entry, never a failed snapshot.

**junkfather**: `junkfather status --json` is `list --json` (running entries) plus the unowned processes from the
last sweep (pid, start, command, directory, root, age) and the sweep's last time and result.

### Settings contract

```text
<tool> settings --json                                  the schema and current values
<tool> settings check KEY VALUE --json                  validate without saving; a preview where there is one
<tool> settings set KEY VALUE [--revision R] --json     save; refused if R no longer matches
<tool> settings reset (KEY | --all) [--revision R] --json
```

```json
{"v": 1, "tool": "slotfather", "writable": true, "reason": null,
 "status": [{"label": "queue", "value": "~/.local/state/slotfather"},
            {"label": "slots", "value": "8 total · 3 free · 5 held · 0 waiting"}],
 "sections": [{"id": "capacity", "title": "Capacity", "settings": [
   {"key": "capacity.mode", "label": "Capacity", "type": "choice", "value": "fixed", "default": "automatic",
    "choices": [{"value": "automatic", "label": "Automatic"}, {"value": "fixed", "label": "Fixed"},
                {"value": "memory", "label": "Memory"}, {"value": "lower", "label": "CPU + memory"}],
    "source": "saved", "writable": true, "revision": "sha256:9c1…", "description": "How the slot count is found."},
   {"key": "order.warn_wait", "label": "Warn after waiting", "type": "duration", "value": 1800, "default": 1800,
    "min": 0, "max": 86400, "step": 300, "zero": "never", "effective": 900, "source": "env SLOTFATHER_WARN_WAIT",
    "writable": true, "revision": "sha256:4d0…", "description": "…"}]}]}
```

- Types: `choice`, `integer`, `number`, `duration` (seconds), `boolean`, `text`; numbers carry `min`, `max`, `step`
  and `unit`; `zero` names what 0 means.
- `effective` is a value the environment tightens (today's `effective here:`); `source` says where the value comes
  from; a row with `writable: false` is shown, never edited.
- `revision` is opaque and per group of settings the tool saves together; `set` with a stale revision exits 3 and
  prints the current settings, which Godfather shows (today's capacity conflict). Without `--revision` the tool
  merges, as Slotfather's order settings do today.
- `check` returns `{"ok": bool, "error": str|null, "preview": str|null}`; the preview carries texts such as
  `will drain 2` or `will start 1 waiting`. Godfather calls it for each pending or typed value.
- Exit codes: 0 done, 2 invalid (the reply says why), 3 conflict, 4 read-only.
- Lanefather exposes only machine-local settings; values from a project's `.lanefather.toml` or the environment are
  read-only rows with their source.

### Actions

| Row | Action | Owner command | Confirm |
| --- | --- | --- | --- |
| QUEUE: one of your requests | Reprioritize to interactive, normal or batch | `slotfather reprioritize PID CLASS --start START` | no |
| QUEUE: a running yieldable lease of a lower class than a waiter | Ask to yield | `slotfather ask-yield PID --start START` (new) | no |
| PROCESSES: an orphan or expired owned entry | Reap | `junkfather reap --id ID --reason "godfather: orphan"` | yes |
| PROCESSES: an unowned process the last sweep reported | Reap | `junkfather reap --pid PID --force --reason "godfather: unowned"` | yes |
| PROCESSES: a running owned entry | Extend TTL by 1 h, 4 h or 1 d | `junkfather extend ID --ttl D` | no |
| LANES: a lane with a ticket | Open the ticket | the platform opener (`open`, `xdg-open`) on the snapshot's URL | no |
| LANES: a lane with a log | Open the latest log | `$PAGER` (default `less -R +G`), the TUI suspended and restored | no |
| LANES: a lane with a worktree | Open the worktree | the platform opener on the path | no |
| LANES: an active lane | Park, with a reason | `lanefather park ID --reason R` (lanefather#19) | yes |

`ask-yield` and `--start` on `reprioritize` are new in Slotfather (SF-5); `--start` refuses when the pid now
belongs to another process. Ask to yield is not confirmed **(chosen here)**: nothing is stopped, the holder yields between its own steps and goes
back to the queue, and Slotfather already asks on its own after two minutes. Reprioritize and extend are reversible.
Opening is local and read-only, so it needs no owner command. Park is offered only once Lanefather advertises it.
Land, promote, done, sync, review and sweep are never offered. A live owned entry cannot be reaped from Godfather;
that is its owner's to end.

### Layout

```
╭─ SLOTS ─────────────────────────────────────────── 12 cores · 8 slots (manual) ─╮ ╭─ LEASE DETAILS ──────────────╮
│  🮅🮅🮅🮅🮅🮅🮅🮅  🮅🮅🮅🮅🮅🮅🮅🮅  ╭──────╮  ╭──────╮                                        │ │  label     lane-710-check    │
│  lane-710   data-grid  free                                                     │ │  lane      710-speech-paste… │
╰─ 2 lanes 3 active · 2 reviews · 🔥 gate red ──────────── 3 procs 7 · 1 orphan ─╯ │  processes vite dev · 1h12m  │
╭─ QUEUE ──────────────── slots ── cpu use/share ──────────── time ──────── pid ─╮ │  …                           │
│  ▶️ ⚡ lane-710-check   ▪   ■■■░░░░░░░ 1.2/1.5   19m37s   88917                   │ ╰──────────────────────────────╯
╰────────────────────────────────────────────────────────────────────────────────╯ ╭─ MACHINE ────────────────────╮
╭─ EVENTS ── all sources ── source ─ event ─ slots ─ detail ─ label ─────── pid ─╮ │  …                           │
│  04:25:48  slot  granted        ▪   no wait          lane-710-check   59827     │ │                              │
│  04:25:40  lane  checked            green, 2 suites  710-speech-…               │ │                              │
│  04:25:12  junk  reaped             ttl expired      vite dev         90412     │ │                              │
╰────────────────────────────────────────────────────────────────────────────────╯ ╰──────────────────────────────╯
 Godfather 0.1.0        ↑↓ select  tab panel  1 2 3 pages  s settings  ? help  q quit        updated 04:25:56  ● live
```

The footer's hints drop as today, `1 2 3 pages` right after help. Pages reuse the overview's frames: a table on the
left, its details on the right, an EVENTS strip under the table.

### Remote hosts (future work)

`host` is in every event and snapshot so a later version can show other machines (VM 110, the Link VM) through
the same contracts over SSH. Nothing in this plan reads or runs anything remotely, and no ticket builds it.

## Migration path

Every step leaves every tool working on its own.

1. **Additive Slotfather CLI** (cross-repo SF-1, SF-2): `capabilities --json`, `status --json --full` and the
   `settings` commands. The in-tree dashboard is untouched.
2. **Godfather 0.1.0** (slice 1): the TUI moved, the schema and validator, fixtures and media. It reads Slotfather
   only through the new commands. Installed on the Mac; the owner checks it beside `slotfather dashboard`.
3. **The switch** (SF-3): `slotfather dashboard` execs Godfather or prints the install message; Slotfather drops its
   dashboard, Settings panel, theme and keys modules and the media tooling, and its README keeps a copy of
   Godfather's media. Released only after step 2 is installed.
4. **Event logs** (slice 2; SF-4, LF-1): both tools mirror their text logs into `events.jsonl`. Nothing reads them yet.
5. **Merged EVENTS** (slice 3): Godfather reads events; Slotfather's transitional `dashboard.events` goes in its next
   release after that Godfather is installed.
6. **Lanes** (slice 4; LF-2), **Processes** (slice 5; JF-1), **Settings sections** (slice 6; LF-3, JF-2),
   **actions** (slice 7; SF-5, lanefather#19): each adds a page or section that stays hidden until its tool supports
   the contract.

### Cross-repo tickets

Slotfather and Lanefather are public; their tickets are filed there by the owner. junkfather has no repository yet.

| Id | Tool | What | Needed by |
| --- | --- | --- | --- |
| SF-1 | Slotfather | `capabilities --json` with a `dashboard` block; `status --json --full` | slice 1 |
| SF-2 | Slotfather | `settings --json`, `check`, `set --revision`, `reset` | slice 1 |
| SF-3 | Slotfather | `dashboard` execs Godfather or prints the install message; the in-tree dashboard goes | after slice 1 is installed |
| SF-4 | Slotfather | `events.jsonl` mirroring `queue.log`, tested with the vendored validator | slice 2 |
| SF-5 | Slotfather | `ask-yield PID --start START`; `--start` on `reprioritize` | slice 7 |
| LF-1 | Lanefather | machine-wide `events.jsonl` mirroring `ledger.log`; `dashboard` block in capabilities | slice 2 |
| LF-2 | Lanefather | machine-wide `status --json` over a project registry | slice 4 |
| LF-3 | Lanefather | `settings` commands for machine-local settings | slice 6 |
| JF-1 | junkfather | the shared event schema; `capabilities --json`; `status --json` | slice 5 |
| JF-2 | junkfather | `settings` commands | slice 6, optional |
| lanefather#19 | Lanefather | `park` and `unpark` | slice 7, park only |

## Seams

- **`godfather --once` under `GODFATHER_FIXTURES`**: rendered frames compared with golden text files, with and
  without colour, wide and narrow. Slotfather's current goldens (the Settings snapshots and the dashboard tests'
  expectations) are the first goldens, unchanged except the footer's name and version.
- **The live dashboard in a PTY under fixtures**: keys, mouse and resize, as Slotfather's live tests do today;
  actions are recorded as argv, never run.
- **Source contract tests**: Godfather's adapters against each real tool's CLI in isolated state directories
  (`SLOTFATHER_DIR`, `LANEFATHER_STATE`, `JUNKFATHER_STATE`), run when a checkout or install is present and skipped
  otherwise.
- **The validator in each tool's tests**: run the tool's real commands in a temporary state directory, then validate
  every line of its `events.jsonl` in strict mode.

## Out of scope

- Remote hosts and any SSH access.
- Retiring `queue.log`, `history.jsonl` or `ledger.log`, and the orchestration metrics that read the ledger.
- Configuring which tools Godfather looks for; the three names are built in.
- Event history beyond what the tailed logs and their `.1` hold.
- Making the repository public, and publishing Godfather to a package registry.

## Open risks

1. **Slice 1 is large**: about 4,000 lines move, and Slotfather needs three new commands. The ticket orders its steps
   so each is checkable; it may still take two sessions.
2. **Process cost**: polling `slotfather status --json --full` every second adds an interpreter start to today's
   in-process look (about 0.4 s of work). Slice 1 measures it; above 1.5× today's dashboard CPU, the fix is a longer
   default poll for the snapshot with timers still ticking locally, decided with the owner.
3. **Version skew**: Godfather and the tools are installed apart. Every snapshot and settings reply carries its
   version, and an unknown one is shown as unsupported, never guessed at.
4. **Lanefather's project registry**: projects that moved or vanished. Each gives an error entry, and `lanefather
   status` drops one whose config has been missing for 7 days.
5. **Lane ids repeat across projects**: every join that involves a lane uses the project's state directory or the
   worktree path, never the id alone.
6. **Appends larger than a pipe buffer** are not atomic by POSIX; they are on local APFS and ext4 with `O_APPEND` and
   one `write`, and the reader skips a torn line instead of failing. Lines stay under 16 KiB.
7. **Two logs per call site drift**: each tool routes both lines through one function (Lanefather's ledger writer
   takes the kind, subject and details and writes the ledger text and the event), and its tests compare the two.
8. **A private validator inside public repositories**: vendoring publishes that file. It carries its own MIT header.
9. **pid reuse between snapshot and action**: every action passes the start time or entry id the snapshot showed,
   and the tool checks it again before acting.
10. **junkfather is being built now**: its contract (JF-1) should land before its first release, so it never has
    to migrate its event log.
11. **Event logs hold commands and paths**: they are created mode 0600 in the user's own state directories.
