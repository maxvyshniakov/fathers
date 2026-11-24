"""The lane lifecycle: one transition table, status predicates, and `lane.*` transition events (fathers#69, #55, #227).

Statuses: planned (registered), active (has a worktree), landed (merged), released (merged with a green smoke while
its agent still locks the worktree), done (cleaned up). Every status, `keep`, and `post_merge` mutation and its
ledger/event entry goes through `register`, `set_keep`, or `transition` under the Store's `state` lock, taking an
explicit `state.Store` rather than `config.current()`.
"""
from __future__ import annotations

import contextlib
from typing import Any, cast

from . import journal, state
from .errors import LaneError
from .state import Record

STATUSES: tuple[str, ...] = ("planned", "active", "landed", "released", "done")
KINDS: tuple[str, ...] = ("lane", "fix")
OUTCOMES: tuple[str, ...] = ("landed", "parked", "abandoned")
OCCUPYING: tuple[str, ...] = state.OCCUPYING
LANDED: tuple[str, ...] = state.LANDED
AWAITING_DONE: tuple[str, ...] = ("landed", "released")
LIVE: tuple[str, ...] = ("planned", "active", "landed", "released")

TRANSITIONS: dict[tuple[str, str], str] = {
    ("planned", "adopt"): "active",
    ("active", "land"): "landed",
    ("landed", "release"): "released",
    ("landed", "finish"): "done",
    ("released", "finish"): "done",
    ("planned", "drop"): "done",
    ("active", "drop"): "done",
    ("planned", "park"): "done",
    ("active", "park"): "done",
    ("planned", "abandon"): "done",
    ("active", "abandon"): "done",
    ("landed", "hand_over"): "landed",
    ("released", "hand_over"): "released",
    ("landed", "record_smoke"): "landed",
    ("released", "record_smoke"): "released",
    ("landed", "record_recheck"): "landed",
    ("released", "record_recheck"): "released",
}

# Repeating an event when the lane is already in its target status is a no-op.
IDEMPOTENT: dict[str, str] = {
    "adopt": "active",
    "land": "landed",
    "release": "released",
    "finish": "done",
}

EVENT_KINDS: dict[str, str] = {
    "adopt": "lane.adopted",
    "land": "lane.landed",
    "release": "lane.released",
    "finish": "lane.done",
    "drop": "lane.dropped",
    "park": "lane.parked",
    "abandon": "lane.abandoned",
}

ALL_EVENTS: tuple[str, ...] = (
    "adopt",
    "land",
    "release",
    "finish",
    "drop",
    "park",
    "abandon",
    "hand_over",
    "record_smoke",
    "record_recheck",
)


def planned(record: Record | None) -> bool:
    """Whether `record` is registered without an adopted worktree."""
    return bool(record) and record.get("status") == "planned"


def active(record: Record | None) -> bool:
    """Whether `record` has an adopted worktree and has not landed yet."""
    return bool(record) and record.get("status") == "active"


def occupying(record: Record | None) -> bool:
    """Whether `record` counts toward the lane cap and holds its claims (`planned` or `active`)."""
    return bool(record) and record.get("status") in OCCUPYING


def outcome_of(record: Record | None) -> str | None:
    """The non-empty string `outcome` of `record` (`landed`, `parked`, `abandoned`), or `None`."""
    if not isinstance(record, dict):
        return None
    raw = record.get("outcome")
    return raw if isinstance(raw, str) and raw else None


def kind_of(record: Record | None) -> str:
    """The lane kind (`"lane"` or `"fix"`), defaulting legacy records via `review_fix_of` or `"lane"`."""
    if not isinstance(record, dict):
        return "lane"
    raw = record.get("kind")
    if isinstance(raw, str) and raw in KINDS:
        return raw
    return "fix" if state.fix_parents(record) else "lane"


def is_fix_lane(record: Record | None) -> bool:
    """Whether `record` is a fix lane (`kind == "fix"`, or a legacy record with `review_fix_of`)."""
    return bool(record) and kind_of(record) == "fix"


def landed(record: Record | None) -> bool:
    """Whether `record` actually landed (`landed`, `released`, or `done` with `landed` outcome/metadata).

    A `done` lane that was parked, abandoned, dropped, or forced done without landing is not landed.
    """
    if not record:
        return False
    status = record.get("status")
    if status in AWAITING_DONE:
        return True
    if status == "done":
        outcome = outcome_of(record)
        if outcome is not None:
            return outcome == "landed"
        return isinstance(record.get("landed"), dict) and bool(record["landed"])
    return False


