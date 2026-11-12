# fathers

- Four packages, one repository: work inside one package's directory and follow its `AGENTS.md` and
  `docs/agents/` (checks, publishing, domain) where it has them; Godfather's are its README (Develop, Versions) and
  SPEC.md. A change to `common/` runs every package's tests.
- Shared terms are in [GLOSSARY.md](GLOSSARY.md); a package's own terms go in its own glossary.
- In UI text and prose a tool's name is capitalised (Godfather, Slotfather, Lanefather, Junkfather); commands, paths and code stay lowercase (`slotfather changelog --json`).
- Each package keeps its own version and `CHANGELOG.md`; a change a user would notice adds an entry to the changed
  package's changelog in the same commit.
- Run heavy checks and test suites through `slotfather run -n 1 -- ...`.
