# Reading the dashboard

`slotfather` opens the dashboard when stdin and stdout are terminals. Otherwise it
prints usage; use `slotfather dashboard --once` for a scripted snapshot.

```sh
slotfather dashboard --interval 5
slotfather dashboard --once --width 156 --height 43 --color
```

## Navigation

| Input | Action |
| --- | --- |
| ↑ / ↓ or j / k | QUEUE: select a request. DETAILS, EVENTS: scroll a line. |
| PgUp / PgDn | QUEUE: first or last request. DETAILS, EVENTS: a page. |
| g / G, Home / End | QUEUE: first or last request. DETAILS, EVENTS: top or bottom. |
| Tab / Shift-Tab | Focus the next or previous panel, by columns: left to right, each top to bottom (wide: QUEUE, EVENTS, DETAILS). Only panels on screen take the focus. |
| Click | Select a request; focus the panel it lands in. A footer hint does what its key does (on `↑↓`, each arrow its own); the footer's version opens the changelog. |
| Mouse wheel | Scroll the panel under the pointer. |
| s | Open Settings. |
| ? | Open help. |
| v | Open the changelog. |
| Esc | Close help, Settings or the changelog; so does a click outside them. |
| q | Quit. |
| Shift+drag | Select text: the terminal keeps the drag for its own selection while the dashboard reads the mouse (button tracking with SGR reports, modes 1000 and 1006). Kitty, Ghostty, WezTerm, xterm, Alacritty and GNOME Terminal use Shift; iTerm2 uses Option. |

The view refreshes on its own, every second by default (Refresh every, in Settings). The footer has the name and
version on the left (`Slotfather 0.1.0`, with `↻` once a newer install waits for a restart), the hints in the
middle (`↑↓ select   tab panel   s settings   ? help   q quit`; `↑↓ scroll` on DETAILS and EVENTS) and the
last update and `● live` on the right; a narrow terminal drops the version first, then the hints help,
settings, tab and select, quit last, and they never cover the status.

**Focus** (#15). One panel has the keys: QUEUE at start, then Tab and Shift-Tab, or a click. Every frame has the
same light, rounded lines; the focused panel's is in the accent blue (the terminal's ANSI blue) and the labels set
into its top border (title, column headers) in the foreground colour, while the others stay muted. Arrow keys on a
focused DETAILS or EVENTS are always theirs, also while the details load or with nothing to scroll. The selected
row's marker is a heavy `┃` on both frames, left and right, in its colour. EVENTS
stays on its newest entry while scrolled to the top; scrolled down, it keeps showing the same events and counts the
new ones in its bottom border (`↑ 3 new`); Home goes back.

