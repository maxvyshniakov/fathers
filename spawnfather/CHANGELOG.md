# Spawnfather Changelog

Versions and release rules: [docs/agents/publishing.md](docs/agents/publishing.md#changelog-and-versions).

## Unreleased

### Added

- The sweep stops what a Claude Code session that has ended left behind (fathers#168). A process in a scratchpad or
  worktree that no entry owns, which launchd adopted after its shell exited, is stopped with its tree once two things
  are certain: its environment names the session it came from (`CLAUDE_CODE_SESSION_ID`, read with `ps -E` on macOS
  and from `/proc` on Linux), and that session is gone (its `SessionEnd` was recorded, or its own process is gone by
  pid and start time, and Claude Code's session registry names it with no running process). Anything less stays
  report-only, and the report says why. Nothing inside a Slotfather lease or held by an active entry is ever
  stopped, nor anything while Slotfather's leases cannot be read, nor an agent-browser daemon. `sweep --json` lists
  the stops in `stopped_unowned`, each with an `unowned.reaped` event; `sweep --dry-run` shows them as `would-stop`.
  The `SessionEnd` hook now also records the session's end.
- A lane can own its worktree and folders (fathers#171). `spawnfather own-resource --owner lane:PROJECT/ID --kind
  worktree|dir --id PATH` owns a lane's worktree, scratch or build directory with no TTL: neither the sweep nor an
  owner's end removes it, only `spawnfather reap --owner lane:PROJECT/ID`, a person's reap, or the folder going away.
  Owning it again for the same lane succeeds; two projects with the same lane id never cross. The new `dir` kind
  refuses `/`, the home folder, Spawnfather's state, symbolic links and any folder holding a Git repository or
  worktree.
- `reap` takes the requester's evidence for worktrees and folders: `--base REF` (work merged into a branch that is
  not promoted yet), `--disposable PATTERN` (ignored files that may go), `--override REASON` (lifts the guards about
  the work, never the ones that protect other work or Spawnfather itself; the stop event records the reason and what
  it lifted) and `--stop-inside` (first stops the leftover processes working inside, never a terminal's, an agent
  session's or anything below a live session). `reap --worktree PATH` removes a worktree no one owns through the
  same guards. `hand --owner lane:A --kind worktree --to lane:B` hands a lane's worktree to its fix lane.
  `capabilities --json` names this contract as `lifetimes`.
- `spawnfather reap --pid PID --started EPOCH --cmd CMD [--tree] --reason TEXT` stops a process no entry owns by
  the identity its caller read: its pid, the second it started and its command, all checked again before a signal
  (fathers#171). No sweep of Spawnfather's need have listed it; the rules of `reap --pid --force` still apply, and a
  pid that now runs another process, or none, is reported `gone`. Slotfather's `sweep --live` stops its leftovers
  through it. `--tree` takes the process's descendants too.
- `status --json` and `list --json` rows name the owner's kind (`owner_kind`) and a lane owner's project
  (`lane_project`), so a dashboard joins a lane's worktrees and folders to the right lane.

- A subagent's background Bash commands are owned on their own (fathers#166). `install.py --mastack` registers,
  through Mastack's lifecycle rewriters, `junkfather hook` for `run_in_background` Bash calls, with the permission
  rule `Bash(<bin>/junkfather background:*)` for the one line it puts before the agent's command; that line adopts
  the call's shell and leaves a watcher on it. The agent's command, its output and its exit status stay as they
  were. A subagent's call has a TTL set by `own.background_ttl` (2 h by default). A main-session call has no TTL.
  Both are cleaned up when the session ends. What the command `exec`s or leaves behind (`sleep 600 &`)
  is still its own. Only a call that is one explicit `junkfather own`, `own-resource` or `adopt` is left alone;
  `junkfather list; until ...; done` is owned. `list` and `status` show the owning subagent, and their rows gain
  `agent_type`, `auto` and `no_ttl`. On macOS `--mastack` also loads the sweep agent, as `--launchd` does.
- Reaping an adopted entry leaves out what another entry holds more firmly: an attached `junkfather own --keep-for
  session` run in an adopted shell keeps running, and a process adopted inside it is reaped by its own entry.
- junkfather owns resources that outlive the command that made them. `junkfather own --kind KIND -- CMD` runs CMD
  and owns the simulator, container, `tailscale serve` route or Safari Technology Preview instance it made (kinds
  `simulator`, `docker`, `tailscale-serve`, `safari-tp`); `junkfather own-resource --kind KIND --id ID` owns one that
  runs, and `--kind command --alive CMD --stop CMD` covers anything else. Their owner's end, their TTL and `reap`
  stop them through the kind's own command (`simctl shutdown`, `docker stop`, that serve route `off`, the app's
  quit), and the hourly sweep reports booted simulators, running containers and serve routes no one owns, without
  stopping them.
- `junkfather changelog --json [--since VERSION]` gives the installed release's changelog entries, newest first, for
  Godfather's what's new; the Unreleased ones come first, with no version and the installed commit, because an
  install from main is what they describe.
- `junkfather status`, `capabilities` and `changelog` print for people when run without `--json`: the `list` table
  with the unowned processes and the last sweep's time, one line of capabilities, the changelog by release. With
  `--json` they print what they always did.

- `junkfather settings` shows and changes three settings through Godfather's settings contract: the default TTL of
  `own`, `own-resource` and `adopt` (`own.default_ttl`), the sweep agent's interval (`sweep.interval`, applied when
  the agent is reinstalled) and the grace between SIGTERM and SIGKILL (`reap.grace`). `check`, `set` and `reset`
  take `--json` and `--revision`; a stale revision exits 3 and a read-only row exits 5. The command-line flags still
  win, and `capabilities --json` lists `settings` in its dashboard block.

- The hourly sweep says whose processes the busy cores belong to: a junkfather entry, a Slotfather lease (from
  `slotfather status --json`, left out with a note when Slotfather is missing), an agent session, or nobody, with
  the 10 busiest unattributed process trees. It stops nothing; the report is `cpu` in `sweep.json` and in
  `status --json`, whose version is now 2 (`capabilities --json` lists `versions: [2]`).
- The sweep removes agent-browser's session leftovers in `~/.agent-browser`: a session's files once its daemon and
  the pid in its name are gone and nothing changed for a day (downloaded browsers and saved sessions stay).
  `sweep --dry-run` lists them, and the report gives files and bytes.
- An agent-browser daemon that launchd adopted now belongs to the entry whose process started its session: it joins
  the entry's new `daemons` field and is reaped with it. One a Slotfather lease started counts for that lease in the
  CPU report, and one nobody owns that idled for an hour is reported, never stopped.
- Git worktrees are a resource kind: `junkfather own --kind worktree -- git worktree add ...` or `own-resource
  --kind worktree --id PATH` owns one, and its owner's end, its TTL or `reap` removes it with `git worktree remove`
  (and its branch when that branch's content is in the base), only when it is clean, merged by content, holds no
  ignored file beyond build output, no nested repository and no process; otherwise the stop fails and says why.
- The hourly sweep removes unowned worktrees of the repositories in the home folder that pass the same guards and
  were idle for a day, reports the others with branch, age and size in `unowned_resources`, and prunes worktree
  entries whose folder is gone. Worktrees of live Lanefather lanes are left to Lanefather.

### Changed

- Junkfather is now Spawnfather (fathers#170): the command is `spawnfather`, its state directory is
  `~/.local/state/spawnfather` (`$SPAWNFATHER_STATE`; legacy `$JUNKFATHER_*` environment variables still work), its
  releases live under `~/.local/share/spawnfather`, its hourly launchd agent is `com.spawnfather.sweep`, and its
  dashboard tag is `spwn`. Installing Spawnfather migrates an existing Junkfather state and settings directory in
  place behind a symbolic link, retires `com.junkfather.sweep`, updates Mastack and Claude Code hook entries while
  keeping the background rewriter off unless it was already enabled, and installs a forwarding `junkfather` wrapper
  that notes the rename on stderr and passes arguments and exit codes through unchanged.
- The minimum supported Python version is now 3.11 (fathers#218).
- The command line follows the CLI convention every tool shares (docs/cli.md, fathers#153). A refusal is one line,
  `junkfather: <reason>`, on stderr; a usage error adds `Try 'junkfather [COMMAND] --help'.` instead of argparse's
  usage text, and names the unknown command or option. `junkfather --version` prints what `junkfather version`
  does, and an unexpected exception prints one `internal error` line (exit 1) with its traceback in
  `logs/junkfather.log`. Exit codes moved: an entry, pid or `--alive` resource that does not exist exits 6 (it was
  1), a valid request the current state refuses exits 3 (it was 1: `adopt` or `reap --pid` of a protected or
  unlisted pid, `hand` or `extend` of an ended entry, an already owned resource), `own-resource --kind safari-tp`
  exits 2 (it was 1), and `sweep` while another sweep runs exits 75 (it was 0). The prefixes `junkfather adopt:`,
  `junkfather reap:`, `junkfather hand:`, `junkfather extend:`, `junkfather sweep:`, `junkfather own-resource:` and
  `junkfather settings:` are now `junkfather:`. Running `junkfather` without a command is a usage error on stderr
  rather than the help on stdout. Options are exact (`--dry` no longer means `--dry-run`). `own-resource` refusals
  of an id say why with the same codes: 2 for an id its kind cannot name, 6 when none runs, 3 for a stopped
  container or a main worktree. `reap` puts a failure's or a missing entry's line on stderr. A blank `--alive` or
  `--stop` is refused (2) again rather than registered.
- A reap no longer signals a leader it refused for a changed command: `known` lists the leader too, and the reap
  found it there again. Its recorded children are still stopped; `reap --force` still takes the leader.
- The command is now the second factor after pid and start time everywhere a process is signalled. A start time
  has one-second granularity, so a pid taken again within that second looked like the same process. Each member's
  command is recorded in the new entry field `known_seen`, and `reap --pid --force` refuses a pid whose command
  changed since the sweep listed it.
- A TTL reap that fails no longer makes the supervisor retry in a busy loop. It retries after 1 s, doubling up to
  60 s, and writes one `entry.reap_failed` event per run of failures.
- Entry files are written durably: fsync before the rename, then of the directory. A write that fails leaves
  the old file and no temp file, and one whose directory cannot be synced reports the error.
- junkfather lives in the `fathers` repository: install it with `python3 install.py junkfather` from a `fathers`
  checkout.

### Fixed

- Worktree and directory removal guards no longer refuse when an unrelated setuid/setgid host binary (such as macOS
  `/usr/bin/top`) has an unreadable working directory under non-root `lsof`; normal processes whose working directory
  cannot be read still refuse removal.
- `spawnfather hook` now treats `SessionEnd` as an immediate session end only for terminal reasons
  (`prompt_input_exit`, `clear`, `logout`, `bypass_permissions_disabled`, or an omitted reason). For `reason: "other"`
  (used both by T3 Code between turns and by Claude Code on CLI signal/`-p` exits), it schedules a detached reap that
  verifies the owning session process has actually exited before reaping or marking the session ended for `leftovers`,
  leaving live T3 threads' `--keep-for session` entries untouched; transient reasons such as `resume` are ignored.
  `spawnfather own --help` documents `--keep-for session` vs `hand --to lane:...` for long-lived servers in T3 threads.
  When `sweep` finds an owned process entry already `gone`, it emits an `entry.gone` event with safe metadata (`how`,
  `exit` if known, `log`, `log_size`, `log_mtime`) and records a bounded `log_tail` only in local `sweep --json` and
  `sweep.json` (fathers#245).
- Spawnfather now shares its Claude Code and Codex agent-session patterns with Slotfather through
  `fathers_common.agents`, and its process-table reads fail closed when `ps` omits the caller or exits 1 with an
  error message on stderr (fathers#146).
- A worktree whose disposable build output sat in an otherwise empty folder (`desktop/node_modules/`) was kept as
  holding ignored files; only the ignored paths themselves count now.
- A worktree with submodules is refused with a clear reason instead of a failed `git worktree remove`, and a dead
  agent's worktree lock no longer keeps a worktree.

- Processes belonging to an entry currently being reaped are now treated as owned during `junkfather sweep` and
  skipped as already reaping by `junkfather reap --pid` instead of being reported as unowned; `junkfather reap
  --dry-run` on a resource whose liveness cannot be determined now reports `would-fail` to match a real reap; and
  worktrees of released Lanefather lanes remain treated as live until Lanefather marks the lane done (fathers#231).
- An attached command whose process-table read fails transiently as it exits now retries cleanly to the exited state
  instead of reporting zero remaining processes, a background-shell watcher backs off for 1 s rather than spinning when
  the process table cannot be read at expiry, shortening an entry's TTL during reap-failure backoff takes effect at the
  new deadline, and detached agent-browser daemons keep an auto-owned background entry alive after its shell exits
  (fathers#226).
- `junkfather reap --owner` and `junkfather reap --lane` now recheck ownership and lane under the store lock even
  without `--expired` or `--lifecycle`, and resource TTL retry streaks record `last_error` without repeating
  `resource.stop_failed` events while avoiding redundant process-table reads (fathers#217).
- `junkfather reap --pid PID --force` now goes straight to the unowned-process guard instead of matching live
  entries first, so a PID adopted or running under an active entry is refused rather than reaping that entry;
  `reap` records the exact lock-time skip reason (`owner changed`, `not expired`, `already exited`, `another reaper
  has it`, or a guard's refusal) without reloading outside the lock; `owner.detect` checks for a Claude ancestor
  once on a miss; and `resources.unowned` drops an unreachable `safari-tp` check (fathers#210).
- `events.jsonl` strips Unicode line and paragraph separators (`U+2028`, `U+2029`), caps general strings at 1000
  characters (400 for commands) and bounds the `events.lock` wait to 1 s, and `install.py` serializes installs under
  `.install.lock` and verifies existing releases through the shared `fathers_common` kit (fathers#117).
- `junkfather changelog --json --since` accepts build-suffixed versions and orders matching release headings by file
  order, and unindented prose ends a bullet's continuation lines as the shared changelog contract requires
  (fathers#119).
- The background-shell watcher re-reads a newly joined child's command after event registration so an `exec` between
  the process-table snapshot and kqueue/pidfd subscription is recorded at once (fathers#186).
- The background-shell watcher enforces the entry's finite TTL deadline while the owning agents are quiet, rechecking
  expiry under the store lock so a concurrent `extend` wins, and `junkfather extend` clears `no_ttl` on a
  main-session background entry (fathers#180).
- A lifecycle reap (`SubagentStop`, `SessionEnd`) rechecks the entry's owner under the store lock before marking it
  for reap, so an entry handed to another session after selection is skipped (fathers#189). The owner check now
  also holds when the reap carries a second guard (a TTL's), and covers resource entries the same way.
- Auto-owned background Bash calls survive `SubagentStop`, even when the hook reaps an explicitly owned runner
  that started them. TTL and session-end cleanup still apply (fathers#176).
- Usage errors name an unknown option even when it follows an option with a missing value (fathers#175).

## 0.1.0 - 2026-11-12

### Added

- `junkfather own` starts a command in a process group of its own and records its owner (Claude Code session,
  subagent, lane; Codex thread; or the calling process), TTL, pgid, command and cwd. Attached it passes output and
  exit status through and stops the group on SIGTERM, SIGINT or SIGHUP; `--detach` runs it under a supervisor with
  output to a log and returns at once. The TTL is enforced on the spot.
- `junkfather adopt PID` owns a process that already runs.
- `junkfather reap` ends entries by id, owner, subagent, lane or pid: SIGTERM, a grace period, SIGKILL, only to
  processes whose start time and command were verified, so a reused pid is never signalled. `--keep-for session`
  entries survive their subagent's stop; `junkfather hand ID --to SESSION` gives an entry to another session;
  `junkfather extend ID --ttl D` renews its TTL.
- `junkfather hook` for Claude Code's `SubagentStop`, `SessionEnd`, `Stop` and `PreToolUse` (which subagent ran a
  command), registered through Mastack's lifecycle hooks by `install.py --mastack`.
- `junkfather sweep` reaps expired entries and entries whose owner session is gone (Claude Code's session registry,
  the recorded process), and reports, without stopping them, unowned processes in agent scratchpads and worktrees;
  `reap --pid PID --force` stops one it listed. `sweep --plist` and `install.py --launchd` run it hourly.
- `junkfather list [--json]` shows what runs, whose it is, its age and its TTL left.
- `events.jsonl` records every start, adoption, exit, extension, hand-over, reap and sweep in Godfather's event
  schema version 1; `junkfather status --json` and `junkfather capabilities --json` give Godfather's dashboard
  its snapshot and contract.
