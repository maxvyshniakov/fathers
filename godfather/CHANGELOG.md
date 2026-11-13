# Godfather Changelog

Versions and release rules: [README.md](README.md#versions).

## Unreleased

### Added

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

- The panel Tab focuses shows without colour too: its top and bottom borders are heavy rules (`┍━ QUEUE ━━┑`, `=`
  in ASCII) as well as blue, and its sides stay light, so the heavy `┃` still marks only the selected row
  (fathers#139).
- A wide terminal too short for EVENTS under QUEUE (156×20 with six queue rows) keeps QUEUE at its rows, puts
  MACHINE's one-line form under it and gives DETAILS the whole right column, instead of padding QUEUE with empty rows
  while DETAILS was cut short (fathers#140).
- MACHINE's history chart says it shows `slots held`: beside the gauges in the blank row above it, so its rows no
  longer read as sparklines of load, mem and swap; under them in its axis line (fathers#140).
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
