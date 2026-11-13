# Slotfather Changelog

Versions and release rules: [docs/agents/publishing.md](docs/agents/publishing.md).

## Unreleased

### Added

- `run` without `--label` names the request after its project (the git repository of its directory, never a temporary
  one), the part of the command that names the work and its lane, ticket and phase: `godfather · measure_cpu`,
  `straylight · lane 743 · node --test · check`; a label that says little (`scratchpad`, `tmp`, the project's name
  alone) gets those parts appended, with a hint. The history keeps `label_derived` and, where it differs,
  `label_given`; `stats --by derived` groups by it.
- Expected run times per run type (project, phase, plan): `status --json --full` gives each request `expected` p50,
  p90 and run count, and `overrun` once a running one passes its p90; the history records each run's `run_key` and
  `verdict`, settled outliers are EVENTS (`run.outlier`), and `slotfather stats --trend --json` gives weekly
  p50/p90, plan changes and whether a run type got slower and why (`run.trend`).
- Every request has a unique `lease_id` in `status --json` (and `--full`), the history and its events' subject.
- `slotfather changelog --json [--since VERSION]` gives the installed changelog as data for Godfather's what's
  new, Unreleased entries first with the installed commit.
- Measurement sessions: `slotfather session start --label NAME` drains the queue once and holds it (all slots by
  default) for a series of `slotfather run --session NAME -- COMMAND`, until `session stop NAME`, `--idle` seconds
  without a run (default 2 minutes) or its holder's death; waiters see its `--expect` run, and it is one holder in
  `status` and the dashboard.
- The history records the git `head`, `tree` and `dirty` of a request's directory, and `--meta cache=hit|miss|partial`.
- `run -n N` hints when its label family's recent runs used far fewer cores than N slots hold; `slotfather stats`
  lists those families (`asks`) and groups by them with `--by family`.
- `slotfather history export` and `history import HOST` keep another host's history (a CI runner's) beside this
  one's; `slotfather stats --host HOST` reads it.

- `slotfather capabilities --json` says which dashboard contracts this release supports, and `slotfather status
  --json --full` adds the host, the time and the dashboard's own model to `status --json`, from the same look at
  the queue, so a separate dashboard (Godfather) can draw today's screen; `status --json` is unchanged.
- `slotfather settings --json`, `settings check`, `settings set [--revision R]` and `settings reset` read, check and
  save every Settings row through the queue's own validation and lock, with the panel's previews, conflicts and
  read-only queues.
- events.jsonl beside the queue records every grant, release, give-up, reap, reprioritize, long wait, yield, capacity
  and order change and sweep stop in Godfather's shared event schema; queue.log and history.jsonl stay as they are.
- `slotfather ask-yield PID --start START` asks a yieldable lease to yield for a waiting request of a higher class,
  as a waiting interactive request would, and `reprioritize ... --start START` refuses a pid that is now another
  process; capabilities offer both as dashboard actions.
- A waiting request ages: a batch one is served as normal after 30 minutes, a normal one as interactive after 60
  more, keeping its arrival. `slotfather order --aging-batch 30m --aging-normal 60m` sets the steps (0: never;
  `SLOTFATHER_AGING_BATCH`, `_AGING_NORMAL` can only age less); Settings shows them as Batch promoted after and
  Normal promoted after. `status` and the dashboard show the class a request is served in and that it aged.
- `slotfather reprioritize PID|LABEL interactive|normal|batch` moves your request to another class without losing
  its place in the queue; queue.log and EVENTS record it.
- `status` gives each waiting request its expected start (`est-start=`, JSON `est_start`) and marks the head with
  its reserved start; the dashboard's details show the expected start.
- A request that has waited 30 minutes (`slotfather order --warn-wait`, Settings: Warn after waiting) says so on
  its stderr every 15 minutes, with what it waits behind, its expected start and the `reprioritize` command to move
  it up; queue.log and EVENTS record it, and `status` flags it (`long-wait=`, JSON `long_wait`).
- QUEUE shows each request's priority after its state: ⚡ interactive, 🐢 batch, nothing for normal; the
  details put the same mark before the priority's name.

### Changed

- The installer retries a held queue lock for up to 15 seconds (`SLOTFATHER_INSTALL_LOCK_WAIT`) before it refuses.
- Slotfather lives in the `fathers` repository with Lanefather, junkfather and Godfather: install it with
  `python3 install.py slotfather` from a `fathers` checkout. `slotfather dashboard` without Godfather points there.
- A memory-pressure warning (macOS level 2, Linux PSI `some` at its limit) no longer stops every new lease: one
  1-slot lease at a time starts beside running ones; larger requests and `-n all` wait, and critical pressure, low
  swap or low available memory still stop. `slotfather order --warning-admits 0` (or `SLOTFATHER_WARNING_ADMITS=0`)
  restores the stop. `status` says why memory holds a request back (`memory-wait=`, JSON `memory_wait`), `advise`
  adds `memory_level`, and the reasons now read `memory pressure level 2 (warning)` or `... 4 (critical)` instead of
  `(warning or critical)`.
- The head's start is reserved and only ever moves earlier: a later request, of any class, passes a head only if
  three times its expected run ends before that reserved start, or in slots the head will not need. Short work keeps
  flowing beside a waiting `-n all` request, and the request starts once the leases it waited for end. The head's
  limit (nothing passes after 2700 s) is off by default; `--limit` still sets one.
