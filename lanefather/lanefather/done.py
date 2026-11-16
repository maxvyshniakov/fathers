"""Finishing a landed lane: stop what it left running in its worktree (#661), then remove the worktree, its branches,
its build directories, its scratch and the shared cache partitions built from it.

A lane whose agent still locks its worktree is released instead. It no longer counts toward the lane cap or holds
claims; done finishes it once the agent exits.

Auto-done (fathers#157): a lane that becomes finished (landed, its smoke green, its review finished or exempt with
every finding marked) runs done by itself, unless it is kept (`keep ID`, `land --keep`) or `[done] auto` is false.
`done --finished` runs done on every finished lane of the registered projects, and start below the disk floor does
so first (at most once per REPAIR_WINDOW); neither skips a refusal of done. Unattended (auto-done, done --finished,
the repair), done also needs a process source that answers and re-reads `keep` under the lock. Every function acts
on the project of the Store it is given (store.project), never on a module-wide current project.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, cast

from . import config, host, journal, procs, registry, review, state, tickets, worktrees
from .errors import LaneError
from .gitops import dirty, git, git_ok, main_checkout, worktree_lock
from .state import Record

REPAIR_WINDOW = 600.0


def ticket_commented(record: Record, project: config.Config) -> str | None:
    """None when the ticket has a comment since the lane started, else the refusal."""
    argv = [project.env("GH") or "gh", "issue", "view", record["ticket"], "--json", "comments",
            *review.repo_args(record["common_dir"])]
    result = subprocess.run(argv, capture_output=True, text=True)
    if result.returncode:
        return f"gh could not read #{record['ticket']}: {result.stderr.strip()}"
    since = journal.parse_time(record["created"])
    comments: list[dict[str, Any]] = json.loads(result.stdout).get("comments", [])
    # The comment land posts (#25) is not the lane's evidence.
    if any(journal.parse_time(c["createdAt"]) >= since and not c.get("body", "").startswith(tickets.LAND_MARKER)
           for c in comments):
        return None
    return (f"#{record['ticket']} has no comment since the lane started; post the lane's RED->GREEN evidence with "
            f"gh issue comment {record['ticket']}")


def done(args: Any) -> int:
    if args.finished:
        if args.id or args.force_reason:
            raise LaneError("done --finished takes no lane id and no --force-reason: it runs done, with every "
                            "refusal, on each finished lane of the registered projects")
        finish_finished(args.dry_run, skip_auto_off=False)
        return 0
    if args.dry_run:
        raise LaneError("--dry-run goes with --finished: done --finished --dry-run lists the lanes it would finish")
    if not args.id:
        raise LaneError("done needs a lane id, or --finished for every finished lane")
    finish(args.id, args.force_reason)
    return 0


def field(record: Record, name: str) -> dict[str, Any]:
    """One of the record's objects (`landed`, `post_merge`), empty when it has none."""
    value: Any = record.get(name)
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def kept(record: Record) -> bool:
    """Whether auto-done and done --finished leave the lane alone (fathers#157): `keep ID` or `land --keep`. The
    landed lane's flag is the one older installs read."""
    return bool(record.get("keep")) or bool(field(record, "landed").get("keep"))


def keep(args: Any) -> int:
    """`keep ID [--off]`: auto-done, done --finished and start's disk repair skip the lane until --off; done ID
    still finishes it. A landed lane's `landed.keep` is set too, which older installs read; before the land there
    is no `landed` to set (an older promote reads every `landed` as a merge), and land carries the flag."""
    store = state.Store(config.current())
    prog = store.project.command
    with store.locked("state"):
        record = store.load(args.id)
        if record["status"] == "done":
            raise LaneError(f"lane {args.id} is already done")
        record["keep"] = not args.off
        if record.get("landed"):
            record["landed"]["keep"] = not args.off
        store.save(record)
        journal.ledger(f"{'unkeep' if args.off else 'keep'} {args.id}", kind="lane.kept", lane=record,
                       details={"keep": not args.off}, directory=store.directory, project=store.project)
    if args.off:
        print(f"Lane {args.id} no longer kept: once it is finished, done runs by itself "
              f"(or {prog} done --finished)")
    else:
        print(f"Lane {args.id} kept: auto-done and {prog} done --finished leave it; {prog} done {args.id} "
              f"finishes it, {prog} keep {args.id} --off releases it")
    return 0


