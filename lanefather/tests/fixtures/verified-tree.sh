#!/usr/bin/env bash
# Stamps the tree a full ./verify.sh run passed on, for .githooks/pre-push.
# verify.sh saves the output of `start` before its checks and passes it to
# `finish` after all of them pass. Stamps live in the common Git directory, so
# every worktree of the clone shares them. `guard` refuses to build, install or
# deploy a checkout that is not promoted (#569).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Prints HEAD's tree and whether tracked files match it; untracked files do not count.
snapshot() {
  local tree changes
  tree="$(git -C "$ROOT" rev-parse -q --verify 'HEAD^{tree}' 2>/dev/null)" || tree=none
  changes="$(git -C "$ROOT" --no-optional-locks status --porcelain --untracked-files=no 2>/dev/null)" || changes=unknown
  if [[ -z "$changes" ]]; then echo "$tree clean"; else echo "$tree dirty"; fi
}

# Succeeds when core.hooksPath names this clone's tracked .githooks: the relative
# path install-git-hooks.sh sets, or an absolute path to the .githooks of this
# worktree or of the main worktree, which every worktree of the clone then uses,
# and the release-notes merge driver .gitattributes names is configured.
hooks_installed() {
  local configured resolved main
  configured="$(git -C "$ROOT" config --get core.hooksPath || true)"
  [[ -n "$configured" ]] || return 1
  [[ "$configured" == /* ]] || configured="$ROOT/$configured"
  resolved="$(cd "$configured" 2>/dev/null && pwd -P)" || return 1
  main="$(dirname "$(git -C "$ROOT" rev-parse --path-format=absolute --git-common-dir)")"
  for expected in "$ROOT/.githooks" "$main/.githooks"; do
    if [[ "$resolved" == "$(cd "$expected" 2>/dev/null && pwd -P)" ]]; then
      git -C "$ROOT" config --get merge.release-notes.driver >/dev/null
      return
    fi
  done
  return 1
}

# Succeeds when the pre-push hook would take the commit's tree on dev: stamped, or differing from a
# stamped tree only in documentation Markdown. The hook's own predicate, through a dev push: main adds
# an ancestry rule that a candidate meets only once on dev.
pushable() {
  printf 'candidate %s refs/heads/dev 0000000000000000000000000000000000000000\n' "$1" |
    (cd "$ROOT" && bash .githooks/pre-push)
}

# Refuses, before anything is built, to build, install or deploy this checkout unless it is promoted
# (#569): tracked files match HEAD, no untracked file Git does not ignore (the builds read files by
# path, so one would ship unverified), HEAD's tree is verified as `check` decides, and HEAD is on
# origin/dev. A reason overrides that for a local experiment; it is printed and appended to
# <git common dir>/unpromoted.log, or to STRAYLIGHT_UNPROMOTED_LOG, which tests name.
guard() {
  local command="$1" reason="$2" head="" problem="" changes dev log
  head="$(git -C "$ROOT" rev-parse -q --verify 'HEAD^{commit}' 2>/dev/null)" || head=""
  if [[ -n "$reason" ]]; then
    echo "$command: UNPROMOTED ${head:0:12}${head:+ }build, allowed for: $reason" >&2
    log="${STRAYLIGHT_UNPROMOTED_LOG:-}"
    if [[ -n "$log" ]] || log="$(git -C "$ROOT" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)/unpromoted.log"; then
      printf '%s %s %s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$command" "${head:-unknown}" "$reason" \
        >>"$log" 2>/dev/null || true
    fi
    return 0
  fi
  if [[ -z "$head" ]]; then
    problem="$ROOT is not a Git checkout with a commit"
  elif ! changes="$(git -C "$ROOT" --no-optional-locks status --porcelain --untracked-files=all 2>/dev/null)"; then
    problem="Git could not read the status of $ROOT"
  elif [[ -n "$(grep -v '^??' <<<"$changes")" ]]; then
    problem="uncommitted changes to tracked files:
$(grep -v '^??' <<<"$changes")"
  elif [[ -n "$changes" ]]; then
    # The builds read files by path, not from Git: an untracked release note or desktop/public asset would reach
    # the bundle or the Link package from outside the verified tree. Build output is ignored and does not count.
    problem="untracked files that Git does not ignore:
$(sed 's/^?? //' <<<"$changes")
Commit them through a lane, delete them, or add build output to .gitignore"
  elif ! pushable "$head" >/dev/null 2>&1; then
    problem="./verify.sh has not passed on its tree $(git -C "$ROOT" rev-parse "$head^{tree}"), nor on a tree that
differs from it only in documentation Markdown files. Promote it: tools/lane promote $head"
  elif ! dev="$(git -C "$ROOT" rev-parse -q --verify 'refs/remotes/origin/dev^{commit}' 2>/dev/null)"; then
    problem="origin/dev is unknown here; run git fetch origin"
  elif ! git -C "$ROOT" merge-base --is-ancestor "$head" "$dev"; then
    problem="it is not on origin/dev (${dev:0:12}); run git fetch origin, or promote it: tools/lane promote $head"
  fi
  [[ -n "$problem" ]] || return 0
  printf '%s: refusing to build, install or deploy %s: %s\n' "$command" "${head:-$ROOT}" "$problem" >&2
  printf '%s\n' "For a local experiment, pass --unpromoted \"<reason>\" (or set STRAYLIGHT_UNPROMOTED); it is logged." >&2
  return 1
}

case "${1:-}" in
  check)
    # This checks a committed candidate; it says nothing about worktree edits.
    candidate="$(git -C "$ROOT" rev-parse --verify "${2:-HEAD}^{commit}")"
    pushable "$candidate"
    echo "Full verification already covers candidate $candidate."
    ;;
  guard)
    # guard COMMAND [REASON]: REASON, else STRAYLIGHT_UNPROMOTED, overrides for a local experiment.
    guard "${2:?usage: $0 guard COMMAND [REASON]}" "${3:-${STRAYLIGHT_UNPROMOTED:-}}"
    ;;
  start)
    snapshot
    ;;
  finish)
    read -r start_tree start_state <<<"${2:-}"
    read -r end_tree end_state <<<"$(snapshot)"
    reason=""
    if [[ "${STRAYLIGHT_NATIVE_ONLY:-0}" == "1" ]]; then
      reason="STRAYLIGHT_NATIVE_ONLY=1 skipped the desktop checks"
    elif [[ -z "$start_tree" || "$start_tree" == none ]]; then
      reason="no Git commit was checked out"
    elif [[ "$start_state" != clean ]]; then
      reason="uncommitted changes to tracked files at the start"
    elif [[ "$end_tree" != "$start_tree" ]]; then
      reason="HEAD's tree changed during the run"
    elif [[ "$end_state" != clean ]]; then
      reason="tracked files changed during the run"
    fi
    if [[ -n "$reason" ]]; then
      echo "Verified tree not stamped: $reason."
    else
      common="$(git -C "$ROOT" rev-parse --path-format=absolute --git-common-dir)"
      arguments=("$common" add "$start_tree" "$(git -C "$ROOT" rev-parse HEAD)" "$(date -u +%Y-%m-%dT%H:%M:%SZ)")
      [[ -z "${3:-}" ]] || arguments+=("$3")
      python3 "$ROOT/tools/verified-tree-ledger.py" "${arguments[@]}"
      echo "Verified tree $start_tree stamped for pushes to dev and main."
    fi
    if ! hooks_installed; then
      echo "The pre-push hook or the release-notes merge driver is not installed: run tools/install-git-hooks.sh once in this clone."
    fi
    ;;
  abort|complete)
    common="$(git -C "$ROOT" rev-parse --path-format=absolute --git-common-dir)"
    python3 "$ROOT/tools/verified-tree-ledger.py" "$common" "$1" "${2:-}"
    ;;
  *)
    echo "usage: $0 start | finish STATE [TOKEN] | abort TOKEN | complete TOKEN | check [REV] | guard COMMAND [REASON]" >&2
    exit 2
    ;;
esac
