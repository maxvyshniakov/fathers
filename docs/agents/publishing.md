# Publishing

## Branches

- Two branches on `maxvyshniakov/fathers`:
  - `dev` is the everyday integration branch, checked out in the main checkout and used for local installs. The
    orchestrator may push verified batches to `origin/dev` without waiting for an explicit push word.
  - `main` is the stable/release destination. Only an explicit push request in the conversation publishes to
    `origin/main`; finishing, landing, or pushing to `dev` does not imply a push to `main`.
- Worktrees: Lanefather's lanes in `.claude/worktrees/lane-<id>` of the main checkout (`lanefather start` creates,
  `lanefather done` and `lanefather gc` remove them); integration worktrees on `integrate/<batch>`, removed with
  `git worktree remove` once their batch is on `dev`. Older hand-made lanes are `../fathers-lane-<slug>`, removed
  the same way.

## Gate

- `.githooks/pre-push` ([tools/prepush.py](../../tools/prepush.py)) runs each pushed commit's own
  `tools/checks.py verify` in a clean temporary worktree: the checks of the packages that commit changes since its
  merge-base with `origin/main`, and the root checks ([checks](checks.md)). A check that passed on a tree is recorded
  in `.git/fathers-verified` and not run again, so a push whose checks all passed takes no lease. Do not run the gate
  by hand right before a push.
- Once per clone, activate the hook: `git config core.hooksPath .githooks`. Git runs the pushing checkout's own hook:
  push a commit from a checkout that has this hook.

## Push

- `git fetch origin`, read the outgoing range (`git log --oneline origin/main..<sha>`), then
  `git push origin <sha>:refs/heads/main`. The SHA must contain `origin/main`. Never force, never `--no-verify`.
- Afterwards fast-forward a clean main checkout: `git -C <main checkout> merge --ff-only origin/main`.
- Then delete the merged topic branch, after removing its worktree if it has one:
  `git worktree remove <path>` and `git branch -d <branch>`. `-d` refuses a branch `main` does not contain; never
  `-D` it.

## CI

- One workflow per package, `.github/workflows/<package>.yml`, and `common.yml` for `common/` and `install.py`, each
  triggered by its own paths on a push to `main`, on pull requests and by hand. Each calls
  [package.yml](../../.github/workflows/package.yml) with its name and Python floor and differs from the others only
  in those and its paths (`tools/test_checks.py`): the floor on Ubuntu 24.04 and the newest Python on
  ubuntu-latest (26.04, uutils coreutils). Only the newest runs lint. The steps are the named checks
  of `tools/checks.py` (`<package> lint`, `<package> tests`) with the tools of `<package>/requirements-dev.txt`.
- A new Python floor changes the package's `install.py` `MIN_PYTHON`, its `requires-python` and its workflow's
  `floor` together; a new Python release changes `newest` in package.yml.
- The runs of a SHA: `gh run list -R maxvyshniakov/fathers --commit <sha>`; follow one with
  `gh run watch <id> -R maxvyshniakov/fathers --exit-status`. A push is finished when every triggered workflow is
  green on that SHA.
- A failure in checkout, setup or a download is infrastructure: rerun it once (`gh run rerun <id> --failed`). A
  failing check or test is fixed in a new commit.

## Release

- Only on an explicit release request, per package: move `## Unreleased` under `## x.y.z - YYYY-MM-DD` in its
  `CHANGELOG.md` and set the version in `pyproject.toml` and in its version source (Slotfather and Godfather
  `src/<package>/version.py`, Spawnfather `spawnfather/__init__.py`, Lanefather `lanefather/__init__.py`). The package's
  `tests/test_changelog.py` checks both. A release is that commit on `main`; nothing goes to a registry and there are
  no tags.
- Format and version rules: each package's publishing page or README (linked from its `AGENTS.md`).

## Install, deploy, restart

- A push installs nothing. `python3 install.py <package>` from a clean checkout of `dev` (or `main`) installs one
  package; its README has the options (Spawnfather `--launchd --mastack`). Check it with
  the command's version (`slotfather version`, `lanefather --version`, `spawnfather version`,
  `godfather --version`). Running commands keep the release they started from.

## Report

- The pushed SHA, each triggered workflow's result on it, and installs still pending.
