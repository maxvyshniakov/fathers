# What Godfather reads for what's new

The footer's `Godfather · what's new?` and the what's new overlay (fathers#102) show one timeline of the changelog
entries of every tool. Godfather gets them from each tool's CLI alone: it never imports a tool and never reads a
tool's files. Its own entries come from the CHANGELOG.md it ships with.

## `<tool> changelog --json [--since VERSION]`

```json
{"v": 1, "tool": "junkfather", "version": "0.1.0",
 "source": "/Users/me/.local/share/junkfather/releases/0.1.0-1a2b3c/junkfather/CHANGELOG.md",
 "entries": [{"version": "0.1.0", "date": "2026-11-12", "section": "Added", "text": "`junkfather own` starts …"}]}
```

- `version` is the installed release; `source` the changelog file read, `null` when the release has none (then
  `entries` is empty).
- `entries` are newest first, one per bullet of every dated release (`## x.y.z - YYYY-MM-DD`); Unreleased is left
  out. `section` is the `###` heading (Added, Fixed, …). `text` is the bullet with its continuation lines joined by
  one space, markdown as written; Godfather takes out bold, code and links.
- `--since VERSION` keeps only releases newer than VERSION. Godfather asks without it: the overlay shows the whole
  history.
- `v` is 1. A reply without `v` is read as 1; another `v`, a `tool` other than the one asked, or a malformed
  `entries` is no reply. An entry missing a string field is skipped.
- It is read-only and fast: Godfather runs it when it starts and each time the overlay opens, with a 2 s timeout.

Who provides it: junkfather (fathers#105), slotfather (fathers#103), lanefather (fathers#104).

## Without `changelog --json`

A tool that does not answer (a non-zero exit, no JSON, a timeout) shows its version only, from the first of these
that answers: `<tool> version --json` (`{"version": …}`), then `<tool> capabilities --json` (`version`). Its row
in the overlay says `versions only — update <tool>`, highlighted, and it adds nothing to the timeline. A tool not
on PATH is `not installed`. A tool that answers neither is shown with its problem.

Godfather's own row says `↻ restart to update` while a newer install waits for a restart (the footer's `↻`).

## Read state

`$XDG_STATE_HOME/godfather/news.json` (default `~/.local/state/godfather/news.json`), Godfather's own:

```json
{"v": 1, "read": {"godfather": "0.1.0", "slotfather": "0.2.0", "junkfather": "0.1.0"}}
```

An entry is unread while its version is newer than its tool's `read` version; with no file, or no version for a
tool, all of that tool's entries are unread. Closing the overlay sets each tool's `read` to its newest entry. The
file is written atomically; one that cannot be read or written leaves everything as it was.
