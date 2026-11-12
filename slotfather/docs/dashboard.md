# The dashboard

The dashboard is part of [Godfather](https://github.com/maxvyshniakov/godfather) now (slotfather#28). Godfather
draws the same screen from `slotfather status --json --full` and changes settings through `slotfather settings`;
see [the dashboard contracts](api.md#dashboard-contracts).

`slotfather dashboard [ARGS...]`, and `slotfather` alone in a terminal, run `godfather ARGS...` in its place, so
its arguments (`--once`, `--width N`, `--interval S`, ...) and exit code are Godfather's. Without `godfather` on
`PATH` they print where to get it and exit 2. Godfather's own help (`?`) explains every panel, key and term.

The guide to the in-tree dashboard, before the move, is in this file at
[37c4df7](https://github.com/maxvyshniakov/slotfather/blob/37c4df7/docs/dashboard.md).
