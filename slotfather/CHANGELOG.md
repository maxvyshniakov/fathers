# Slotfather Changelog

Versions and release rules: [docs/agents/publishing.md](docs/agents/publishing.md).

## Unreleased

### Changed

- The scrollbar is a thumb alone, moving in half cells; the `↑ N above` and `↓ N more` counts are gone and
  their lines hold content. EVENTS still shows `↑ N new` while scrolled away from the newest.
- The focused panel's frame is the terminal's blue, with its title and column headers in the foreground
  colour; the other panels stay muted. Every frame keeps the same light, rounded lines.
- A selected row keeps its text colour; only its background is tinted, lightly enough that its text keeps
  most of its contrast.
- Every footer hint, in the dashboard and in help, Settings and the changelog, is a click target that does
  what its key does.
- Tab goes by columns: QUEUE, EVENTS, then DETAILS (top to bottom in one column), only through panels on
  screen.
- Holder colours come in pairs: a vivid fill for slot blocks, cells, CPU bars and the selection, and an ink of
  the same hue for labels and numbers, readable on light and dark backgrounds. None sits near the theme's blue,
  red or yellow, so a holder is never taken for the focused frame, an over-share bar or an idle lease; the
  selected row's tint is stronger on light backgrounds.
- The holder palette is "Neon": eight saturated hues (hot pink, orange, acid green, spring teal, cyan, electric
  violet, magenta, green), more distinct from one another, with inks at 4.6:1 or more.

### Fixed

- Settings: an arrow change still waiting to be saved is no longer saved over a capacity change made
  elsewhere meanwhile, nor under another capacity mode; the panel says the capacity changed instead.
- Settings: a refused save when moving to another setting stays visible, and Esc on a panel too small to
  show cancels a pending change visibly instead of dropping it.
- With DETAILS focused while its details load, or with nothing to show, the arrow keys no longer move the
  QUEUE selection.

## 0.1.0 - 2026-11-11

### Added

- Dashboard colours follow the terminal's theme, light or dark, and switch with it; Settings → Colour theme
  picks Auto, Terminal or Built-in (`SLOTFATHER_THEME`).
- Twelve vivid holder colours, the same owner in the same colour everywhere.
- QUEUE rows start with their state: ▶️ running, ⏸️ waiting, ↩️ yielded, ❓ asked to yield, 💤 idle,
  🔥 over its CPU share, 👨‍🦳 a lease of an older client.
- Tab and Shift-Tab move between QUEUE, details and EVENTS; arrows, PgUp/PgDn and Home/End scroll the
  focused panel, and a click focuses the panel it lands in.
- Scrollbars on every panel that overflows; EVENTS stays put while scrolled down and counts what arrived.
- The details load in the background with a spinner, so moving through the queue is instant.
- The footer shows the version; `↻` when a newer install waits for a restart. Click it, or press `v`,
  for this changelog.
- `slotfather version` prints the version, e.g. `slotfather 0.1.0 (b353695)`.

### Changed

- The selected row has a heavy left frame and a background tinted with its own colour.
- MACHINE bars fill blue to red along their length and turn all red when the resource is critical; the
  history chart is four rows with a time axis (`−60m … now`) and the mean, peak and full share in slots.
- Help is a short reference of keys, states and columns; Settings is one flat list whose values carry
  their units.
- Footer hints are centred: `↑↓ select   tab panel   s settings   ? help   q quit`.

### Removed

- The pause (`p`), refresh (`r`) and select-text (`m`) keys: the view refreshes on its own, and
  Shift+drag selects text.

## 0.0.6 - 2026-11-11

### Added

- Idle leases are marked, and a yieldable idle lease is asked to yield while a request waits for its
  slots.
- Dashboard hotkeys work on any keyboard layout.
- QUEUE shows slots as countable cells; EVENTS shows what each event did.

### Changed

- A capacity change applies at once, also while work runs; a shrink drains.
- Batch requests defer only while other work waits; whole-machine waiters no longer block backfill.
- Installing converts live queue entries of recent clients in place.

## 0.0.5 - 2026-11-07

### Added

- An editable Settings panel in the dashboard.
- Lane details (ticket, branch, receipt, review) for leases that carry lane metadata.
- A versioned, stateless lane admission query.
- Priority and ticket as request fields.

## 0.0.4 - 2026-11-04

### Added

- `slotfather sweep` finds and stops processes left behind machine-wide.

## 0.0.3 - 2026-11-03

### Added

- `slotfather stats`: waits, runs and cores per slot by label and kind.
- Backfill past a blocked head by expected run time; request classes and polite preemption.
- Lease metadata (`run --meta`, `status --json`).
- Memory-based capacity and a Linux memory guard.

## 0.0.2 - 2026-11-02

### Added

- A live dashboard.
- Smaller requests may pass a blocked waiter for a while.

### Fixed

- A swap guard that serialised the whole queue.

## 0.0.1 - 2026-10-29

### Added

- A machine-wide slot queue with shared capacity, extracted from Straylight, and an installer that
  migrates the old queue.