def unfinished(record: Record, store: state.Store) -> str | None:
    """None when the lane is finished (fathers#157): landed, its smoke green, its review finished or exempt with
    every finding (each panel member's) marked, and not kept; else why not."""
    if record["status"] not in ("landed", "released"):
        return f"it is {record['status']}"
    smoke = field(record, "post_merge").get("status")
    if smoke != "green":
        return f"its post-merge smoke is {smoke or 'not run'}"
    reviewed, words = review.review_state(record["id"], store)
    if not reviewed:
        return words
    return "kept" if kept(record) else None


def refused(lane_id: str, error: Exception, store: state.Store) -> str:
    """Log an unattended done that did not run (lane.done_refused); its reason on one line."""
    text = str(error).replace("\n", " ") or type(error).__name__
    journal.ledger(f"auto-done {lane_id} refused: {text}", kind="lane.done_refused", level="warn", lane=lane_id,
                   details={"error": text}, directory=store.directory, project=store.project)
    return text


def finish_landed(lane_id: str, keep: bool) -> None:
    """When a lane becomes finished (land of a reviewed or exempt lane, its clean review, the mark of its last
    finding): finish it as done does, unless it is kept or `[done] auto` is false. When its agent still locks the
    worktree, keep the worktree and release the lane; done finishes it later. A landed lane does not count toward
    the lane cap; a refused done only leaves cleanup behind."""
    store = state.Store(config.current())
    prog = store.project.command
    record = store.load(lane_id)
    if record["status"] == "done":
        # land and the lane's review both finish a reviewed lane; whichever comes second has nothing left to do.
        return
    if keep or kept(record):
        how = "--keep" if field(record, "landed").get("keep") and not record.get("keep") else f"{prog} keep"
        print(f"Lane {lane_id} kept ({how}): it does not count toward the lane cap; "
              f"finish it with {prog} done {lane_id}")
        return
    if not store.project.done_auto:
        print(f"Lane {lane_id} is finished; auto-done is off ([done] auto = false): "
              f"finish it with {prog} done {lane_id} or {prog} done --finished")
        return
    worktree = record.get("worktree")
    lock = (worktree_lock(main_checkout(record["common_dir"]), worktree)
            if worktree and os.path.isdir(worktree) else None)
    if lock is not None:
        with store.locked("state"):
            record = store.load(lane_id)
            record.update(status="released", released=journal.now())
            store.save(record)
            journal.ledger(f"release {lane_id}: worktree {worktree} is locked ({lock}); "
                           "lane cap space and claims freed", kind="lane.released", lane=record,
                           details={"worktree": worktree, "lock": lock}, directory=store.directory,
                           project=store.project)
        print(f"Lane {lane_id} released: it no longer counts toward the lane cap or holds claims. "
              f"Its agent still holds {worktree} "
              f"(locked: {lock}); once the agent exits, remove it with {prog} done {lane_id}")
        return
    try:
        finish(lane_id, None, store, unattended=True)
    except LaneError as error:
        refused(lane_id, error, store)
        print(f"{prog} done {lane_id} did not run after landing; "
              "the lane stays landed and does not count toward the lane cap.\n"
              f"{error}\nFix that, then run {prog} done {lane_id}")


def removal_paths(record: Record) -> list[str]:
    """What done removes of the lane besides its branches: the worktree, its build directories and its scratch."""
    worktree = record.get("worktree")
    return [p for p in (worktree, *record.get("cargo_targets", []), *record.get("scratch", []))
            if p and os.path.exists(p)]


