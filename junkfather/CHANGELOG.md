# Junkfather Changelog

Versions and release rules: [docs/agents/publishing.md](docs/agents/publishing.md#changelog-and-versions).

## Unreleased

### Added

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
  rather than the help on stdout.
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