- The scrollbar is a thumb alone, moving in half cells; the `↑ N above` and `↓ N more` counts are gone.
  EVENTS still shows `↑ N new` while scrolled away from the newest.
- The focused panel's frame is the focus blue, with its title and column headers in the foreground
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
- The selected row is marked on both frames, left and right, in QUEUE and Settings.
- Help and the changelog start their text under their title's first letter, with two blank lines before each
  section or release and one after its title.
- Every chromatic colour is the dashboard's own, in a dark and a light variant that reads on either background:
  the focus blue, ok, warn, bad and legacy, and the MACHINE chart and bars, a smooth indigo-to-red ramp through
  purple and magenta whose red is also a critical bar's. Terminal mode keeps the theme's ANSI colours.
- Unfocused frames are a little paler than muted text.
- MACHINE's chart sits right under the resource rows and over its axis, with half a cell of air at each end; short of rows, the chart shrinks to two rows and then
  goes before load, memory and swap share one compact line, and memory and swap are always shown.
- MACHINE's rows are short: `load 10.1/12`, `mem 23.0/32G`, `swap 10.0/11G`, used / total with the values in
  one right-aligned column and bars of one length. From 62 columns the chart sits beside them, so MACHINE is
  shorter and the panels above get the rows.
- MACHINE has no status row: each of load, memory and swap starts with 🆗 or 🔥, and the unleased load and the
  admission state sit on the right of its top border. A blank row stays under the border; short of rows it goes
  after the chart and before the rows compress.
- In one column a long queue scrolls instead of squeezing the details, which keep at least nine rows.
- MACHINE's mem row shows what admission reads: on macOS the kernel's memory pressure (normal, warning,
  critical) as a three-step gauge, not used RAM, which the file cache keeps near full; on Linux used / total
  from MemAvailable, or the PSI figure while it holds admission back.
- Without its chart, MACHINE keeps a blank row under its rows as well as over them.
- Under 110 columns EVENTS and MACHINE sit side by side below the details, EVENTS on the left, in MACHINE's own
  height, with the details taking the rest; under 83, EVENTS goes.
- A scrollbar's thumb is muted at rest everywhere, also on the focused panel and in help, the changelog and
  Settings, and flashes in the text colour while its panel scrolls.
- The holder palette is "Neon": eight saturated hues (hot pink, orange, acid green, spring teal, cyan, electric
  violet, magenta, green), more distinct from one another, with inks at 4.6:1 or more.

### Removed

- The in-tree dashboard and the `slotfather-dashboard` command: the dashboard is part of
  [Godfather](https://github.com/maxvyshniakov/fathers/tree/main/godfather) now. `slotfather dashboard [ARGS]`, and `slotfather`
  alone in a terminal, run `godfather ARGS` (its arguments and exit code), or say where to get it and exit 2 when
  it is not installed. The installer removes an earlier `slotfather-dashboard`. `status --json --full`,
  `capabilities --json` and `settings` stay, for Godfather.

### Fixed

- On macOS two clients in different locales (`LC_ALL`, `LC_TIME`) no longer reap each other's live leases: `ps`
  runs in the C locale everywhere, and an entry an older client wrote in its own locale's words still matches its
  holder.
- A `ps` that fails, times out, prints nothing or leaves out its caller no longer makes every holder look dead:
  that look reaps no lease and grants nothing, says so in queue.log (`skipped reaping: ...`) and EVENTS
  (`reap.skipped`), and waiters keep their place until the next good table. The dashboard's CPU sample refuses
  such a table too.
- A batch `-n all` request no longer waits for as long as normal and interactive work keeps coming: on 2026-11-11 a
  Safari profile run waited 6 h 14 min and was given up. It now ages to normal after 30 minutes and is then the head.
- QUEUE, EVENTS, the details, help, the changelog and Settings keep a blank row over and under their content
  again, as SLOTS and MACHINE do; the scrollbar runs between them.
- The load chart no longer shows more slots held than the queue has (`peak 9` of 8): a request that left
  without ever being granted was counted as a lease that ended once queue.log grew past the part read.

- Muted text and panel frames are readable in themes whose bright black sits close to the background (Seti,
  One Dark, Solarized Dark, Dracula): the dashboard mixes them from the theme's own text and background colours, at
  4.5:1 or more and dimmer than the text, and recomputes them when the terminal switches between light and dark.
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

- QUEUE, EVENTS, the details, help, the changelog and Settings keep a blank row over and under their content
  again, as SLOTS and MACHINE do; the scrollbar runs between them.
- The load chart no longer shows more slots held than the queue has (`peak 9` of 8): a request that left
  without ever being granted was counted as a lease that ended once queue.log grew past the part read.

- Muted text and panel frames are readable in themes whose bright black sits close to the background (Seti,
  One Dark, Solarized Dark, Dracula): the dashboard mixes them from the theme's own text and background colours, at
  4.5:1 or more and dimmer than the text, and recomputes them when the terminal switches between light and dark.

- A swap guard that serialised the whole queue.

## 0.0.1 - 2026-10-29

### Added

- A machine-wide slot queue with shared capacity, extracted from Straylight, and an installer that
  migrates the old queue.
