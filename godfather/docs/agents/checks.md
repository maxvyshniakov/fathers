# Checks

The installed command uses only Python's standard library (3.11+). Ruff, Pyright and Pillow (pictures only) are
development tools, pinned in `requirements-dev.txt`.

## Gate

The repository's [checks](../../../docs/agents/checks.md) run Godfather's lint gate and tests whenever a change touches
`godfather/` or Slotfather's sources (`python3 tools/checks.py verify`, the pre-push hook, a lane check). By hand, from `godfather/`:

```sh
python3 -m unittest discover -s tests -t tests -p 'test_<area>.py'     # one file, while working
python3 tools/check.py                                                 # Ruff and Pyright, about 10 s
slotfather run -n 1 --priority batch --label "<task> godfather tests" -- python3 -m unittest discover -s tests -t tests
```

`tools/check.py` holds Pyright to `tools/type-baseline.json`, which only shrinks (`--shrink-baseline` after fixing
debt). `tests/test_imports.py` pins the layers (SPEC.md, Package and modules) and `tests/test_spec.py` the SPEC's
module table and Status.

## Goldens and pictures

- `tests/goldens/` holds every `--once` screen as text and a curated palette-representative subset as colour escapes
  in Seti and Pro Light. After a deliberate visible change, `python3 tools/goldens.py` rewrites them: read every diff
  it makes before committing it.
- `python3 tools/raster.py --goldens OUTDIR` draws the colour goldens as PNGs; `tools/compare_media.py` diffs two
  pictures.

## Live tests

The `test_live*.py` files run `godfather` in a PTY against fake tools (`tests/terminal.py`), the PTY its controlling
terminal so a resize arrives as SIGWINCH. They wait for what the screen shows, never for a fixed time; how frames are
written is tested on the recorded byte stream with the test's own clock (`tests/test_frames.py`). Idle CPU against
another build: `tools/measure_cpu.py`, under `slotfather run -n all`.

## Review

- Godfather never reads a tool's state files or imports its code: only its CLI, through `sources`.
- Selecting, scrolling, paging and overlays never run a mutating command; an action is re-checked against a fresh
  snapshot of its source and runs with no shell, a timeout and `GODFATHER_ACTOR`.
- A source that is absent, unsupported or failing hides or dims its panels and never ends the view.
- Text from a tool reaches the terminal without control characters (`text.dashboard_text`, and the event validator
  for logs).
