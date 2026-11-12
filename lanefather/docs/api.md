# Machine-readable commands

The commands other tools read. Each answers JSON with a version field `v`; a new field is added without a version
change, a removed or changed one changes `v`.

| Command | Reader | Contract |
| --- | --- | --- |
| `lanefather capabilities --json` | project launchers, Godfather | [reference](reference.md#installed-runtime-contract) |
| `lanefather status --json` | Godfather | [reference](reference.md) |
| `lanefather settings --json` | Godfather | Godfather's settings contract |
| `lanefather results key\|lookup\|record` | Repo-Kit's gates and CI | [results](results.md) |
| `lanefather stats --json` | people, dashboards | below |
| `lanefather changelog --json` | Godfather's what's new | below |

## `lanefather changelog --json [--since VERSION]`

The installed release's CHANGELOG.md (install.py ships it inside the release; a checkout reads its own) as
entries, for Godfather's what's new ([its contract](../../godfather/docs/contracts/changelog.md)). Read-only, no
Git and no state: it reads one file.

```json
{"v": 1, "tool": "lanefather", "version": "0.1.0",
 "source": "/Users/me/.local/share/lanefather/releases/0.1.0-1a2b3c4d5e6f/lanefather/CHANGELOG.md",
 "entries": [
   {"version": null, "date": null, "unreleased": true, "source": "1a2b3c4d5e6f…", "section": "Added",
    "text": "Check results belong to a tree and a plan …"},
   {"version": "0.1.0+1138486", "date": "2026-11-12", "section": "Added", "text": "Every ledger entry …"}]}
```

- `source` (top level) is the file read, `null` when the release has none; then `entries` is empty.
- Entries are newest first: the `## Unreleased` bullets first, then one per bullet of each dated release
  (`## x.y.z[+build] - YYYY-MM-DD`). An Unreleased entry has `version` and `date` null, `unreleased: true` and
  `source`, the installed release's commit (null in a checkout), so a reader can tell what is new since the commit
  it last saw. `section` is the `###` heading; `text` is the bullet with its continuation lines joined by one space,
  markdown as written.
- `--since VERSION` keeps the releases newer than VERSION, and Unreleased.
- Releases installed before 0.2.0 all said 0.1.0; the changelog tells them apart as `0.1.0+<commit>`.

## `lanefather stats [--days N] [--lane ID] --json`

`{"v": 1, "days", "lane", "lines", "checks": [{"check", "runs", "failed", "flakes", "p50", "p90",
"total_seconds"}]}` from the run history (`<state>/history/`, lanefather.history), heaviest first. `--days 0` reads
everything kept (twelve months).
