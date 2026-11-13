# Publishing

## Branches

- One branch, `main` on `maxvyshniakov/fathers`: the everyday destination, the release destination and the base for
  cleanup. There is no `dev`.
- Only an explicit push request in the conversation publishes; finishing, landing or releasing does not imply one.
- Worktrees: Lanefather's lanes in `.claude/worktrees/lane-<id>` of the main checkout (`lanefather start` creates,
  `lanefather done` and `lanefather gc` remove them); the orchestrator's integration worktree `../fathers-integrate`
  on `integrate/<batch>`, removed with `git worktree remove` once its batch is on `main`. Older hand-made lanes are
  `../fathers-lane-<slug>`, removed the same way.

## Gate

- `.githooks/pre-push` (Repo-Kit) runs `bun run verify --release` on each pushed commit in a clean temporary
  worktree: the checks of the packages that commit changes since `origin/main`, and the root checks
  ([checks](checks.md)). A commit that passed once is recorded in `.git/repo-kit-verified` and not run again. Do not
  run the gate by hand right before a push.
- `bun install` in the main checkout activates the hooks (`core.hooksPath = .githooks`) and installs the kit the hook
  and every lane check use. It needs `GITHUB_TOKEN` with `read:packages`, or `gh` logged in.

## Push

- `git fetch origin`, read the outgoing range (`git log --oneline origin/main..<sha>`), then
  `git push origin <sha>:refs/heads/main`. The SHA must contain `origin/main`. Never force, never `--no-verify`.
- Afterwards fast-forward a clean main checkout: `git -C <main checkout> merge --ff-only origin/main`.

## CI

- One workflow per package, `.github/workflows/<package>.yml`, and `common.yml` for `common/` and `install.py`, each
  triggered by its own paths on a push to `main`, on pull requests and by hand; Ubuntu and macOS.
- The runs of a SHA: `gh run list -R maxvyshniakov/fathers --commit <sha>`; follow one with
  `gh run watch <id> -R maxvyshniakov/fathers --exit-status`. A push is finished when every triggered workflow is
  green on that SHA.
- A failure in checkout, setup or a download is infrastructure: rerun it once (`gh run rerun <id> --failed`). A
  failing check or test is fixed in a new commit.

## Release

- Only on an explicit release request, per package: move `## Unreleased` under `## x.y.z - YYYY-MM-DD` in its
  `CHANGELOG.md` and set the version in `pyproject.toml` and in its version source (Slotfather and Godfather
  `src/<package>/version.py`, Junkfather `junkfather/__init__.py`, Lanefather `lanefather/__init__.py`). The package's
  `tests/test_changelog.py` checks both. A release is that commit on `main`; nothing goes to a registry and there are
  no tags.
- Format and version rules: each package's publishing page or README (linked from its `AGENTS.md`).

## Install, deploy, restart

- A push installs nothing. `python3 install.py <package>` from a clean checkout of the pushed `main` installs one
  package, only on an explicit request; its README has the options (Junkfather `--launchd --mastack`). Check it with
  the command's version (`slotfather version`, `lanefather --version`, `junkfather version`,
  `godfather --version`). Running commands keep the release they started from.

## Report

- The pushed SHA, each triggered workflow's result on it, and installs still pending.
