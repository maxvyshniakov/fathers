# Security

## Reporting a vulnerability

Report it privately through GitHub: this repository's **Security** tab → **Report a vulnerability**. Please do not
open a public issue for a vulnerability. Include the affected commit, what an attacker controls, and the smallest
reproduction you have. Lanefather is maintained by one person in spare time; expect an acknowledgement within about
a week, and a fix on `main` when the report holds.

Only the latest commit on `main` is supported. There are no packaged releases.

## What is in scope

- The landing lock and `hooks/reference-transaction`: anything that lets an agent session move `dev` or `main`
  without holding the lock, or lets one lane's command act on another lane's worktree or branch.
- Cleanup (`done`, `gc`): anything that makes Lanefather remove a dirty, unmerged, in-use or unowned worktree,
  branch or directory, or a path outside the ones its records and configuration name.
- The installer and installed-release verification: anything that makes `lanefather capabilities` or a project
  launcher accept bytes that differ from the release manifest.
- State handling: a lane record, receipt, queue file or command output that makes Lanefather run a command or
  write a path the project's configuration did not name.

## What is not

Lanefather runs the project's own configured commands (`[checks]`, `[promote]`, `[gc]`, `[hooks]`) with the
invoking account's privileges. It is not a sandbox: a repository's `.lanefather.toml`, its verifier, its hooks and
the lane branches you choose to check or land are trusted code.

Automatic reviews send lane diffs and context to the configured Codex or Claude CLI. What those providers do with
the data is governed by your account with them; see the README's notes on AI review before landing.