def finish(lane_id: str, force_reason: str | None, store: state.Store | None = None, *,
           unattended: bool = False) -> int:
    """done ID in the project `store` holds (by default the current one): every refusal, then the removal; the
    bytes it freed. `unattended` (auto-done, done --finished, the disk repair, fathers#157) also refuses a kept lane
    under the lock and a missing process source, and takes a lane already done for done."""
    store = store or state.Store(config.current())
    project = store.project
    prog = project.command
    with store.locked("state"):
        record = store.load(lane_id)
        refusals: list[str] = []
        if record["status"] == "done":
            if unattended:
                return 0
            raise LaneError(f"lane {lane_id} is already done")
        if unattended and kept(record):
            raise LaneError(f"lane {lane_id} is kept ({prog} keep {lane_id} --off releases it)")
        if record["status"] not in ("landed", "released"):
            refusals.append(f"lane {lane_id} is {record['status']}, not landed")
        elif field(record, "post_merge").get("status") == "red":
            refusals.append("its post-merge check is red: fix forward (a follow-up commit on the lane) first")
        elif (state.read_json(store.receipt_path(lane_id, "review")) is not None
              and not review.review_state(lane_id, store)[0]):
            # The fix round of the review runs in this worktree (#570); a lane landed without a review has none.
            refusals.append(f"its {review.review_state(lane_id, store)[1]}: mark each finding with "
                            + review.mark_help(lane_id, prog))
        common = record.get("common_dir")
        if not common or not os.path.isdir(common):
            raise LaneError(f"lane {lane_id} names no repository that exists ({common or 'none'})")
        main = main_checkout(common)
        worktree = record.get("worktree")
        integration = field(record, "landed").get("integration") or record.get("integration")
        if not integration or not os.path.isdir(integration):
            refusals.append(f"its integration worktree {integration or '(none)'} is gone: nothing to check its "
                            "commits against")
            integration = None
        tip = git(main, "rev-parse", "--verify", "--quiet", record["branch"], check=False)
        if (integration and tip and record["status"] in ("landed", "released")
                and not git_ok(integration, "merge-base", "--is-ancestor", tip, "HEAD")):
            refusals.append(f"{record['branch']} has commits that are not in {integration}")
        # A fix lane works in its parent's worktree (review --fix-lane): it keeps the worktree while not done.
        sharing = [r["id"] for r in store.lanes() if r["id"] != lane_id and r.get("status") != "done"
                   and worktree and r.get("worktree") == worktree]
        if sharing:
            refusals.append(f"lane {', '.join(sharing)} works in {worktree} too")
        lock: str | None = None
        leftover: list[Any] = []
        scanner = procs.source(project)
        if unattended and isinstance(scanner, procs.NoSource):
            refusals.append("no process source answers (slotfather procs is missing or too old, and no [hooks] "
                            f"process_scan): unattended done needs one; {prog} done {lane_id} removes it as before")
        if worktree and os.path.isdir(worktree):
            if dirty(worktree):
                refusals.append(f"{worktree} has uncommitted changes")
            head = git(worktree, "rev-parse", "--verify", "--quiet", "HEAD", check=False)
            if integration and head and not git_ok(integration, "merge-base", "--is-ancestor", head, "HEAD"):
                refusals.append(f"{worktree} is at {head[:10]}, with commits that are not in {integration}")
            lock = worktree_lock(main, worktree)
            if lock is not None:
                refusals.append(f"an agent still holds {worktree} (locked: {lock}); run {prog} done {lane_id} "
                                "once it exits")
            # #661: what the process source takes for left behind there stops first; any other process, or no
            # answer, holds the worktree.
            leftover, held = scanner.sort_for_removal(worktree)
            refusals += [f"{reason}; it works in {worktree}" for reason in held]
        # The build directories and scratch go too: whatever works there holds them (fathers#157 review).
        for path in removal_paths(record):
            if path == worktree:
                continue
            stopping, held = scanner.sort_for_removal(path)
            refusals += [f"{reason}; it works in {path}" for reason in held]
            refusals += [f"{process.describe()} works in {path}" for process in stopping]
        if record["ticket"] != "none" and not refusals:
            missing = ticket_commented(record, project)
            if missing:
                refusals.append(missing)
        if refusals and not force_reason:
            raise LaneError("done refused (not landed or not safe to remove):\n- " + "\n- ".join(refusals))
        if refusals:
            journal.ledger(f"override done {lane_id}: {force_reason} ({'; '.join(refusals)})", kind="override",
                           level="warn", lane=record,
                           details={"command": "done", "reason": force_reason, "refusals": refusals},
                           directory=store.directory, project=project)
        freed, removed = 0, list[str]()
        if leftover and worktree:
            # The source stops only what it took for left behind, then reads the worktree again: whatever still works
            # there keeps the worktree, as tools/lane done and the janitor do since #661's review (fail closed).
            left = scanner.stop(leftover, worktree)
            for process in leftover:
                journal.ledger(f"done {lane_id}: {procs.source_name(scanner)} stopped {process.describe()} in "
                               f"{worktree}", kind="process.stopped", lane=record,
                               details={"source": procs.source_name(scanner), "process": process.describe(),
                                        "worktree": worktree}, directory=store.directory, project=project)
                removed.append(f"stopped {process.describe()}")
            if left and not force_reason:
                raise LaneError(f"done refused: {procs.source_name(scanner)} stopped what it took for left behind in "
                                f"{worktree}, but it is still in use:\n- " + "\n- ".join(p.describe() for p in left))
        if worktree and os.path.isdir(worktree):
            freed += worktrees.size(worktree)
            # A second --force removes a locked worktree, which only an override reaches.
            git(main, "worktree", "remove", "--force", *(["--force"] if lock is not None else []), worktree)
            removed.append(worktree)
        git(main, "worktree", "prune")
        harness = f"worktree-{os.path.basename(worktree)}" if worktree else None
        for branch in (record["branch"], harness):
            if not branch or not git_ok(main, "rev-parse", "--verify", "--quiet", "refs/heads/" + branch):
                continue
            if branch == harness and (not integration or not git_ok(
                    main, "merge-base", "--is-ancestor", branch, git(integration, "rev-parse", "HEAD"))):
                continue
            git(main, "branch", "-D", branch)
            removed.append(f"branch {branch}")
        protected = [main, str(Path.home()), str(store.directory.resolve())]
        for path in [*record.get("cargo_targets", []), *record.get("scratch", [])]:
            if os.path.exists(path):
                freed += worktrees.remove_tree(path, protected)
                removed.append(path)
        # The lane's worktree, and the base checkouts a red check classified in, under its scratch (#568).
        checkouts = [path for path in (worktree, *record.get("scratch", [])[:1]) if path]
        for partition in worktrees.shared_partitions(*checkouts, project=project) if checkouts else []:
            with open(str(partition) + ".lock", "a") as handle:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                freed += worktrees.remove_tree(str(partition), protected)
                removed.append(str(partition))
        record.update(status="done", done=journal.now(), freed_bytes=freed)
        store.save(record)
        journal.ledger(f"done {lane_id} freed={freed // 2**20} MiB removed={' '.join(removed) or '-'}",
                       kind="lane.done", lane=record, details={"freed_bytes": freed, "removed": removed},
                       directory=store.directory, project=project)
    print(f"Lane {lane_id} done: claims released, {freed / 1e9:.1f} GB freed")
    for item in removed:
        print(f"  {item}" if item.startswith(("stopped ", "did not stop ")) else f"  removed {item}")
    return freed