**Details in the background** (#15). Moving the selection redraws at once; the selected request's lane facts
(a lanefather call, 150–260 ms) and its holder's directory (`lsof`, about 25 ms) load in a background worker,
one at a time, the newest request replacing a waiting one, with a spinner meanwhile. Loaded details are shown at
once for five seconds and then refreshed quietly; a load that finishes after the selection moved on never paints
over it. The queue itself (about 0.4 s a look) refreshes in the background too.

**Changelog**. Click the footer's version, or press `v`: what each release changed, from the CHANGELOG.md
installed with the package. The running release is marked installed; after a newer install, the releases it
adds are marked new until the dashboard restarts.

**Any keyboard layout** (#13). Hotkeys are the keys at their US-QWERTY positions:
on a Russian (ЙЦУКЕН) or Ukrainian layout `ы` opens Settings and `й` quits. Where
the terminal speaks the kitty keyboard protocol (kitty, WezTerm, Ghostty, recent
iTerm2) the dashboard asks it to report every key with its base-layout key and
restores the terminal's mode on exit, also after a failure; elsewhere a table maps
those layouts to the US positions. Only hotkeys are translated: what you type into
a Settings field stays as typed. Help keeps showing the Latin letters.

Only the selected request has a heavy `┃` on both frames, in its fill (`#` in ASCII), and a
tinted background; its text keeps its ink. A running row's tint mixes up to 16% of
its fill into the terminal's background, less where its ink would lose more than a
tenth of its contrast; a waiting row's is the neutral selection;
without a known background the row is bold. Every panel that overflows (QUEUE,
EVENTS, the details, Settings, help, the changelog) shows a scrollbar thumb, no
track, in the padding cell left of its right frame: muted, or in the foreground on
the focused panel, as long as the visible share and moving in half cells (`╻` and
`╹` at its ends). There are no row counts. Like SLOTS and MACHINE, every scrollable
panel (QUEUE, EVENTS, the details, help, the changelog, Settings) keeps one blank row under
its top border and one over its bottom border; its content and the thumb run between them.
The blank separator between running and waiting requests is not selectable.

With help open, arrows / j / k and the wheel scroll its text; PgUp / PgDown or
Space scroll a page, and Home / End or g / G jump to the ends. `?` / Esc close
help; q still quits the dashboard. Help navigation preserves request selection and
the tables' scroll positions.

## Queue and details

The QUEUE panel lists the queue: the running requests, then a blank separator
and the waiting ones in the order the queue serves them. The details panel's title
follows the selection: **LEASE DETAILS** for a running request, whose lease holds
slots, and **REQUEST DETAILS** for a waiting or yielded request, which holds none.
The rows are the same either way.

The QUEUE columns are state, priority, label, slots, cpu use/share, time and pid.

**State** (#15) is one emoji per row, two cells wide:

| Emoji | ASCII | Row |
| --- | --- | --- |
| ▶️ | `>` | running |
| ❓ | `?` | running, asked to yield: a waiter wants its slots (wins over the rest) |
| 💤 | `z` | running, idle (below) |
| 🔥 | `!` | running above 1.5 times its share, as its red bar |
| 👨‍🦳 | `o` | a lease of an older client, from the earlier queue directory (👴 where a terminal does not join the sequence) |
| ⏸️ | `.` | waiting |
| ↩️ | `<` | yielded and back in the queue |

A running row shows the first of ❓, 💤, 🔥 and 👨‍🦳 that applies, else ▶️. A terminal that draws an emoji
narrower or wider than two cells shifts nothing after it: the live dashboard draws it over two spaces in the
row's style and puts the cursor on the next cell itself, with colour on or off; `--once` never moves the cursor.
At startup it measures how wide the terminal draws 👨‍🦳 (with colour off too) and uses 👴 where that is two
glyphs; a reply that arrives late, over a slow link, still switches it. An ASCII terminal
(and `--once` without UTF-8) gets the one-character tags.

**Priority** is a second two-cell column right after the state: ⚡ (`!` in ASCII) for an interactive request,
🐢 (`~`) for a batch one, and blank for normal, so every label starts in the same column. LEASE DETAILS and
REQUEST DETAILS put the same mark before the priority's name (`⚡ interactive`, `🐢 batch`, plain `normal`).

Each row has one
colour: a running request's row (label, slot cells, CPU numbers, time and pid) is
in the colour of its blocks in SLOTS, the filled part of its CPU bar included (red
when above 1.5 times its share and yellow while the lease is idle; the empty
part keeps the frame colour); a
waiting or yielded request's row is in the normal text colour. The selected
row keeps its highlight on top. **slots** draws one countable cell per slot
(`▪▪▪`, small squares whose side bearing keeps them apart). A count too wide for
the column, or above eight, shows the cells that fit and `+N` for the rest; an `-n all` request
that does not fit shows `all`. A request that holds no slots shows its state in
**cpu use/share**: `waiting #N` (its place in the order the queue serves) or
`yielded #N` (a lease back in the queue after a yield). Bars and states start at
the same column on every row, and the numbers line up around the slash. On a wide terminal the columns share the
spare width in proportion to their natural widths (the longest label, up to 40
cells, the widest slot cells, the bar and numbers, time and pid), and the CPU bar
grows with its column up to twice its length. On a narrow terminal pid
is dropped first, then time, then cpu use/share; the label is shortened before
any of them.

Time is elapsed run time for a running request and elapsed wait time for a pending
one. CPU use/share is **cores used / cores represented by the lease’s slots**;
it is an observation, not a hard CPU limit. Red indicates use well above its share.

**Idle leases** (#12). A running lease is idle when its command's process tree has used
less than the idle cores (0.1 by default) for the idle time (5 minutes), with no process
starting or ending, by its holder's own samples. Both are queue settings (Settings →
queue policy, or `slotfather order --idle-cpu C --idle-after S`). A short pause for the
network, a disk or a browser never lasts that long. Idle is one definition everywhere:
the bar turns yellow, the state column shows 💤, LEASE DETAILS
says `idle 23m since 10:12`, and `status` and `status --json` show it. A yieldable idle
lease is asked to yield while a request waits that its slots would start; the state then
shows ❓, which wins over idle, and LEASE DETAILS says `asked at 10:35 for …`. A lease
that cannot yield is only marked. Nothing is ever stopped. Idle is not a diagnosis of a hung process.

`owner pid` is the process holding the lease. The process table includes that
owner and its descendants, busiest first. Each row’s CPU is a percentage:
100% means one core. Memory is summed resident memory, so shared pages can be
counted in more than one process.

For the first waiting request:

- `start est` is its estimated start, by the held leases' expected ends now.
- `reserved` is the start it is promised once it is the head: the earliest
  `start est` since it became the head, which only moves earlier. Later work,
  of any class, may backfill only if its predicted finish, including the safety
  margin, is before this time, or it uses only slots the head will not need.
- `pass until` is the end of the current overtaking window, or with a limit
  set, the moment from which no later request may pass at all.

For example, `reserved 10:30` allows a later request at 10:10 only if it is
expected to finish before 10:30 with the safety margin. If a held lease runs on
past its estimate, `start est` moves to 10:45 but `reserved` stays 10:30: from
10:30 nothing passes. These are estimates, not a promised start.

For a waiting request further back, `start est` is when the queue is expected
to start it if nothing more comes (`status`' `est-start`). The priority line
shows the class a request is served in, with `aged from batch` when ageing moved
it up and `reprioritized from …` after `slotfather reprioritize`; QUEUE's ⚡ and
🐢 marks show the served class too. The help explains them alongside CPU, load, memory, admission and history.

## Events

EVENTS lists the grants, releases, waiters that left and reaped requests at the end
of `queue.log`, and two events without slots: `priority` (`slotfather reprioritize`, detail
`batch → interactive`) and `waiting` (a request that has waited the warn time says so, detail
`45m00s, est. 18:40`), newest first, in the columns time, event, slots, detail, label and
pid. **slots** uses the same cells as QUEUE in the event's
colour: filled `▪` for slots taken (granted) and hollow `▫` for slots handed back
(released, a held lease reaped) or never held (a waiter that left or was reaped).
**detail** comes from the request's line in `history.jsonl`: `waited 3m00s` (or
`no wait`) for a grant, `exit 1, ran 4m00s` for a release, `gave up after 15m00s`
for a waiter that left and `owner exited, ran 5m55s` for a reap. The history line
also tells a waiter that gave up from a lease that ended, which `queue.log` alone
cannot once it is longer than the part the dashboard reads. Detail is empty where
the history does not reach back.

## Colours

The dashboard takes its colours from the terminal's theme (#14), so it reads on light and dark backgrounds alike.
Text, bold, hotkeys, the cursor and the status line use the terminal's own foreground. Every chromatic colour is
the dashboard's own, the same whatever the theme, in a dark and a light variant that reads at 4.5:1 or more on the
background (moved in lightness where a background needs it):

| Token | Dark | Light | For |
| --- | --- | --- | --- |
| accent | `#4da3ff` | `#1f6feb` | the focused frame, links, `released` |
| ok | `#3ddc84` | `#15803d` | `granted`, "ok" status words |
| warn | `#ffc23d` | `#a16207` | an idle bar, warnings |
| bad | `#ff3b3b` | `#d1203a` | an over-share CPU bar, errors, `no admission` |
| legacy | `#b48cff` | `#6d3fd8` | a lease of an older client |

`Terminal` mode keeps the theme's ANSI colours for these, as the opt-out.
Muted text uses one token, which the dashboard derives itself where the terminal reports its
foreground and background: 42% of the way from the foreground to the background in OKLab, moved back toward the
foreground until it reads at 4.5:1, and kept at no more than 85% of the foreground's own contrast so it stays
dimmer. Many themes put ANSI bright black close to their background (Seti: 1.5:1; One Dark: 2.3:1). Where the
foreground itself reads under 4.5:1 (Solarized Light, 4.1:1) the muted token keeps 85% of it, dimmer than the text
and never stronger. Without both colours, and in `Terminal` mode, it is ANSI bright black. Unfocused frames and
the rules between sections are a little paler: 28% of the way on from the muted colour to the background, at least
2.5:1 (the muted colour itself where it is bright black).

What a palette lacks is mixed from the terminal's background and foreground, like `color-mix()` in OKLab: the
selected row's background (10% foreground), a bar's empty track (15%) and a free slot (20%). Selected text keeps its
colour. At startup the dashboard asks the terminal for its colours (OSC 10, 11 and 4, at most 100 ms, before the
kitty keyboard push) and subscribes to colour-scheme change notifications (mode 2031): when the terminal switches
between light and dark, the dashboard asks again and repaints. A terminal that does not answer is taken as light or
dark by `COLORFGBG`; with neither, the surfaces have no background colour: tracks are `░` in bright black, a free
slot is an outline and the selected row is bold beside its ▶.

Holder colours are the dashboard's own ("Neon"), two tokens per holder. The **fill** paints what needs no text
contrast: the SLOTS blocks, the QUEUE slot cells, the CPU bar, the selection marker and its tint. It is OKLCH
chroma 0.24, lowered only as far as sRGB requires at its hue, at lightness 0.73 on a light background and 0.80 on
a dark one. The **ink** paints the label, the numbers and the SLOTS labels: the same hue at chroma 0.16 at most,
from the fill's lightness darker (light background) or lighter (dark background) until it reads at 4.6:1 against
the reported background (a reference `#fdfcfa` or `#1e1e1e` where none is reported); on a dark background a fill
that already reads is its own ink. Eight hues are handed out in order: hot pink (350), orange (68), acid green
(138), spring teal (172), cyan (205), electric violet (288), magenta (318), green (152). No fill may sit within
OKLab ΔE 0.12 of our accent (the focused panel's frame), bad (a CPU bar over its share) or warn (an idle lease): a fill too close turns away a few degrees at a time (at most
30°) or moves a little in lightness, the smallest change first, and one with no room left is dropped. No fill
turns into blue, sky or indigo (OKLCH hues 225–280). The load chart and MACHINE's bars use our ramp from indigo
to red, `#5b5cf0`, `#8b5cf6`, `#c44fe0`, `#ee4a8c`, `#ff3b3b`, in OKLCH the short way round through purple and
magenta, each stop 0.06 deeper in lightness on a light background; `Terminal` mode keeps palette steps.

Settings → this dashboard → **Colour theme**: `Auto` is the above; `Terminal` uses the palette only, with nothing
mixed (the chart in palette steps); `Built-in` is the fixed 256-colour palette from before, for themes such as
Solarized whose bright black is close to the background. `SLOTFATHER_THEME` sets the starting value. `dashboard
--once` cannot ask the terminal: it reads `SLOTFATHER_TERMINAL_BG` and `_FG` (`#fdfcfa`) when given.

## Machine

- **health marks:** 🆗 before load 1m, memory and swap while they are fine, 🔥 while
  critical (memory low and swap low can block additional leases).
- **unleased load** (top border): estimated load not explained by the CPU share of held slots.
  It uses the lower of the one- and five-minute load averages.
- **admission** (top border): slots a generic new request can get immediately, considering
  queue order and memory checks. It is not a count of worker processes.
- **load 1m:** one-minute system load, shown beside the core count.
  Load counts runnable and waiting tasks; it is not CPU busy percentage and can
  exceed the number of cores.
- **memory / swap bars:** observed used capacity. Missing observations are shown
  as unavailable rather than as zero.
- **bar colours:** each filled cell of the load, memory and swap bars takes the
  history chart's ramp at its place along the bar, so a half-full bar
  runs from indigo to the middle and a nearly full one ends red. A bar is entirely
  the ramp's red, the red a full bar ends on, while its resource is critical: memory low, swap low
  (the conditions that put 🔥 before the row), or a one-minute load that leaves
  less than one slot's share of the cores free (by load alone: a fully leased but
  quiet machine is not red).

The histogram shows peak occupied slots per fixed time bucket over roughly an
hour, four rows high. The ramp's colour follows the capacity scale. The newest
bucket is incomplete. The line right under it is its time axis:

```
−60m              avg 5.2/8 · peak 8 · full 20%              now
```

The left label is the window the chart shows (its columns times the seconds each
stands for), in a friendly unit (−45s, −15m, −60m, −2h), under the first column;
`now` is under the last. Between them, in slots: `avg` is the mean held, `peak`
the most held and `full` the share of the window's time every slot was held. A
narrow chart drops `full`, then `peak`; the axis stays. Whole slots can still
produce fractional block heights when graph height and capacity differ.
Historical over-capacity values are retained in `peak`.

MACHINE does not scroll: one blank row under its top border, then load, memory and swap, the chart right under
them and its axis right under the chart. The chart keeps half a cell of air at each end instead of blank rows: its
scale runs from the bottom cell's mid-line (a column with any height shows `▀` there) to the top cell's (a full
column tops out at `▄`), in exact eighths between. Each row starts with its health: 🆗, or 🔥 when critical by the
rules that turn its bar red (`+` and `!` in ASCII). The load outside leases and the admission state sit on the
right of the top border, `unleased 3.5 · no admission`, the admission red while it blocks; a narrow border keeps
the admission alone. Short of rows, the chart gives up height first, four rows to three to two and
then entirely (never one row, and its axis goes with it), then the blank row. Shorter still, load, memory and swap
share one line,
`🆗 load ■■■■ 26.8/12 · 🔥 mem ■■■ 24.1/32G · 🆗 swap ■■ 7.6/9G`, with short bars in the same ramp (red when
critical), and on a narrow panel the numbers alone, a critical one in red; the narrowest drops the marks. Memory and swap never go while MACHINE is shown.

**One column** (under 110 columns): SLOTS, QUEUE and the details stack; below them MACHINE and EVENTS sit side by
side, MACHINE on the left at half the width and at least 44 columns, EVENTS in the rest. The row is MACHINE's own
height, its chart included; EVENTS scrolls in the same, and QUEUE and then the details take the height above it.
Where EVENTS would get under 38 columns (a terminal under 83 columns wide), it goes and MACHINE takes the whole
width; a hidden EVENTS is no Tab stop. Tab goes by columns as everywhere: QUEUE, the details, EVENTS.

Refreshing can reap dead queue entries, like `status`. Selecting and
scrolling never grants a lease, cancels work or kills a process.

## Settings

`s` opens a centered, scrollable Settings panel like Help. Its top block,
**STATUS**, is read-only and live: the queue directory; total, free and held
slots and waiting requests; the capacity and how it was found, as the dashboard's
last look read it under the queue lock; the CPU and memory budgets; and how many
slots are draining. Status rows have dim labels
and no selection cursor. Below a rule are the editable settings, one flat list,
each value with its unit: Capacity, Slots or GiB per slot, Queue order (Backfill
or Arrival), Backfill margin (×), Unknown job runtime, Overrun estimate (×),
Overtaking stops after (never: the head's reserved start alone), Interactive head start, Batch
held back, Batch promoted after, Normal promoted after, Warn after waiting (each 0: never), Idle when below
(cores), Idle for, Ask to yield after, and this dashboard's own Refresh every and
Colour theme. The selected setting has ▶, a soft background and a one-line
description under the list; the key line at the bottom is centred.

There is no draft. ←/→ (Space, or Enter, on a choice) shows the next value at
once, marked `pending`, and saves it once no key has moved it for one second;
Enter saves it now, Esc cancels it, and moving to another setting saves it first.
Stepping through the capacity modes therefore never applies the ones on the way.
Enter (or typing a digit) edits a number in place; Enter saves it and Esc leaves
the field without saving. What a pending or typed capacity would do shows under
it: the slots it gives, `will drain N` below the held slots, or `will start N
waiting` for a grow (by slot counts in queue order; backfill and memory checks
can still hold some back). Invalid input is shown there in red and is never
saved; a capacity above four slots per core is refused. A short line confirms
each save. `d` resets the selected setting to its default (either capacity field
returns to automatic capacity); `D` twice within four seconds resets every
setting. One hint line at the bottom lists the keys. A click selects a setting,
cancelling an edit of another one, and a click on the selected one changes or
edits it; the wheel scrolls inside the panel and does nothing outside it, and a
click outside closes the panel, as Esc does (never selecting requests underneath). `q` outside a field still quits. On a terminal too small
for the panel only Esc, `s` and `q` work.

Shared settings use the same rules and lock as `slotfather slots` and
`slotfather order`, and apply at the queue's next grant, also while requests run
or wait (see [configuration](configuration.md)). A shrink below the held slots
drains: running work keeps its slots and nothing more starts until no more than
the capacity is held; SLOTS shows `draining N` meanwhile. A capacity change is
refused, and nothing saved, when `capacity.json` changed since the panel read it
(another dashboard or `slotfather slots`); the panel then shows the new value. A
policy change merges
with the saved file under the lock, so a concurrent CLI change to another knob is
kept. The panel reads the shared settings again every second, so changes from
other clients show while it is open. Values tightened by this dashboard's
environment show `effective here:`. The refresh interval (0.1–60 seconds) is
never written to the shared queue, nor is the colour theme (see Colours). Another user's queue stays read-only.

## Lane context and details scrolling

The details panel has one scroll position for its facts, lane context and process
rows. Wheel anywhere inside the widget to scroll; the process table has no
independent viewport. Selecting another request resets the details to the top.

When the caller supplies `meta.lane` and `meta.lanefather.state`, the selected
request can show lane fields: project, phase, lane stage, branch, base,
ticket, receipt, review, claims and dependencies. Lane stage and request
status describe different lifecycles. Ticket numbers use OSC 8 hyperlinks;
open them with the terminal's link gesture, usually Cmd-click on macOS.
The worktree path appears before command as a muted folder link. Wrapped path
lines share the same destination. Plain-text snapshots contain labels and paths
without escape sequences.

Slotfather queries the installed `lanefather show ID --state-dir DIR --json`
only for selected work and caches the response for five seconds. The state
directory and lane ID together identify the lane. Older requests with only a
lane ID can resolve their project from the recorded working directory; lanefather
verifies that the resulting lane belongs to that repository. Missing references, an older
CLI, timeouts and unsupported responses appear as unavailable lane details;
they do not prevent viewing the request or its processes. No GitHub network
request is needed: ticket URLs come from the repository's GitHub origin.

Priority and optional ticket are request fields, displayed separately from caller
metadata. Known context gets named rows: phase, kind, check, revision, run on,
and session. Only custom fields remain in `meta`. A lane's `stage` is its
workflow lifecycle; a request's `phase` describes the current execution.
`receipt` shows the saved verification result and whether its tree is current;
`review` is the independent code review and its unresolved findings. The
occupancy histogram is five terminal rows high.
