# Godfather Changelog

Versions and release rules: [README.md](README.md#versions).

## Unreleased

### Added

- EVENTS rows are selectable (fathers#152): with EVENTS focused, ↑ ↓ or a click move a row cursor (the `┃` bar
  QUEUE's selection has) and DETAILS shows that run or event instead of QUEUE's entry: a run's label, slots, wait,
  priority, run time, outcome and pid with each mark explained (`▲ outlier · ran 1m13s · its run type's p90 52s`,
  `↗ slower +38% than its run type's p50` and why), another event's tool, kind, subject and fields. The cursor on
  the newest row follows new rows; on an older one it stays put and `↑ N new` counts what arrived; Home goes back.

- Settings has a section per present tool (fathers#87): this dashboard's own (Refresh every, Colour theme) first,
  then Slotfather's, Lanefather's and Junkfather's, each its read-only status rows and its settings, read with
  `<tool> settings --json` (Slotfather's every second, the others' every two) and changed with `settings set` and
  `reset`, each carrying its row's revision. One cursor moves through every section; rows a tool marks read-only
  show their source and are skipped; a tool without a settings contract says why in one dimmed row. A pending or
  typed value is checked with `settings check`: its preview or its error shows under the row. A conflict, an
  invalid value or a refusal shows once, in red under its row, and nothing is saved. `D` twice resets the
  selected row's section only. Junkfather's read-only exit 5 is taken like 4.

- EVENTS is one feed of every tool's event log, newest first (fathers#84): Lanefather's and junkfather's rows come
  between Slotfather's in time order, each with its tool's tag (`slot`, `lane`, `junk`) in a colour of its own and the
  event (`checked`, `owned`, `reaped`); with Slotfather alone those two columns are not drawn. `f` cycles the source
  filter (all, then each tool), `/` types a text filter into EVENTS' bottom border (Enter keeps it, Esc clears it,
  also once kept), the top border names the filter (`all sources`, `lanefather`, `"710"`; cut with `…` where it is
  long, never left out), `↑ N new` counts only rows it lets through, and the bottom border counts lines the reader
  skipped and why (`3 lines skipped: other host`). A narrow panel shortens the label first, then drops the pid, then the slots, then shows the tag as a
  one-cell mark. Help lists `f` and `/`.
- `godfather events [--source NAME]... [--follow] [--json]` prints the tools' event logs merged, newest last: time,
  source tag (`slot`, `lane`, `junk`), kind, subject and the detail EVENTS shows; `--json` prints the lines as read,
  `--follow` keeps printing what the logs gain (checked every 250 ms). It follows each log by inode through a rotation
  to `events.jsonl.1`, waits on a torn last line, and skips, counting them, lines that fail the shared validator,
  name another schema version or another host; an escape sequence in a line never reaches the terminal. An absent
  tool is left out; one whose capabilities name no event log is named on stderr (fathers#83).
- The footer says `Godfather what's new?`; a click or `v` opens what's new: one timeline of every
  tool's changelog, newest first, filtered by tool with ← → or a click on a tool's name. An install from main
  shows its Unreleased entries, which are what it runs, under `Slotfather · unreleased · installed 81abd86`. Closing it marks
  everything read (`~/.local/state/godfather/news.json`). Each tool is asked through its `changelog --json`; one
  without it adds nothing (fathers#102).
- Expected durations from Slotfather's run types (fathers#108): a running QUEUE row shows `12m / ~18m`, its time
  against the expected run, in yellow once it is past the p90; a waiting row shows the run it expects
  (`waiting #1 · ~18m`); DETAILS says `expected 18m · p90 31m · 23 runs`. In EVENTS a finished run that was an
  outlier ends in `▲` (`^` in ASCII) and the run that turned its run type's trend slower in `↗` (`/`); each trend
  change is a row of its own, `Straylight gate · slower +32% · same plan`, read from Slotfather's event log.

### Changed

- A command line Godfather does not take prints one line with the reason and one pointing to the help (`Try
  'godfather events --help'.`), no longer the whole usage text, as the tools' CLI convention says (docs/cli.md,
  fathers#154); so does the live view without a terminal. Every option that takes a value takes `--opt=value` too
  (`--interval=2`, `events --source=lanefather`), and a `--version` without its install record still names the tool.
- MACHINE says why nothing is admitted instead of a red `no admission` (fathers#156): `full · 8 of 8 held`,
  `whole machine: <label>` when one request holds every slot, `queue ahead · 2 waiting`, `draining · 6 of 4 held`
  (yellow), `memory short` (red: the one machine problem among them, first when there is another reason too:
  `memory short · full · 8 of 8 held`). A full or queued machine is the queue working and reads in the normal
  colour.
- Help fits the standard screen (fathers#145): at 156x43 its legend is laid out in columns, read down then across,
  a section going on in the next column between two rows, and shows whole with nothing to scroll; a smaller
  screen gets the one scrolling column as before.
- EVENTS shows one tool's log at a time (fathers#152): its title is the switch, `◂ SLOTS LOG ▸` (default where
  Slotfather is present), `LANES LOG`, `PROCS LOG`, then `ALL LOG`, cycled with ← → while EVENTS has the focus, `f`
  or a click on the title. Each log has its own columns, so labels keep their width: SLOTS LOG time, slots, label,
  waited, outcome and pid; LANES LOG event, lane, result and ticket; PROCS LOG event, label, detail and pid; ALL LOG
  a type (`slot run`, `lane checked`, `proc reaped`, in the tool's colour) and one sentence (`sid-kit build · ok 1s
  · 1 slot`). A Slotfather run is one row, `running 3m` while it holds its slots and how it ended once done; `r`
  shows every event as logged. A run's trend is a mark on its row (↗ slower, ↘ faster, ▲ outlier), no longer a row
  that ran across the columns. `/` filters within the log shown; the footer says `←→ log` while EVENTS has the
  focus, and help lists the keys.
- Settings: every rule is one length, the list sits two cells in from the frame, durations read `1h`, `30m`,
  `1h30m` (typed still in seconds), and a refused value is said once, under its row, not again in the notes
  (fathers#87).
- EVENTS reads Slotfather's event log instead of the events its snapshot describes, and its result column, now
  `detail`, is filled from each event's fields: `wait 32s` or `no wait` for every grant, `ok 3m25s` or
  `exit 1 · 26s` for every release, `gave up 5m`, `owner died · 12m`, `batch → interactive`, `waiting 45m`; it no
  longer reads durations out of Slotfather's prose. Wide panels also show the pid. A Slotfather whose capabilities
  name no event log shows EVENTS empty with `slotfather too old for events` in its border, one whose log is of a
  newer schema only with `slotfather's events need a newer godfather` (fathers#84).
- EVENTS' run marks and trend rows (fathers#108) are read through the same reader, so a rotated Slotfather log no
  longer drops them, and an outlier marks the release of its own lease id rather than the one that ended nearest
  (fathers#83, fathers#84).
- The panel Tab focuses shows without colour too: its top and bottom borders are heavy rules (`┍━ QUEUE ━━┑`, `=`
  in ASCII) as well as blue, and its sides stay light, so the heavy `┃` still marks only the selected row
  (fathers#139).
- A wide terminal too short for EVENTS under QUEUE (156×20 with six queue rows) keeps QUEUE at its rows (less at
  most two that MACHINE's three-row form needs), puts MACHINE's one-line form under it and gives DETAILS the whole
  right column, instead of padding QUEUE with empty rows while DETAILS was cut short. EVENTS comes in from the height
  that gives it its least rows, and as the terminal grows taller panels only ever arrive (fathers#140).
- MACHINE's history chart says it shows `slots held`. Beside the gauges the title and its figures
  (`slots held  avg 4.4/6 · peak 6 · full 10%`) sit in the blank row above it and its axis line keeps only the window
  and `now`, so neither its rows nor its figures line up with load, mem and swap; stacked under the gauges, the
  bottom border names it and the axis line keeps every figure (fathers#140).
- Screenshots and the README media come from `tools/raster.py`, Godfather's own cell rasterizer, with no browser,
  terminal or machine slot: box drawing and blocks are geometry snapped to each cell, so rounded corners and joins
  meet their lines and bars have no seams at fractional cell widths; emoji such as 🆗 are drawn in colour across
  their two cells; the same cells give the same pixels. `tools/screens.mjs` and screen-kit are no longer used
  (fathers#114).
- `what's new?` in the footer has one muted style, the key hints', whether or not anything is unread, and one space
  after `Godfather`; what's new itself still marks unread entries (owner, 2026-11-13).
- A request is known by Slotfather's `lease_id` (fathers#101) for its selection, colour and details, and by its pid
  and start only with an older Slotfather that gives none.
- The footer's key hints are centred between its left and right content at every width, and a narrow footer drops
  `tab panel` and `s settings` before `? help` (fathers#102).
- The changelog overlay became what's new: one left column, caps section headings, ticket references dimmed at
  the end of an entry (fathers#102).
- The batch priority mark is 🐌 instead of 🐢; the ASCII mark stays `~`.

### Fixed

- `?` and `v` switch between help and the changelog instead of closing one, and keys typed right after `v`
  scroll the changelog (fathers#17).
- The selection follows its request through reorders, grants and yields; when it ends, the selection moves to its
  neighbour among the running or the waiting, never across to the other group; two requests of one pid started in
  the same second are two rows; a lease that yields keeps its colour when it resumes (fathers#21).
- Details that fail to load say why and are tried again, and a grant or a yield loads them again at once
  instead of showing the other kind's facts for up to 5 s (fathers#23).
- Settings that cannot open say so in the footer for a few seconds; the status stays `● live` and nothing claims
  the refresh failed (fathers#24).

## 0.1.0 - 2026-11-12

### Added

- `godfather` opens Slotfather's dashboard, moved here unchanged: SLOTS, QUEUE, DETAILS, EVENTS and MACHINE in
  the wide and one-column layouts, focus and Tab order, scrollbars, emoji and ASCII marks, the theme modes, help,
  Settings, mouse and the kitty keyboard protocol on any layout, as of slotfather 346db8e (the details' floor in
  one column, MACHINE's padding without its chart, mem showing memory pressure). Only the footer's name and
  version differ.
- It reads Slotfather through its CLI alone: `slotfather capabilities --json`, `status --json --full` every
  Refresh every, and `settings --json`, `check`, `set` and `reset` for Settings. It never imports Slotfather and
  never reads its state files.
- A Slotfather snapshot that fails, takes over 5 s or has an unknown version leaves the last good screen,
  dimmed, with `stale since HH:MM` in SLOTS' border and the reason in Settings' STATUS. Without Slotfather, or
  with one that has no dashboard contract, one panel says what was looked for and why.
- The changelog overlay has a tab per tool whose capabilities name a changelog (← → switch them).
- Refresh every and Colour theme are kept in `~/.config/godfather/settings.json`. `GODFATHER_THEME`,
  `GODFATHER_TERMINAL_BG` and `_FG` set the theme inputs; the `SLOTFATHER_` names still work where they are unset.
- `GODFATHER_FIXTURES=DIR` replaces every tool call with recorded replies and records writes instead of making
  them; the tests, goldens and README media use it.
- The shared event schema, version 1: `docs/events.md`, `schema/event-v1.schema.json` and `eventcheck.py`, one
  MIT-licensed standard-library file the tools vendor into their tests (`python3 eventcheck.py [--strict] FILE...`).
- `python3 install.py` installs `godfather` into `~/.local/bin` as an entry point into an immutable release; a
  running dashboard shows `↻` once a newer one is installed.

### Changed

- EVENTS has four columns, time · slots · label · result, where Slotfather's had six. The slot cells say what
  happened, filled `▪` for slots taken and hollow `▫` for slots handed back, in the text colour; the event and pid
  columns are gone, and the label gets their width. The result is short and right-aligned: `wait 32s` or
  `no wait`, `ok 3m25s` (muted), `exit 1 · 26s` (red), `gave up 5m`, `owner died · 12m`. Help explains them.
