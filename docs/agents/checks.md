# Checks

The installed commands use only Python's standard library (3.11+: each package's
`requires-python`), and so do the repository's checks in `tools/`. Ruff and Pyright are development tools only.

## Focused

- One test file, from the package's directory: `python3 -m unittest tests/test_<area>.py` (Godfather:
  `python3 -m unittest discover -s tests -t tests -p 'test_<area>.py'`).
- One package's lint gate, from its directory: `python3 tools/check.py` (about 10 s). After a small edit run
  `uvx ruff@0.14.3 check <file>` there first (under 1 s, with the package's rules): an unused import or a long line
  fails in a second, not 10 s into `python3 tools/checks.py run "<package> lint"`.
- The root tools: `python3 tools/checks.py run tools`. Any check by name: `python3 tools/checks.py run "<name>"`.
- `common/`'s tests (the CLI convention's helper and the root entry points), from the root:
  `python3 -m unittest discover -s common/tests`.

## Full gate

- `python3 tools/checks.py verify` runs the checks the changes since `origin/main` select
  ([tools/checks.py](../../tools/checks.py)): `<package> lint` and `<package> tests` for each changed package (every
  package for `common/`; Slotfather's sources also select `godfather tests`), `common lint` and `common tests` for
  `common/` and `install.py`, and always `tools` and `test hygiene`. A path no rule knows selects everything, and so
  does a tree without changes. `FATHERS_CHECKS=all python3 tools/checks.py verify` runs every check;
  `FATHERS_CHECK_BASE=<ref>` names another base; `--keep-going` lets the others finish after a failure.
- It leases two slots itself (`fathers verify`) and runs the checks side by side
  ([tools/verify.py](../../tools/verify.py)). Every check took 2026-11-13: lint gates 2 to 14 s, Godfather's tests
  1.5 min, Spawnfather's 2, Slotfather's 4-5, Lanefather's 13-18 (the whole run: as long as Lanefather's).
- A check that passed on a clean tree is recorded in `.git/fathers-verified` (per tree, Python and platform) and
  skipped the next time, in any worktree and by the pre-push hook; `--fresh` runs it again.
- The pre-push hook runs it on the pushed commits and a lane's `lanefather check` on the lane's diff
  ([orchestration](orchestration.md)); do not run it again by hand right before either.
- CI runs the same named checks (`<package> lint`, `<package> tests`) through one reusable workflow, on each
  package's Python floor and the newest Python on Ubuntu ([publishing](publishing.md#ci)). On macOS run
  `FATHERS_CHECKS=all python3 tools/checks.py verify` locally; this includes the PTY and detached-process tests on
  the local Python.

## Lint gate

- Every package carries the same `tools/check.py`: Ruff with the shared rules of [tools/ruff.toml](../../tools/ruff.toml)
  and Pyright with the package's own strictness, against `tools/type-baseline.json`. A diagnostic beyond the budget
  of its exact file, rule and message fails; a package without a baseline (Spawnfather) allows none. The budget only
  shrinks: `python3 tools/check.py --shrink-baseline` after fixing debt; never grow it or weaken a rule to pass.
- A package's `[tool.ruff.lint] ignore` lists the shared rules it does not pass yet; drop a code with the fix.
- `tools/test_checks.py` (in the `tools` check) holds the four `check.py` copies identical and their pins equal:
  change them together.

## Heavy checks

- Whole test suites and anything else that takes more than a few seconds run under a slot:
  `slotfather run -n 1 --priority interactive --ticket <N> --label "<ticket> <what it does>" -- <command>`
  (`normal` is the default priority; the label is required). `checks.py verify`, the pre-push hook and lane checks
  take their own lease.
- Test hygiene ([tools/hygiene.py](../../tools/hygiene.py), the `test hygiene` check): no fixed sleep of 0.2 s or more
  and no raw timing assertion in a test file, unless the line or the comment above it gives `sleep-ok: <reason>` or
  `load-tolerant: <reason>`; a budget belongs in the perf suite (`perf/`, `test_perf_*.py`). In any Python file a
  `git` call with `--name-only` or `--name-status` passes `-c core.quotePath=false` or `-z` (`git-quoted-paths`).

## Browser and visual

- No browser checks. Godfather's screens are text goldens (`godfather/tests/goldens/`) and pictures from
  `godfather/tools/raster.py` ([Godfather's README](../../godfather/README.md#develop)).

## Pins

- Ruff 0.14.3 and Pyright 1.1.407 in each package's `requirements-dev.txt`: Slotfather's and Godfather's baselines
  key on Pyright's messages, which another version rewrites. Move all four together, with the baselines, when a
  typing ticket retires those budgets.
- uv 0.13.5 in the Lanefather and Spawnfather workflows, which run the pinned tools through uv.

## Review

- Reviewer: the `review` role of Mastack's `models.toml`, of another family than the implementer.
- Project rules on top of the coding standards: installed code imports only the standard library and runs on the
  package's Python floor; state, event and snapshot formats stay readable by older installs (each package's
  `docs/agents/domain.md`); a change a user would notice adds an entry under `## Unreleased` in that package's
  `CHANGELOG.md` in the same commit; every package's own Review section applies to its code.
- Ignore: frozen fixtures (`*/tests/fixtures/`), baselines that only shrank.
