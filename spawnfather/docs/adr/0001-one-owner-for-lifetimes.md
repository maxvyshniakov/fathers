# One owner for lifetimes: a shared process reader and an owner-scoped API

Status: accepted (fathers#171, 2026-11). Context: two tools removed worktrees (Lanefather's `done`/`gc` and
Spawnfather) and two stopped processes (`slotfather sweep --live` and `spawnfather reap`), each with its own copy of
the `ps`/`lsof` reader and its own guards. The owner decided that each responsibility has one owner: Slotfather the
capacity, Lanefather the work, Spawnfather the lifetime, and `common/` the shared reading.

## The process reader: one complete table in `common/`

**Decision.** `fathers_common.proc` is the one reader: a full `Proc`/`Table` model (pid, parent, group, user,
terminal, start, command, CPU, memory; trees and groups), read-only and fail-closed (an unreadable table, or one
that leaves out its caller, raises, and is never "nothing runs"). It reads `ps` in the C locale and UTC and renders
the start text in the clock each tool stores: UTC for Spawnfather, local time for Slotfather, byte for byte as before.
The identity that crosses between tools is `{pid, started, cmd}`: the epoch second (within `EPOCH_SLACK`, one
second, since Linux derives it from the uptime) and the command, which tells apart a pid taken again in the same
second. Slotfather's three cheap samplers (usage, CPU, the queue's identity writer) keep a narrow `sample()` call.

**Rejected: primitives and a codec, each package keeping its own table.** It would have left two parsers and two
facades, so the duplication would move rather than go, and every comparison of identities would stay per package.
Its one advantage, no risk to Slotfather's local start text, is covered by tests that pin the rendering.

## The API Lanefather and Slotfather call: owner-scoped lifetimes

**Decision.** Spawnfather owns what a lane holds. Lanefather registers a lane's worktree and folders as owner
`lane:<project>/<id>` (`own-resource --owner`, no TTL; owning again succeeds, so `done`, `drop` and `gc` register on
demand before they reap), and asks `reap --owner lane:...` with its evidence (`--base`, `--disposable`, `--override`,
`--stop-inside`) to remove them; `reap --worktree` removes an unowned one through the same guards. The guards split
into integrity guards, never lifted, and work guards, which the requester's evidence or a recorded override lifts.
Lanefather keeps its own refusals and decides them under its own lock; Spawnfather checks again when it acts and
never calls Lanefather. Slotfather keeps deciding what its sweep may stop under its leases and hands each tree to
`reap --pid --started --cmd --tree --reason`, which checks the identity and the `--force` policy again; for one
release a live sweep without Spawnfather still stops by itself, with a deprecation warning. Status rows name
`owner_kind` and `lane_project`, so Godfather joins a lane's entries by project and id.

**Rejected: stateless removal verbs** (`spawnfather remove --worktree PATH --dir PATH ...`, no registration).
Lanefather would have stayed the only record of what a lane holds, so Godfather's `lane` column and LANE DETAILS
would still guess from working directories, and Spawnfather's sweep would still read Lanefather's files to leave lane
worktrees alone. It avoids drift between a lane record and its entries, which register-on-demand covers instead.

## Consequences

- Only Spawnfather signals processes or removes worktrees and folders (Slotfather after its forwarding release).
- An older Spawnfather that does not list `identity` in `capabilities --json` (`lifetimes.reap`) gets the
  deprecated fallback; the release after drops it, and a live sweep without Spawnfather stops nothing.
- Lane entries are never reaped for a TTL or a departed owner: only the owner's request, a person, or the folder
  going away ends them.