def dropped(record: Record | None) -> bool:
    """Whether `record` is done because it was dropped (added nothing to its base)."""
    if not record or not finished(record):
        return False
    raw: Any = record.get("dropped")
    return isinstance(raw, dict) and bool(cast(dict[str, Any], raw))


def after_satisfied(record: Record | None) -> bool:
    """Whether `record` satisfies an `--after` dependency (`landed`, or `dropped` since it added nothing)."""
    return landed(record) or dropped(record)


def parked(record: Record | None) -> bool:
    """Whether `record` is done with outcome `parked`."""
    return finished(record) and outcome_of(record) == "parked"


def abandoned(record: Record | None) -> bool:
    """Whether `record` is done with outcome `abandoned`."""
    return finished(record) and outcome_of(record) == "abandoned"


def display_state(record: Record | None, *, missing: str = "not started") -> str:
    """Human-readable lane state: `parked`/`abandoned` for those done outcomes, `dropped` when dropped, else status."""
    if not record:
        return missing
    if finished(record):
        outcome = outcome_of(record)
        if outcome in ("parked", "abandoned"):
            return outcome
        if isinstance(record.get("dropped"), dict) and record["dropped"]:
            return "dropped"
    return str(record.get("status") or missing)


def released(record: Record | None) -> bool:
    """Whether `record` is landed and released while its agent still locks the worktree."""
    return bool(record) and record.get("status") == "released"


def awaiting_done(record: Record | None) -> bool:
    """Whether `record` is landed or released and has not been cleaned up yet."""
    return bool(record) and record.get("status") in AWAITING_DONE


def finished(record: Record | None) -> bool:
    """Whether `record` has completed cleanup (`done`)."""
    return bool(record) and record.get("status") == "done"


def live(record: Record | None) -> bool:
    """Whether `record` is in any non-done status (`planned`, `active`, `landed`, or `released`)."""
    return bool(record) and record.get("status") in LIVE


def kept(record: Record) -> bool:
    """Whether auto-done and done --finished leave the lane alone (fathers#157): `keep ID` or `land --keep`."""
    landed_info: Any = record.get("landed")
    return bool(record.get("keep")) or (
        isinstance(landed_info, dict) and bool(cast(dict[str, Any], landed_info).get("keep"))
    )


def set_keep(store: state.Store, lane_id: str, keep: bool) -> Record:
    """Set or clear `keep` on `lane_id` under `store`'s `state` lock and emit `lane.kept`."""
    flag = bool(keep)

    def _apply(record: Record) -> bool:
        if finished(record):
            raise LaneError(f"lane {lane_id} is already done")
        record["keep"] = flag
        if isinstance(record.get("landed"), dict):
            record["landed"]["keep"] = flag
        return True

    with store.locked("state"):
        record = store.update(lane_id, _apply)
        journal.ledger(
            f"{'keep' if flag else 'unkeep'} {lane_id}",
            kind="lane.kept",
            lane=record,
            details={"keep": flag},
            directory=store.directory,
            project=store.project,
        )
        return record


def register(store: state.Store, record: Record, *, status: str = "planned", worktree: str | None = None,
             agent: dict[str, Any] | None = None, now: str | None = None,
             ledger_message: str | None = None, ledger_details: dict[str, Any] | None = None) -> Record:
    """Persist a newly registered lane record under `store`'s state lock and emit `lane.started`."""
    if status not in OCCUPYING:
        raise LaneError(f"cannot register lane {record.get('id')} in status {status!r}")
    effective_worktree = worktree if worktree is not None else record.get("worktree")
    if status == "planned" and effective_worktree:
        raise LaneError(f"cannot register planned lane {record.get('id')} with a worktree")
    with store.locked("state"):
        record["status"] = status
        record.setdefault("kind", kind_of(record))
        if worktree is not None:
            record["worktree"] = worktree
        if agent is not None:
            state.record_agent(record, "start", agent, now=now or journal.now())
        store.save(record)
        details = dict(ledger_details or {})
        details.setdefault("status", record["status"])
        details.setdefault("kind", record["kind"])
        message = ledger_message or f"start {record['id']}"
        journal.ledger(message, kind="lane.started", lane=record, details=details,
                       directory=store.directory, project=store.project)
        return record


