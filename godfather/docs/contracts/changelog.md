# What Godfather reads for what's new

The footer's `Godfather what's new?` and the what's new overlay (fathers#102) show one timeline of the changelog
entries of every tool. Godfather gets them from each tool's CLI alone: it never imports a tool and never reads a
tool's files. Its own entries come from the CHANGELOG.md it ships with.

## `<tool> changelog --json [--since VERSION]`

```json
{"v": 1, "tool": "spawnfather", "version": "0.1.0",
 "source": "/Users/me/.local/share/spawnfather/releases/0.1.0-1a2b3c/spawnfather/CHANGELOG.md",
 "entries": [{"version": null, "unreleased": true, "date": null, "source": "1a2b3c4d5e6f", "section": "Added",
              "text": "`spawnfather changelog --json` …"},
             {"version": "0.1.0", "date": "2026-11-12", "section": "Added", "text": "`spawnfather own` starts …"}]}
```

- `version` is the installed release; `source` is the resolved path of the changelog file read, `null` when the
  release has none or it cannot be read (then `entries` is empty).
- `entries` are newest first, in file order, one per `- ` bullet under a `### <section>` heading in `## Unreleased`
  (or `## [Unreleased]`, case-insensitive) and in every dated release (`## <version> - YYYY-MM-DD`, where `<version>`
  starts with `x.y.z` and may carry a `-prerelease` or `+build` suffix, optionally in `[...]`). Anything before the
  first release heading, under `# ` titles, under other `## ` headings, or before a release's first `### <section>` is
  left out; `* ` bullets and empty `- ` bullets are not entries.
- `section` is the trimmed `### ` heading (`Added`, `Fixed`, …; an empty `###` heading starts no section). `text` is
  the bullet with its two-space-indented continuation lines (`  …`) joined by one space, markdown as written;
  Godfather takes out bold, code and links. Blank lines leave the current bullet open for indented continuations; any
  unindented non-blank line (loose prose, `* ` bullets, `#### ` subheadings) ends continuation for that bullet.
- Installs come from main, so the Unreleased section is what is installed. Its entries come first, as
  `{"version": null, "unreleased": true, "date": null, "source": "81abd86…", "section": …, "text": …}`: `source`
  is the installed commit (`null` from a checkout) and `date` the install date where the tool knows it, else
  `null`. Dated release entries are `{"version": …, "date": "YYYY-MM-DD", "section": …, "text": …}` and omit
  `unreleased` and `source`. The overlay titles Unreleased entries `Slotfather · unreleased · installed 81abd86`,
  above the tool's releases, by install date or, without one, at the top.
- `--since VERSION` requires `VERSION` to match `\d+\.\d+\.\d+\S*` (otherwise exit 2) and keeps the Unreleased
  entries plus releases newer than `VERSION`: when `VERSION` matches a dated release heading in the file, every dated
  release above that heading in file order (so a build release such as `0.1.0+7eca44a` above `0.1.0` counts as newer
  than `0.1.0`); otherwise, every dated release whose numeric `(major, minor, patch, …)` version tuple (before `-` or
  `+`) is greater than `VERSION`'s. Godfather asks without `--since`: the overlay shows the whole history.
- `v` is 1. A reply without `v` is read as 1; another `v`, a `tool` other than the one asked, or a malformed
  `entries` is no reply. An entry missing a string field is skipped.
- It is read-only and fast: Godfather runs it when it starts and each time the overlay opens, in the background,
  with a 2 s timeout per call (a tool that times out on every fallback costs up to 6 s before its row settles).

Lanefather also gives its `## Unreleased` bullets, first, as `{"version": null, "date": null, "unreleased": true,
"source": "<the installed commit>", ...}` (owner decision, fathers#104), so its news since the installed commit
shows before it has a release; its releases before 0.2.0 carry a build suffix (`0.1.0+7eca44a`), since each
installed as 0.1.0; Godfather orders releases by date, then in the order a tool gives them, never by such a
suffix. Its contract: [lanefather/docs/api.md](../../../lanefather/docs/api.md).

Who provides it: Spawnfather (fathers#105), Slotfather (fathers#103), Lanefather (fathers#104).

## Without `changelog --json`

A tool that does not answer (a non-zero exit, no JSON, a timeout) adds nothing to the timeline; Godfather still
reads its version, from the first of these that answers: `<tool> version --json` (`{"version": …}`), then
`<tool> capabilities --json` (`version`). The overlay is only the timeline (fathers#102): it lists no versions, so
a tool without the contract, one not on PATH, or one that does not answer is simply absent from it. A newer
Godfather install waiting for a restart is the footer's `↻`.

## Read state

`$XDG_STATE_HOME/godfather/news.json` (default `~/.local/state/godfather/news.json`), Godfather's own:

```json
{"v": 1, "read": {"godfather": "0.1.0", "slotfather": "0.2.0", "spawnfather": "0.1.0"}}
```

An entry is unread while its version is newer than its tool's `read` version; an Unreleased one while `read` is
not that install's `version+commit` (`"0.1.0+81abd86"`), so a later install's Unreleased entries are new again.
Without a commit (a checkout, `source` null) the token is `version+unreleased`, and its Unreleased entries stay
read until the version changes. With no file, or no version for a tool, all of that tool's entries are unread.
Closing the overlay, `q` while it is open included, sets each tool's `read` to its newest entry: `version+commit`
where it has Unreleased entries. The file is written atomically; one that cannot be read or written leaves everything as
it was.
