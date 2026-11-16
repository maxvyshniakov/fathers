# Godfather glossary

Godfather's own terms, each one built (SPEC.md, Status); a term for something not built yet is marked planned with
its ticket. The contract vocabulary it shares with the tools (source, snapshot, event, event kind, subject,
actor, capabilities, action, host, owner, reap, orphan) is in the [fathers glossary](../GLOSSARY.md).

| Term | Meaning |
| --- | --- |
| Source state | Whether a source can be shown: absent, unsupported, ok or stale. |
| Discovery | Finding the tools on PATH and asking each for its capabilities. |
| Stale snapshot | The last good snapshot, kept on screen after a later one failed. |
| Source tag | A tool's short name from its capabilities (`slot`, `lane`, `junk`); EVENTS' ALL log shows it as the row's type. |
| Join | A read-only link the dashboard computes between a request, a lane and an entry. |
| Border summary | A tool's one-line count set into the SLOTS border. |
| Page | A full-screen view of one tool (Overview, Lanes, Processes), one key away (`1 2 3`). |
| Log switch | EVENTS' title (`❮ SLOTS LOG ❯`), which picks the one tool's log it shows. |
| Run row | One row per Slotfather run in the SLOTS log, from its grant to its release. |
| Run mark | A trend or outlier set on a run row (▲ ↗ ↘), never a row of its own. |
| Row action | A tool's command offered for the selected row, re-checked against a fresh snapshot before it runs. |
| Action menu | The overlay listing a row's actions (Enter, `a` or a right-click). |
| Synchronized update | One frame written between `CSI ? 2026 h` and `l`, so the terminal paints it whole. |
| Effective value | The value of a setting in force after the environment tightens it. |
| Settings revision | An opaque version of a group of settings, used to refuse a stale write. |
| Adapter module | Godfather's internal per-tool module that turns a snapshot into the render model. Not an adapter in the shared sense. |
