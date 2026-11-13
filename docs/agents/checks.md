# Checks

The installed commands use only Python's standard library (3.9+; Lanefather 3.11+). Ruff, Pyright, Bun and Repo-Kit
are development tools only.

## Focused

- One test file, from the package's directory: `python3 -m unittest tests/test_<area>.py` (Godfather:
  `python3 -m unittest discover -s tests -t tests -p 'test_<area>.py'`).
- One package's lint gate, from its directory: `python3 tools/check.py` (about 10 s).
- The root tools: `python3 tools/checks.py run tools`. Any check by name: `python3 tools/checks.py run "<name>"`.

## Full gate

- `bun run verify` (Repo-Kit, [repo-kit.config.mjs](../../repo-kit.config.mjs)) runs the checks the changes since
  `origin/main` select ([tools/checks.py](../../tools/checks.py)): `<package> lint` and `<package> tests` for each
  changed package (every package for `common/`; Slotfather's sources also select `godfather tests`), `common` for
  `common/` and `install.py`, and always `tools`, `kit` (`repo-kit check`) and `test hygiene`. A path no rule knows
  selects everything, and so does a tree without changes. `FATHERS_CHECKS=all bun run verify` runs every check;
  `FATHERS_CHECK_BASE=<ref>` names another base.
- It leases two slots itself and runs the steps side by side. Every check took 2026-11-13: lint gates 2 to 14 s,
  Godfather's tests 1.5 min, Junkfather's 2, Slotfather's 4, Lanefather's 13 (the whole run: 13 min).
- The pre-push hook runs it on the pushed commits and a lane's `lanefather check` on the lane's diff
  ([orchestration](orchestration.md)); do not run it again by hand right before either.
- CI runs each package's lint gate and tests on its own workflow ([publishing](publishing.md#ci)).

## Lint gate

- Every package carries the same `tools/check.py`: Ruff with the shared rules of [tools/ruff.toml](../../tools/ruff.toml)
  and Pyright with the package's own strictness, against `tools/type-baseline.json`. A diagnostic beyond the budget
  of its exact file, rule and message fails; a package without a baseline (Junkfather) allows none. The budget only
  shrinks: `python3 tools/check.py --shrink-baseline` after fixing debt; never grow it or weaken a rule to pass.
- A package's `[tool.ruff.lint] ignore` lists the shared rules it does not pass yet; drop a code with the fix.
- `tools/test_checks.py` (in the `tools` check) holds the four `check.py` copies identical and their pins equal:
  change them together.

## Heavy checks

- Whole test suites, `bun run verify` and anything else that takes more than a few seconds run under a slot:
  `slotfather run -n 1 --priority interactive --ticket <N> --label "<ticket> <what it does>" -- <command>`
  (`normal` is the default priority; the label is required). Repo-Kit's verify and pre-push take their own lease.
- Test hygiene: `repo-kit test-hygiene` keeps fixed sleeps and timing assertions in tests from growing
  (`test-hygiene-baseline.json`, shrink-only: `bun run repo-kit test-hygiene --update` after removing some).

## Browser and visual

- No browser checks. Godfather's screens are text goldens (`godfather/tests/goldens/`) and pictures from
  `godfather/tools/raster.py` ([Godfather's README](../../godfather/README.md#develop)).

## Pins

- Ruff 0.14.3 and Pyright 1.1.407 in each package's `requirements-dev.txt`: Slotfather's and Godfather's baselines
  key on Pyright's messages, which another version rewrites. Move all four together, with the baselines, when a
  typing ticket retires those budgets.
- Bun 1.4.4 and Repo-Kit 0.7.0, exact (`package.json`, `bun.lock`): each commit is verified by the kit it pins.
- uv 0.13.5 in the Lanefather and Junkfather workflows, which run the pinned tools through uv.

## Review

- Reviewer: the `review` role of Mastack's `models.toml`, of another family than the implementer.
- Project rules on top of the coding standards: installed code imports only the standard library and runs on the
  package's Python floor; state, event and snapshot formats stay readable by older installs (each package's
  `docs/agents/domain.md`); a change a user would notice adds an entry under `## Unreleased` in that package's
  `CHANGELOG.md` in the same commit; every package's own Review section applies to its code.
- Ignore: frozen fixtures (`*/tests/fixtures/`), baselines that only shrank.
