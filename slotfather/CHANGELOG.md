# Slotfather Changelog

Versions and release rules: [docs/agents/publishing.md](docs/agents/publishing.md).

## Unreleased

### Added

- Explicit expected-peak memory budget and per-group concurrency caps (fathers#251): `slotfather order --memory-budget-mb MB` (and `SLOTFATHER_MEMORY_BUDGET_MB`) caps concurrent expected peak RSS alongside `memory_share`, and `advise` now derives `gib_per_slot` (`gib_per_slot_source`: `"derived"` vs `"configured"`) and `memory_gib` in `fixed` capacity mode on macOS (previously `null`), alongside `recommended_gib_per_slot` (from per-slot history peak RSS), `memory_budget_mb`, `memory_expected_mb`, and `group_cap_block`. `slotfather order --group-cap GROUP=N|off` (and `SLOTFATHER_GROUP_CAPS`) caps concurrent leases per group (matching explicit `group`/`suite`/`kind` metadata, `browser`/`control`/`story checks`/`playwright` labels, and `playwright`/`story-checks` commands as `browser`), holding excess waiters with `group-wait=` in `status` and `group_wait` / `group_caps` in `status --json` while non-matching work and yield requests proceed.

- Machine samples record who uses the machine outside leases: a sample taken while processes no lease owns use at
  least one core stores the 8 busiest command families with their CPU share, process count and owner (the
  Spawnfather entry or agent session that started them), after taking out each lease's tree, processes carrying
  its marker and its detached browser helpers. Families carry no arguments or paths. `stats` adds "outside leases":
  core-hours by family and by owner (`--json`: `outside`). `machine.jsonl` lines are version 2; older lines still
  read (fathers#239).

- Requests can ask for the cores they use instead of a slot count: `run --cores C` holds the slots that C cores
  need (`--cores 6` is 4 slots on a 12-core, 8-slot machine), and `run --cores auto` the slots that its label
  family's recent measured runs used, one slot until there are enough of them. `run` warns when its family's runs
  used at least 1.5 times the cores its slots hold, with the `-n` that would hold them; `advise` adds `weight`, the
  core-weighted slot count for `--cores C` or the measured use; `stats` lists such families under "asks far below
  use" (`--json`: `under_ask`, `cores_p90`); `history` marks such a lease `> N held` and `status` tags a running one
  `over-cores=USED/HELD`. The load admission threshold is unchanged, and `-n` and library callers ask what they
  asked before (fathers#148).

- Sustained CPU load holds further grants beside running leases: when `min(load1, load5)` less the held slots'
  share of the cores is at least the cores, or the load itself is at least 1.5 times the cores, a request waits
  until the load drops, while an idle machine still starts its first request. `slotfather order --load-gate X`
  sets the multiple (`0` turns it off) and `SLOTFATHER_LOAD_GATE` lowers it for one client. `status` says why
  (`load-wait=`), `status --json` adds `load_wait`, the dashboard block adds `load_block` and its `admission`
  follows the gate, and `advise` adds `load_block` (fathers#184).

- The dashboard's admission follows the expected-peak memory budget that grant() applies: beside running leases
  whose expected peak memory and a new request's exceed `memory_share` of the memory it is 0, where it used to
  advertise free slots no request could take. The dashboard block adds `memory_budget_block` (why, or `null`) and
  `advise` adds `memory_budget_block` for the request it is asked about (fathers#206).

- `slotfather.contract` defines `TypedDict` contracts for `status --json` (`StatusSnapshot`, `StatusEntry`),
  `status --json --full` (`FullSnapshot`, `DashboardBlock`, `DashboardRow`, `DashboardUnknown`, `DashboardEvent`), and
  `events.jsonl` (`Event`, `Subject`, `Actor`, `KINDS`), checked against `docs/api.md`, with strict Pyright typing
  across `stats`, `usage`, and `install.py` (fathers#125).

- `slotfather sweep` owns machine-wide disk hygiene across six disk classes (`docker-anonymous`,
  `docker-dangling-images`, `docker-superseded`, `claude-scratch` sparing running Junkfather entries' working
  directories, `package-caches` for uv/Homebrew/npm/bun, and `low-disk` consumer notices), reporting by default,
  acting only with `--live`, configured through `slotfather sweep config`, and preserving `sweep.json` bytes when
  no disk setting is changed (fathers#6).

- `slotfather run -n MIN-MAX` queues as a `MIN`-slot request (scheduling order treats it as `MIN`) and grants up
  to `MAX` of what is free at grant time. The granted slot count is exported as `SLOTFATHER_SLOTS` and recorded on
  the entry, in `status --json`, and in the `lease.granted` event; `capabilities --json` advertises `slot-range`
  support (fathers#128).

- `slotfather run` automatically tags leases with `lane` (from `LANEFATHER_LANE` or the worktree's Lanefather
  record/path) and `agent_session` (from `CLAUDE_CODE_SESSION_ID`) unless set by the caller, trimming auto-tags
  to 200 characters and dropping them with a warning at the 16-key limit while still refusing over-limit caller
  metadata. `run` also exports `SLOTFATHER_LEASE_CORES` (`max(1, floor(min(count, total) * cores / total))`) to
  the child process (fathers#11).

- Memory-aware admission estimates each request's peak RSS from history by run type, tag, and label family
  (`estimates.py`), falling back to `memory_default_mb` (default 2048 MB), and defers concurrent grants in
  `schedule.memory_gate` when held plus candidate expected peaks exceed `memory_share` of RAM (default 0.85,
  0 disables). `status --json`, the `dashboard` block, and `stats --json` expose the peak memory estimates
  (fathers#9).

- `slotfather stats` and `stats --json` split request outcomes into four groups: command exit nonzero, reaped
  (held vs waiting), gave up waiting, and refused. Request outcomes are decided once from what the client observed
  (`entry.decide_outcome`), so a request granted concurrently at timeout or termination is recorded as `left` with
  its `reason` rather than `released` and does not feed runtime estimates; reaped lines record `was_held`
  (fathers#10).

- `install.py` smoke-checks a staged release (`--version` and `capabilities --json`) before swapping any wrapper,
  restores the previous wrapper set if a swap fails, and adds `--rollback`, `--prune` (keeping current, previous,
  and in-use releases), and `--uninstall` (fathers#126).

- A detached measurement session can yield its slots while its only active run waits on a foreground lock.
  Slots stay held for a background lock, a busy sibling or uncertain membership. Slotfather reconsiders a
  recorded wait when a sibling finishes, and a lock command waits for a grant before starting work. (fathers#183)

- A command joining a measurement session uses the ordinary lease wait. If the session yielded for a lock,
  Slotfather requests its slots back and waits for the grant before starting the command. (fathers#161)

- Session startup acknowledges the holder's grant even if process inspection fails afterward. A grant obtained
  while a waiter prints queue details is recognized before reporting its timeout. (fathers#161)

- The Settings panel and `slotfather settings --json` expose the timeout for a head blocked by leases with no end
  estimate. Changes that older clients cannot read show a compatibility warning. (fathers#178)

- A head whose reserved start passed while leases with no end estimate hold its slots no longer idles the machine
  (fathers#160): a request that fits the free slots starts when its p90 run ends by the head's projected start,
  and past `reservation_timeout` (`order --reservation-timeout S`, default 30 minutes, 0: never) the head gives
  way, naming the leases (its waiting line, queue.log, `request.head_dropped`); only leases granted before it
  gave way let it give way again, and meanwhile a request with no history is not admitted before it. `status` says `head LABEL blocked
  by leases ...; N slots idle`; `status --json` adds `blocked_by`, `idle_slots` and `gave_way` where they apply.
  A `reservation_timeout` other than 30 minutes saved in order.json makes a slotfather before it refuse the file
  and fall back to the window order, as for the earlier knobs (`slotfather order` says so).
- `slotfather lock PATH -- COMMAND` runs COMMAND holding a `flock` on PATH. While it waits for the lock, the lease
  it runs under gives up its slots if nothing else runs under it (in its own place and ageing, never granted
  meanwhile) and keeps them otherwise; once the lock is taken, or a signal ends the wait, it waits until the lease
  is held again. A waiter killed outright leaves a mark the next look clears, and work starting under such a lease
  takes its slots back. `status` shows `waiting-for-lock=PATH`, `status --json` and the dashboard block `lock`;
  EVENTS gets `lease.lock_ended` (fathers#159).
- `status --json` gives every entry its `phase` (`waiting`, `yielded`, `held`, `asked` or `unknown`).
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
- `slotfather procs --json` gives each process's start as epoch seconds too (`started`), the form another tool
  takes to name the same process (fathers#171).
- `status --json` (and so `status --json --full`) gives each entry's `uid`, the user its holder runs as, so a
  dashboard offers `reprioritize` only on the requests Slotfather lets that user move (fathers#88).

### Changed

- `slotfather --help` now uses the shared compact, grouped layout (`leases`, `inspect`, `configure & maintain`,
  ≤ 80 columns).
- The minimum supported Python version is now 3.11 (fathers#218).
- The CLI conforms to the shared CLI convention (`docs/cli.md`, fathers#124): table-driven parsing with per-command
  `--help`, one-line usage errors pointing to `--help`, `--opt=value`, `--version`, and optional `--json` on
  `slots`, `order`, `capabilities`, `settings`, `procs`, `changelog`, `stats`, and `advise`.
- The library's `acquire()` and `Lease` are in `slotfather.client` (were `slotfather.leases`), `run()` and
  `execute()` in `slotfather.runner`; `slotfather.leases` keeps the queue transactions (fathers#37).
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
- `slotfather sweep --live` stops processes through Spawnfather, the one tool that stops them (fathers#171): each
  tree the sweep decides may go is handed to `spawnfather reap --pid --started --cmd --tree`, which checks its start
  time and command again before a signal. Slotfather still decides what goes and keeps every lease's processes. A
  tree Spawnfather refuses (one of its entries owns it) is reported as spared with its reason. `--json` and
  `sweep.jsonl` add `stops_by`. `SLOTFATHER_SPAWNFATHER` names the command.

### Deprecated

- A live sweep without Spawnfather (or with one too old to stop a process by its identity) still stops processes
  itself in this release, with a warning on stderr and a note in its output and in `sweep.jsonl` (`deprecated`).
  The next release drops this: install Spawnfather with `python3 install.py spawnfather`.

### Removed

- The in-tree dashboard and the `slotfather-dashboard` command: the dashboard is part of
  [Godfather](https://github.com/maxvyshniakov/fathers/tree/main/godfather) now. `slotfather dashboard [ARGS]`, and `slotfather`
  alone in a terminal, run `godfather ARGS` (its arguments and exit code), or say where to get it and exit 2 when
  it is not installed. The installer removes an earlier `slotfather-dashboard`. `status --json --full`,
  `capabilities --json` and `settings` stay, for Godfather.

### Fixed

- Detached browser daemons adopted by Linux `systemd --user` (such as `agent-browser` spawning headless Chromium
  workers whose own `/proc/PID/environ` is cleared) are now attributed to their owning lease across `stats`
  (`outside leases` and `history` `cpu_detached`), `procs --json`, `status --json --pid` (`via: "env"`), and
  `slotfather sweep` sparing: `"env"` membership now propagates from a detached ancestor carrying `SLOTFATHER_LEASE`
  to its descendants, `outside` inspects detached roots of busy subtrees alongside the busiest individual processes,
  and per-lease usage tracking discovers orphans adopted by `systemd --user` alongside `ppid == 1`.
- `slotfather advise` now consults measured unleased process CPU whenever the load-average residual actually reduces
  `size()`'s slot capacity (the half-slot rounding boundary, `0.5 * cores / slots`) rather than at a fixed `>= 1.0`
  core threshold (fathers#257): this lowers the trigger below `1.0` core on machines with fewer than 2 cores per slot
  so stale load averages do not shrink worker advice, and raises it above `1.0` core on machines with more than 2 cores
  per slot so `ps` is skipped when unleased load costs no slot.
- Unleased CPU load estimation (`query.size()`, `advise`, dashboard `advice.unleased_load`, and the CPU load admission
  gate) now caps the load-average residual (`min(load1, load5) - held * cores / slots`) by measured unleased process
  CPU from `ps` (read lazily when needed, or reused from the dashboard's process table), so Linux `loadavg` inflation
  from leased processes' runnable or I/O-wait threads is no longer misattributed as high unleased load or false
  unleased load-gate blocks while the peak total-load gate (`1.5 x cores`) still applies.
- Slotfather and Spawnfather now share one table of Claude Code and Codex agent-session patterns
  (`fathers_common.agents`), so `slotfather procs --json`, `slotfather sweep` and outside-leases attribution
  recognize the same agent sessions as Spawnfather, including vendor-bundled Codex and interpreter-launched
  sessions (fathers#146).
- `slotfather sweep` protects live processes belonging to newer clients' unknown queue entries, wait forecasts
  account for the full slot count granted to `-n MIN-MAX` range requests, `-n 1-MAX` grants clamp to 1 slot under a
  memory-pressure warning, and requests with no run history are no longer admitted ahead of a head that gave way
  after a second head is chosen (fathers#228).
- Releasing or aborting a measurement session removes its session record beside the queue entry instead of leaving a
  stale file behind, finishing a `slotfather lock` wait updates the enclosing lease in one queue step so it cannot
  clear another waiter's active lock wait, and history and `slotfather stats` count leases that yielded only for a
  lock as having held slots and record their yielded time (fathers#224).
- Enforced the `memory_share` expected-peak memory budget on the second and later concurrent grants of an initially
  idle machine, and applied the blocked-head `p90` backfill rule consistently across dashboard admission, `available`,
  `advise`, `ask-yield`, and `yield` (fathers#214).
- Fixed scheduler, admission, and sweep edge cases: backfill head-drop now accounts for earlier grants (including
  `-n MIN-MAX` range sizes) in the same look and refuses 0-second backfill past unknown earlier-queue slots;
  `window` order refuses `slotfather ask-yield`, withdraws standing yield requests, and ignores stale yield markers
  (including under `SLOTFATHER_QUEUE=window`); `status` matches `grant` for `-n MIN-MAX` requests under memory
  pressure warning; dashboard admission keeps CPU load and memory pressure blocks distinct around `warning_admits`
  and idle-machine grants; and `slotfather sweep` spares any stopping ancestor whose tree contains a spared child
  (including under `--pid`) (fathers#211).
- On macOS with a readable kernel memory-pressure level (`kern.memorystatus_vm_pressure_level >= 1`), `slotfather
  sweep` no longer reports a false `memory short: free swap ...` solely because the currently allocated
  `vm.swapusage` slice is below `swap_free_mb` or `swap_free_pct`, matching `platform.memory_level()`; Linux,
  unavailable macOS pressure, and the `SLOTFATHER_MEMORY_FILE` test seam keep the free-swap floor (fathers#205).
- On macOS, detached browser and WebKit XPC helper processes spawned via `launchd` (`PPID=1`, such as Playwright
  `ms-playwright/webkit-*` `com.apple.WebKit.{WebContent,GPU,Networking}.Development` or isolated-profile browser
  helpers) are attributed to their owning live lease for dashboard `LEASE DETAILS` CPU/memory/process accounting,
  run history (`cpu_detached`, `rss_peak_mb`, `processes`), idle tracking, `status --json --pid`, `procs --json`,
  and `sweep` protection (`via: "helper"`), while excluding unrelated user/system browsers and refusing ambiguous
  attribution when multiple runs share the same browser driver concurrently (fathers#202).
- `events.jsonl` strips Unicode line and paragraph separators (`U+2028`, `U+2029`), caps general strings at 1000
  characters (400 for commands) and bounds the `events.lock` wait to 1 s, and `install.py` vendors `fathers_common`
  into each immutable release and includes it in the source digest and dirty checks (fathers#117).
- `slotfather changelog --json` follows the shared changelog contract (`godfather/docs/contracts/changelog.md`):
  entries require `- ` bullets under a `### ` section, dated entries omit `unreleased`, `--since` accepts build-suffixed
  versions and orders matching headings by file order, and `source` on Unreleased entries comes from `version.json`
  (`null` from a checkout) (fathers#119).
- On Linux, `procs --json --cwd` omits non-dumpable long-lived managed services (`systemd --user`, `(sd-pam)`, and
  systemd `.service` `MainPID`s) whose `/proc` links fail with `EACCES`, while any non-dumpable process started
  inside a worktree or outside a service's `MainPID` remains in `cwd_unknown` (fathers#164).
- The holder's `.idle` record now includes its recent CPU rate (`cpu_pct`, cores × 100), and on Linux the
  dashboard block uses it for held leases' `cpu` (falling back to `ps %cpu` for older clients or on macOS), so
  an idle lease is no longer also marked hot from `ps`'s lifetime average (fathers#20).
- Dashboard EVENTS and the held-slot history now read `events.jsonl` (falling back to `queue.log` only when
  `events.jsonl` is absent) and account for yields, resumes and releases while waiting to resume instead of
  drifting the held-slot count (fathers#18).
- A request run outside any git repository (a temporary directory, a clean worktree there) no longer gets a
  run type named after its command (`true · check`, `echo · check`): its run type keys by its label's family, or
  without a label of its own by its phase alone, so its runs form one series whatever their command (fathers#106).
- A lease whose tree runs a container or a virtual machine (`docker run`, `docker compose up`, `podman exec`,
  `qemu-system-*`, `vfkit`, ...) is never idle, though that work uses no host CPU (fathers#159).
- `procs --json --cwd` no longer lists in `cwd_unknown` or `lease_unknown` a process that exited between the scan
  and the read of its directory or environment (a short-lived `lsof`, git or shell helper), which made
  `lanefather done` refuse nearly every lane on a busy machine; only one still running with the same start
  time is listed, and the added `unknown` names each with its `start` and `cmd` (fathers#158).
- A process whose environment cannot be read (another user's, an Apple platform binary on macOS, a `/proc` with
  `hidepid`) is unknown, never "not in a lease": while leases are live the sweep spares and reports it (`lease
  unknown` in its reason, `lease_unknown` in `--json`) instead of stopping it, and `status --json --pid` adds
  `lease_unknown` for it. On macOS the kernel's copy of an environment tells a hidden one from one without the
  marker, so `procs --json` lists fewer pids in `lease_unknown`. Apple's own binaries (`/bin/sleep`, `/bin/zsh`,
  `/usr/bin/tail`) always show an empty environment there: while any lease is live the sweep never stops such an
  orphan (a `tail -f` among them), and reports it instead.
- `procs --json` takes an agent run from an app bundle whose path has spaces (`/Applications/Claude Code.app/...`)
  for a session, as the sweep always has: both follow one session rule.
- A queue entry this Slotfather does not know (a state other than waiting or held, no state, or no pid, start,
  since or count) is left alone, since it may be a newer client's lease: it is no longer granted as a waiter, reaped
  or deleted, and no longer stops every client's look with a `KeyError`. `status`, `status --json` and the dashboard
  block show it as `unknown` with a warning. While its holder runs its slots stay reserved (held, not free); once
  its holder is gone `status` says to remove the file, and `procs --json` and the installer read it as absent with
  a warning instead of failing.
- Queue entries and the `sequence` file are written whole and synced to disk, so a crash no longer leaves an empty
  entry or loses the counter; a torn `sequence` no longer puts a new request ahead of those already waiting. An
  entry that cannot be read is left in place and `status` lists it as `unreadable`.
- queue.log no longer grows for ever: past 4 MiB it becomes queue.log.1, replacing the older one, as history.jsonl
  does; the dashboard's load chart reads across both.
- Two clients in different locales or time zones no longer reap each other's live leases: entries carry
  `started`, the holder's start in epoch seconds, which current clients compare. `start` stays as each system's
  older clients write it (macOS: the client's own locale), so older and current clients share a queue during an
  upgrade as before; an older client's entry in another locale's words still matches its holder.
- A `ps` that fails, times out, prints nothing or leaves out its caller no longer makes every holder look dead:
  that look reaps no lease and grants nothing, and waiters keep their place until the next good table. Each
  client says so once per failure in queue.log (`skipped reaping: ...`, then `reaping resumed`) and EVENTS
  (`reap.skipped`, `reap.resumed`); `status` and the dashboard fail without logging. The dashboard's CPU sample
  refuses such a table too.
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
