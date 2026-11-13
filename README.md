# fathers

Four small command-line tools for a development machine shared by several terminals, projects and coding agents.
Each is a separate package with its own version, changelog, documentation and tests, and installs on its own.

| Package | What it does |
| --- | --- |
| [slotfather](slotfather) | One queue for expensive work: builds, tests and agents request machine capacity (slots) before starting. |
| [lanefather](lanefather) | Parallel changes in one Git repository: a worktree and a path claim per task, checks, one integration branch, review and cleanup. |
| [junkfather](junkfather) | Owns the long-lived processes and resources agents start, and ends them when their owner ends or their time is up. |
| [godfather](godfather) | One terminal dashboard for the three: their snapshots, settings and one merged event feed. |

The installed commands use only the Python standard library (3.9+; Lanefather 3.11+) on macOS or Linux.

## Install

```sh
git clone https://github.com/maxvyshniakov/fathers.git
cd fathers
python3 install.py slotfather      # or lanefather, junkfather, godfather; arguments after the name go to its installer
```

`python3 install.py PACKAGE` runs `PACKAGE/install.py`, which copies only that package into an immutable release and
swaps its command in one rename, so running commands keep their release. Each package's README has its options; a CI
runner installs Slotfather alone (`python3 slotfather/install.py --user runner --bin /usr/local/bin`).

## Layout

- `slotfather/`, `lanefather/`, `junkfather/`, `godfather/`: one package each, with its own `install.py`, version,
  `CHANGELOG.md`, `README.md`, `GLOSSARY.md`, `docs/` and `tests/`, and the same lint gate, `tools/check.py`.
- `common/`: code the packages share today. It holds [`eventcheck.py`](common/eventcheck.py), the validator of the
  shared event schema that every package's tests import.
- `tools/`: the repository's check entry ([`checks.py`](tools/checks.py)), Lanefather's verify command
  (`lane-check`) and the shared Ruff rules (`ruff.toml`).
- [`GLOSSARY.md`](GLOSSARY.md): the terms the tools share; each package's glossary holds its own.
- [`notes/sha-map.md`](notes/sha-map.md): the packages moved here from their own repositories with their history;
  the map gives the new commit for each old one.

CI runs one job per package on GitHub-hosted runners, triggered by changes under that package or `common/`.

## Develop

[Repo-Kit](https://github.com/maxvyshniakov/repo-kit) runs the checks; it needs Bun 1.4.4 and a `GITHUB_TOKEN` that
reads GitHub Packages (`export GITHUB_TOKEN=$(gh auth token)`). It is a development tool only.

```sh
bun install              # once: the kit, and the pre-push gate in .githooks
bun run verify           # the checks of the packages changed since origin/main; FATHERS_CHECKS=all for every one
```

Each package's checks also run from its directory: `python3 tools/check.py` (Ruff and Pyright) and its tests.
[docs/agents/checks.md](docs/agents/checks.md) has the details.

## License

MIT, see [LICENSE](LICENSE).
