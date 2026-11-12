# Godfather Changelog

Versions and release rules: [README.md](README.md#versions).

## Unreleased

## 0.1.0 - 2026-11-12

### Added

- `godfather` opens Slotfather's dashboard, moved here unchanged: SLOTS, QUEUE, DETAILS, EVENTS and MACHINE in
  the wide and one-column layouts, focus and Tab order, scrollbars, emoji and ASCII marks, the theme modes, help,
  Settings, mouse and the kitty keyboard protocol on any layout. Only the footer's name and version differ.
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
