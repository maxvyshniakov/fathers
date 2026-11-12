# Godfather glossary

Godfather's own terms. The contract vocabulary it shares with the tools (source, snapshot, event, event kind, subject,
actor, capabilities, action, host, owner, reap, orphan) is in the [fathers glossary](../GLOSSARY.md).

| Term | Meaning |
| --- | --- |
| Source state | Whether a source can be shown: absent, unsupported, ok or stale. |
| Discovery | Finding the tools on PATH and asking each for its capabilities. |
| Stale snapshot | The last good snapshot, kept on screen after a later one failed. |
| Source tag | The four-cell label of a source in the merged feed (`slot`, `lane`, `junk`). |
| Join | A read-only link the dashboard computes between a request, a lane and an entry. |
| Border summary | A tool's one-line count set into the SLOTS border. |
| Page | A full-screen view of one tool (Overview, Lanes, Processes). |
| Effective value | The value of a setting in force after the environment tightens it. |
| Settings revision | An opaque version of a group of settings, used to refuse a stale write. |
| Adapter module | Godfather's internal per-tool module that turns a snapshot into the render model. Not an adapter in the shared sense. |
