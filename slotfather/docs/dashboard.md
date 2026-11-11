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
| ↑ / ↓ or j / k | Select a running or waiting request. |
| Click a request | Select that row. |
| Mouse wheel | Scroll the table under the pointer. |
| g / G, Home / End | First or last request. |
| p or Space | Pause/resume updates. |
| r | Refresh. |
| m | Toggle native text selection; copying freezes updates and disables app mouse handling. |
| ? | Open scrollable help. |
| s | Open Settings. |
| q | Quit. |

Only the selected request has a triangle and a soft background. The blank separator
between running and waiting requests is not selectable. Overflow counts appear in
the tables’ top and bottom padding rows.

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

The QUEUE columns are label, slots, cpu use/share, time and pid. Each row has one
colour: a running request's row (label, slot cells, CPU numbers, time and pid) is
in the colour of its blocks in SLOTS, the filled part of its CPU bar included (red
when above 1.5 times its share and yellow while nearly idle, as before; the empty
part keeps the frame colour); a
waiting or yielded request's row is white, the normal text colour. The selected
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
it is an observation, not a hard CPU limit. Yellow indicates a nearly idle
holder; red indicates use well above its share. After sustained low activity,
status includes `idle for …`; this is not a diagnosis of a hung process.

`owner pid` is the process holding the lease. The process table includes that
owner and its descendants, busiest first. Each row’s CPU is a percentage:
100% means one core. Memory is summed resident memory, so shared pages can be
counted in more than one process.

For the first waiting request:

- `start est` is its estimated start. Later work may backfill only if its
  predicted finish, including the safety margin, is before this time, or it
  uses only slots the protected request will not need.
- `pass until` is the end of the current overtaking window. With `start est`
  shown, no later request may pass after this deadline. Without it, stricter
  backfill rules may follow the initial window.

For example, `start est 10:30` and `pass until 10:20` allow a later request at
10:10 only if it is expected to finish before 10:30 with the safety margin.
At 10:21 it cannot pass. These are policy deadlines and estimates, not a promised
start. The help explains them alongside CPU, load, memory, admission and history.

## Events

EVENTS lists the grants, releases, waiters that left and reaped requests at the end
of `queue.log`, newest first, in the columns time, event, slots, detail, label and
pid. **slots** uses the same cells as QUEUE in the event's
colour: filled `▪` for slots taken (granted) and hollow `▫` for slots handed back
(released, a held lease reaped) or never held (a waiter that left or was reaped).
**detail** comes from the request's line in `history.jsonl`: `waited 3m00s` (or
`no wait`) for a grant, `exit 1, ran 4m00s` for a release, `gave up after 15m00s`
for a waiter that left and `owner exited, ran 5m55s` for a reap. The history line
also tells a waiter that gave up from a lease that ended, which `queue.log` alone
cannot once it is longer than the part the dashboard reads. Detail is empty where
the history does not reach back.

## Machine

- **memory / swap status:** white is normal, red is a condition that can block
  additional leases. The memory bar also reflects memory health.
- **unleased load:** estimated load not explained by the CPU share of held slots.
  It uses the lower of the one- and five-minute load averages.
- **admission:** slots a generic new request can get immediately, considering
  queue order and memory checks. It is not a count of worker processes.
- **load 1m:** one-minute system load, shown beside the core count.
  Load counts runnable and waiting tasks; it is not CPU busy percentage and can
  exceed the number of cores.
- **memory / swap bars:** observed used capacity. Missing observations are shown
  as unavailable rather than as zero.

The histogram shows peak occupied slots per fixed time bucket over roughly an
hour. Blue-to-orange colour follows the capacity scale; `s/col` gives the time
represented by each column. The newest bucket is incomplete. Its legend gives
average occupancy, peak slots and the share of time capacity was full. Whole
slots can still produce fractional block heights when graph height and capacity
differ. Historical over-capacity values are retained in the legend.

Refreshing can reap dead queue entries, like `status`. Selecting, pausing and
scrolling never grants a lease, cancels work or kills a process.

## Settings

`s` opens a centered, scrollable Settings panel like Help. Its top block,
**STATUS**, is read-only and live: the queue directory; total, free and held
slots and waiting requests; the capacity and how it was found, as the dashboard's
last look read it under the queue lock; the CPU and memory budgets; and how many
slots are draining. Status rows have dim labels
and no selection cursor. Below a rule are the editable settings, in three
groups: shared capacity (mode, and total slots or GiB per slot), queue policy
(order, safety multiplier, overtaking limit, unknown runtime, overrun factor,
interactive advance, batch delay, yield delay) and this dashboard's refresh
interval. The selected setting has ▶, a soft background and a one-line
description under the list.

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
edits it; the wheel scrolls inside the panel, and clicks outside it do not select
requests underneath. `q` outside a field still quits. On a terminal too small
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
never written to the shared queue. Another user's queue stays read-only.

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