def footprint(record: Record) -> int:
    """The bytes done would free at least: the worktree, the lane's build directories and its scratch."""
    return sum(worktrees.size(path) for path in removal_paths(record))


def finish_finished(dry_run: bool, *, skip_auto_off: bool) -> tuple[int, int, list[str]]:
    """Run done, unattended and with every refusal it has, on each finished lane of the registered projects
    (fathers#157); a dry run lists them with their sizes and removes nothing. `skip_auto_off` (start's disk repair)
    leaves the projects whose `[done] auto` is false. One project or lane that fails is reported and the rest go
    on. Prints what it removed and freed; returns the bytes freed, the lanes finished and one line per lane that
    was refused."""
    seen: set[tuple[str, str]] = set()
    freed, finished, failed, listed = 0, 0, list[str](), 0
    if dry_run:
        print("Finished lanes of the registered projects (dry run):")
    for entry, where, unreadable in registry.projects():
        if where is None:
            print(f"skipped {entry.get('project') or entry.get('state_dir')}: {unreadable}")
            continue
        if skip_auto_off and not where.config.done_auto:
            continue
        store = state.Store(where.config, directory=where.directory)
        try:
            records = state.of_project(store.lanes(), where.common)
        except Exception as error:  # one unreadable project never stops the others
            print(f"skipped {where.name}: {error}")
            continue
        for record in records:
            key = (str(where.directory), str(record.get("id")))
            if key in seen or record.get("status") not in ("landed", "released"):
                continue
            seen.add(key)
            try:
                why = unfinished(record, store)
                if why is not None:
                    if dry_run:
                        print(f"  {where.name} {record['id']}: not finished ({why})")
                    continue
                if dry_run:
                    size = footprint(record)
                    freed += size
                    listed += 1
                    print(f"  {where.name} {record['id']}: {size / 1e9:.1f} GB  {record.get('worktree') or '-'}")
                    continue
                print(f"{where.name}: done {record['id']}", flush=True)
                freed += finish(record["id"], None, store, unattended=True)
                finished += 1
            except Exception as error:  # a lane that cannot be finished is refused; the next goes on
                text = refused(str(record.get("id")), error, store)
                failed.append(f"{where.name} {record.get('id')}: {text}")
                print(f"  refused: {text}")
    if dry_run:
        print(f"{listed} finished lane{'s' if listed != 1 else ''}, {freed / 1e9:.1f} GB; dry run: nothing removed")
    else:
        print(f"done --finished: {finished} lane{'s' if finished != 1 else ''} finished, {freed / 1e9:.1f} GB freed"
              + (f"; {len(failed)} refused" if failed else ""))
    return freed, finished, failed