def hand_over(
    store: state.Store,
    parent_id: str,
    fix_record: Record,
    *,
    agent: dict[str, Any] | None = None,
    now: str | None = None,
    ledger_message: str | None = None,
    ledger_details: dict[str, Any] | None = None,
) -> tuple[Record, Record]:
    """Atomically hand over `parent_id`'s worktree to `fix_record` under `store`'s `state` lock (fathers#45).

    Validates `parent_id` can perform `hand_over` and holds a worktree, clears `parent["worktree"]` while recording
    `handed_over`, and registers `fix_record` as `active` (`kind="fix"`) holding that worktree. Writes the parent
    first and rolls it back if writing the fix record fails so an interrupted hand-over never leaves both lanes
    owning the worktree.
    """
    stamp = now or journal.now()
    fix_id = str(fix_record["id"])
    with store.locked("state"):
        original_parent = store.load(parent_id)
        current = str(original_parent.get("status", ""))
        if (current, "hand_over") not in TRANSITIONS:
            raise LaneError(f"lane {parent_id}: cannot hand_over from status {current!r}")
        worktree = original_parent.get("worktree")
        if not isinstance(worktree, str) or not worktree:
            raise LaneError(f"hand-over refused: lane {parent_id} ({current}) has no worktree to hand over")
        target = TRANSITIONS[(current, "hand_over")]

        updated_parent: Record = dict(original_parent)
        updated_parent["status"] = target
        updated_parent["worktree"] = None
        updated_parent["handed_over"] = {"to": fix_id, "worktree": worktree, "at": stamp}

        fix_record["status"] = "active"
        fix_record["kind"] = "fix"
        fix_record["worktree"] = worktree
        if agent is not None:
            state.record_agent(fix_record, "start", agent, now=stamp)

        # Clear parent's worktree first so a hard crash between writes never leaves two owners;
        # on write failure saving the fix record, restore the original parent record.
        store.save(updated_parent)
        try:
            store.save(fix_record)
        except BaseException:
            with contextlib.suppress(BaseException):
                store.save(original_parent)
            raise

        details = dict(ledger_details or {})
        details.setdefault("status", fix_record["status"])
        details.setdefault("kind", fix_record["kind"])
        details.setdefault("worktree", worktree)
        message = ledger_message or f"start {fix_id}"
        journal.ledger(
            message,
            kind="lane.started",
            lane=fix_record,
            details=details,
            directory=store.directory,
            project=store.project,
        )
        journal.ledger(
            f"review-hand-over {parent_id} to {fix_id} worktree={worktree}",
            kind="review.handed_over",
            lane=updated_parent,
            details={"to": fix_id, "worktree": worktree},
            directory=store.directory,
            project=store.project,
        )
        return fix_record, updated_parent


def _default_ledger(lane_id: str, event: str, record: Record) -> tuple[str, dict[str, Any]]:
    if event == "adopt":
        return (f"adopt {lane_id} worktree={record.get('worktree')} branch={record.get('branch')}",
                {"worktree": record.get("worktree"), "branch": record.get("branch"), "status": record["status"]})
    if event == "land":
        info: dict[str, Any] = record.get("landed") or {}
        return (f"land {lane_id} merge={info.get('merge')} previous={info.get('previous')}",
                {"merge": info.get("merge"), "previous": info.get("previous"), "tip": info.get("tip"),
                 "integration": info.get("integration"), "keep": bool(info.get("keep"))})
    if event == "release":
        return f"release {lane_id}", {"worktree": record.get("worktree")}
    if event == "finish":
        return f"done {lane_id}", {"freed_bytes": record.get("freed_bytes", 0)}
    if event == "drop":
        info = record.get("dropped") or {}
        return f"drop {lane_id}", {"why": info.get("why"), "freed_bytes": record.get("freed_bytes", 0)}
    if event == "park":
        info = record.get("parked") or {}
        return f"park {lane_id}", {"ref": info.get("ref"), "tip": info.get("tip"),
                                   "reason": info.get("reason"), "freed_bytes": record.get("freed_bytes", 0)}
    if event == "abandon":
        info = record.get("abandoned") or {}
        return f"abandon {lane_id}", {"reason": info.get("reason"), "tip": info.get("tip"),
                                      "kept_branch": info.get("kept_branch"),
                                      "freed_bytes": record.get("freed_bytes", 0)}
    return f"{event} {lane_id}", {}


