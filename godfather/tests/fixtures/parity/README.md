# Parity fixture (frozen)

What Slotfather's in-tree dashboard drew for its own demo scene, read by `tests/test_parity.py`. That dashboard no
longer exists upstream (slotfather#28, 81abd86 deleted it), so this fixture is frozen: never regenerate it from a
newer Slotfather. A missing or unreadable file fails the parity test; it never skips.

- `cells.json.gz`, `frames.json.gz`: every cell and every rendered frame, gzipped
- `help.json`: Slotfather's help sections
- `scene.json`: the moment, the chart window and the lanes the scene froze
- `slotfather/replies.json`: the same model as `capabilities --json` and `status --json --full` replies, read by
  Godfather in fixture mode (`GODFATHER_FIXTURES` points at this directory). The changelog path and host in it are
  those of the machine that produced it and mean nothing here.

Produced on 2026-11-12 from Slotfather 37c4df7, the last commit with the dashboard, by Godfather 651e709's
`tests/parity_reference.py`:

```sh
mkdir DIR && git -C ~/slotfather archive 37c4df7 | tar -x -C DIR
SLOTFATHER_SRC=DIR/src python3 tests/parity_reference.py OUT
gzip -9 -n OUT/cells.json OUT/frames.json
cp -R OUT/. tests/fixtures/parity/
```