def repair_path() -> Path:
    """The disk repair's marker, beside the project registry it acts on: when it last ran and what it did."""
    return registry.path().with_name("disk-repair.json")


def repair_disk(free_gb: float, floor: host.Floor) -> str:
    """start below the disk floor (fathers#157): done on the machine's finished lanes, at most once per
    REPAIR_WINDOW seconds machine-wide (a start within it reuses the marker's result; one waits for another's
    repair). Never fails start. The words for the refusal should the disk still be short."""
    marker = repair_path()
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        with open(marker.with_name("disk-repair.lock"), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            last = state.read_json(marker) or {}
            if isinstance(last.get("at"), (int, float)) and time.time() - last["at"] < REPAIR_WINDOW:
                words = str(last.get("words") or "")
                print(f"lane: free disk is {free_gb:.0f} GB (10^9), under the floor of {floor.words()}; done ran "
                      f"on the finished lanes {time.time() - last['at']:.0f} s ago: {words}", flush=True)
                return words
            print(f"lane: free disk is {free_gb:.0f} GB (10^9), under the floor of {floor.words()}: running done "
                  "on the registered projects' finished lanes first", flush=True)
            freed, finished, failed = finish_finished(False, skip_auto_off=True)
            words = f"done on finished lanes freed {freed / 1e9:.1f} GB (10^9) on {finished}"
            words += f", {len(failed)} refused ({'; '.join(failed[:3])})" if failed else ""
            state.atomic_write(marker, json.dumps({"v": 1, "at": time.time(), "freed_bytes": freed,
                                                   "finished": finished, "refused": failed, "words": words}) + "\n")
            return words
    except Exception as error:  # the repair only helps start; it never fails it
        print(f"lane: warning: the disk repair failed: {error}", file=sys.stderr)
        return f"the disk repair failed ({error})"