def _emit_transition(store: state.Store, event: str, record: Record, message: str, details: dict[str, Any]) -> None:
    """Emit the matching `lane.*` event with a literal kind so every call site is statically checkable."""
    if event == "adopt":
        journal.ledger(message, kind="lane.adopted", lane=record, details=details,
                       directory=store.directory, project=store.project)
    elif event == "land":
        journal.ledger(message, kind="lane.landed", lane=record, details=details,
                       directory=store.directory, project=store.project)
    elif event == "release":
        journal.ledger(message, kind="lane.released", lane=record, details=details,
                       directory=store.directory, project=store.project)
    elif event == "finish":
        journal.ledger(message, kind="lane.done", lane=record, details=details,
                       directory=store.directory, project=store.project)
    elif event == "drop":
        journal.ledger(message, kind="lane.dropped", lane=record, details=details,
                       directory=store.directory, project=store.project)
    elif event == "park":
        journal.ledger(message, kind="lane.parked", lane=record, details=details,
                       directory=store.directory, project=store.project)
    elif event == "abandon":
        journal.ledger(message, kind="lane.abandoned", lane=record, details=details,
                       directory=store.directory, project=store.project)


def transition(store: state.Store, lane_id: str, event: str, *,
               force: bool = False,
               agent: dict[str, Any] | None = None,
               agent_now: str | None = None,
               overrides: list[dict[str, Any]] | None = None,
               post_merge: dict[str, Any] | None = None,
               post_merge_cleared_by: dict[str, Any] | None = None,
               ledger_message: str | None = None,
               ledger_details: dict[str, Any] | None = None,
               **fields: Any) -> Record:
    """Apply one lifecycle transition to `lane_id` in `store` under the `state` lock.

    Reloads the lane record under the lock, checks the transition table, applies `fields`, saves, and emits the
    matching `lane.*` event. Any pair outside the table is refused with `LaneError`; an idempotent repeat is a no-op.
    """
    changed = False

    def _apply(record: Record) -> bool:
        nonlocal changed
        current = str(record.get("status", ""))
        if (current, event) in TRANSITIONS:
            target = TRANSITIONS[(current, event)]
        elif force and event == "finish" and current in OCCUPYING:
            target = "done"
        elif IDEMPOTENT.get(event) == current:
            if event == "adopt" and (fields or agent is not None):
                target = current
            else:
                return False
        else:
            raise LaneError(f"lane {lane_id}: cannot {event} from status {current!r}")

        record["status"] = target
        for key, value in fields.items():
            record[key] = value
        if event == "finish" and "outcome" not in fields:
            if isinstance(record.get("landed"), dict) and record["landed"]:
                record["outcome"] = "landed"
        elif event == "park" and "outcome" not in fields:
            record["outcome"] = "parked"
        elif event == "abandon" and "outcome" not in fields:
            record["outcome"] = "abandoned"
        if event == "land":
            record.pop("landing", None)
            if isinstance(record.get("landed"), dict) and record.get("keep"):
                # Preserve a concurrent `lanefather keep` flag set before the land saved.
                record["landed"]["keep"] = True
        if overrides:
            existing_overrides: list[dict[str, Any]] = record.setdefault("overrides", [])
            existing_overrides.extend(overrides)
        if post_merge is not None:
            record["post_merge"] = dict(post_merge)
        if post_merge_cleared_by is not None and isinstance(record.get("post_merge"), dict):
            record["post_merge"]["cleared_by"] = post_merge_cleared_by
        if agent is not None:
            state.record_agent(record, event, agent, now=agent_now or journal.now())
        changed = True
        return True

    with store.locked("state"):
        record = store.update(lane_id, _apply)
        if changed and event in EVENT_KINDS:
            if ledger_message is not None:
                message = ledger_message
                details = dict(ledger_details or {})
                if event == "adopt":
                    details.setdefault("status", record["status"])
                elif event == "land" and isinstance(record.get("landed"), dict) and "keep" in details:
                    details["keep"] = bool(record["landed"].get("keep"))
            else:
                message, details = _default_ledger(lane_id, event, record)
            _emit_transition(store, event, record, message, details)
        return record
