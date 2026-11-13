# Domain

- Contexts: one per package, each with its own glossary and `docs/agents/domain.md`
  ([Slotfather](../../slotfather/docs/agents/domain.md), [Lanefather](../../lanefather/docs/agents/domain.md),
  [Junkfather](../../junkfather/docs/agents/domain.md); Godfather: [GLOSSARY.md](../../godfather/GLOSSARY.md) and
  [SPEC.md](../../godfather/SPEC.md)). The terms the tools share: [GLOSSARY.md](../../GLOSSARY.md).
- Contracts between the tools: the shared event schema ([godfather/docs/events.md](../../godfather/docs/events.md),
  validated by [common/eventcheck.py](../../common/eventcheck.py)) and Godfather's capabilities, snapshot and settings
  contracts ([SPEC.md](../../godfather/SPEC.md#decisions)). They change by version, never in place.
- ADRs: each package's `docs/adr/`, numbered `0001-slug.md`.
- History: the packages moved here from their own repositories; [notes/sha-map.md](../../notes/sha-map.md) maps
  their old commits.
