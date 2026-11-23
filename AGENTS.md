# fathers

- Four packages, one repository: work inside one package's directory and follow its `AGENTS.md`, which links what
  differs there. A change to `common/` runs every package's checks.
- Repository-wide: [checks](docs/agents/checks.md), [publishing](docs/agents/publishing.md),
  [issue tracker](docs/agents/issue-tracker.md), [orchestration](docs/agents/orchestration.md),
  [domain](docs/agents/domain.md).
- Shared terms are in [GLOSSARY.md](GLOSSARY.md); a package's own terms go in its own glossary.
- In UI text and prose a tool's name is capitalised (Godfather, Slotfather, Lanefather, Spawnfather); commands, paths and code stay lowercase (`slotfather changelog --json`).
- Each package keeps its own version and `CHANGELOG.md`; a change a user would notice adds an entry to the changed
  package's changelog in the same commit.
- The installed commands and the repository's checks (`tools/`) stay standard-library Python; Ruff and Pyright are
  development tools only. No Node, Bun or `package.json`.
- Shared code: a behaviour two packages need goes to `common/` (fathers#117), not into a second copy; the copies that
  exist today (atomic writes, event writers, installers, changelog parsers, process identity) are being folded there.
