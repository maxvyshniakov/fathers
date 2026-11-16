# Domain

- Glossary: [GLOSSARY.md](../../GLOSSARY.md); the terms the tools share: [fathers glossary](../../../GLOSSARY.md).
- What is built and why: [SPEC.md](../../SPEC.md), its Status table and Decisions.
- Contracts read from the tools, each versioned: capabilities, snapshots and settings ([SPEC.md,
  Decisions](../../SPEC.md#decisions)); the exact shapes in [docs/contracts/](../contracts/) (Slotfather, Lanefather,
  Junkfather, changelog); the event schema in [docs/events.md](../events.md) with `schema/event-v1.schema.json`.
- Compatibility: Godfather and the tools are installed apart. It reads every contract version it lists and shows a
  newer or unknown one as unsupported, never guessed at; a contract changes by a new version, never in place.
  Godfather's own file, `~/.config/godfather/settings.json` (`v: 1`), gains fields and never renames one.
- ADRs go in `docs/adr/` as `0001-slug.md`; none yet.
