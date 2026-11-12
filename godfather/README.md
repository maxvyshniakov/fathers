# godfather

One terminal dashboard for the tools that share a development machine:
[Slotfather](https://github.com/maxvyshniakov/slotfather) (capacity leases),
[Lanefather](https://github.com/maxvyshniakov/lanefather) (parallel agent lanes) and junkfather (the long-lived
processes agents start).

**Status: planned, not built.** [SPEC.md](SPEC.md) is the agreed design; the issues carry the slices.

## What it will do

- Keep Slotfather's dashboard as the main screen, with its layout, colours, keys and mouse unchanged: the TUI
  framework moves here, and `slotfather dashboard` opens Godfather.
- Show lanes and processes as summaries on that screen, linked from each lease's details, with full Lanes and
  Processes pages on `2` and `3`.
- Merge the tools' events into one feed with a source column and a filter.
- Edit each tool's settings through that tool's own `settings` CLI, so the tool keeps its validation.
- Offer a few safe row actions (reprioritize, ask to yield, reap an orphan, extend a TTL, open a lane's ticket, log
  or worktree, park a lane), each run by the owner tool's CLI; irreversible ones ask first.

Godfather reads; the tools own their state. A tool that is not installed simply has no panels.

## How the tools talk to it

| Contract | Command or file |
| --- | --- |
| Discovery | `<tool> capabilities --json` |
| Snapshot | `<tool> status --json`, polled in the background |
| Events | `events.jsonl` in the tool's state directory, one shared versioned schema |
| Settings | `<tool> settings --json`, `settings check`, `settings set` |

The event schema and a vendorable validator for the tools' tests live in this repository.

## Install

Not yet. It will be `python3 install.py` from a checkout, standard library only, Python 3.9 or later, like
Slotfather. It is meant for workstations, not CI runners.

## Versions

[CHANGELOG.md](CHANGELOG.md) uses the Sid-Kit format: one `## Unreleased`, then `## x.y.z - YYYY-MM-DD`, newest
first, with sections only when they have entries, in the order Breaking, Added, Changed, Deprecated, Removed,
Fixed, Security. Before 1.0 a breaking or broad behaviour change takes a minor version, anything else a patch.
