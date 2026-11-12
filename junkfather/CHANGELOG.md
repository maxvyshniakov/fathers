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
  Godfather's what's new.

### Changed

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
