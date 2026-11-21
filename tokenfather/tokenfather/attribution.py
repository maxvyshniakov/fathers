"""Lane attribution: join RequestRecords to Lanefather lanes via fathers#200 agent metadata or worktree-cwd fallback."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any

from .schema import RequestRecord, as_dict, as_list


@dataclass(frozen=True)
class LaneMeta:
    lane_id: str
    project: str
    status: str
    worktree: str
    agents: tuple[dict[str, Any], ...]


def _project_from_common_dir(common_dir: str) -> str:
    if not common_dir:
        return ""
    p = Path(common_dir)
    return p.parent.name if p.name == ".git" else p.name


def discover_lane_dirs(home: Path | None = None, extra_dirs: list[Path] | None = None) -> list[Path]:
    """Find Lanefather state directories from LANEFATHER_STATE, extra_dirs, and projects.json."""
    root_home = home or Path.home()
    dirs: list[Path] = []
    if extra_dirs:
        dirs.extend(extra_dirs)
    env_state = os.environ.get("LANEFATHER_STATE", "").strip()
    if env_state:
        dirs.append(Path(os.path.expanduser(env_state)))
    machine = root_home / ".local" / "state" / "lanefather"
    if machine.is_dir():
        dirs.append(machine)
        registry = machine / "projects.json"
        if registry.is_file():
            try:
                data = json.loads(registry.read_text(encoding="utf-8"))
                data_dict = as_dict(data)
                projects = data_dict.get("projects") if data_dict is not None else data
                proj_list = as_list(projects)
                proj_dict = as_dict(projects)
                if proj_list is not None:
                    for raw_entry in proj_list:
                        entry = as_dict(raw_entry)
                        if entry is not None and isinstance(entry.get("state_dir"), str):
                            dirs.append(Path(os.path.expanduser(entry["state_dir"])))
                elif proj_dict is not None:
                    for raw_entry in proj_dict.values():
                        entry = as_dict(raw_entry)
                        if entry is not None and isinstance(entry.get("state_dir"), str):
                            dirs.append(Path(os.path.expanduser(entry["state_dir"])))
            except (OSError, ValueError):
                pass
    unique: list[Path] = []
    seen: set[str] = set()
    for d in dirs:
        key = str(d)
        if key not in seen and d.is_dir():
            seen.add(key)
            unique.append(d)
    return unique


def load_lanes(lane_dirs: list[Path]) -> list[LaneMeta]:
    """Read lane JSON records from the given Lanefather state directories."""
    found: list[LaneMeta] = []
    for directory in lane_dirs:
        for path in sorted(directory.glob("*.json")):
            if path.name.count(".") != 1 or path.name in {"projects.json", "settings.json"}:
                continue
            try:
                data = as_dict(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
            if data is None or not isinstance(data.get("id"), str):
                continue
            worktree = str(data.get("worktree") or "")
            if worktree:
                worktree = os.path.realpath(worktree)
            raw_agents = as_list(data.get("agents"))
            agents = (
                tuple(a for item in raw_agents if (a := as_dict(item)) is not None)
                if raw_agents is not None
                else ()
            )
            project = str(data.get("project") or _project_from_common_dir(str(data.get("common_dir") or "")))
            found.append(LaneMeta(
                lane_id=data["id"],
                project=project,
                status=str(data.get("status") or ""),
                worktree=worktree,
                agents=agents,
            ))
    return found


class LaneIndex:
    """Fast lookup from a RequestRecord to its attributed LaneMeta."""

    def __init__(self, lanes: list[LaneMeta]):
        self.lanes = lanes
        self.by_id: dict[tuple[str, str], LaneMeta] = {(lane.project, lane.lane_id): lane for lane in lanes}
        self._by_bare_id: dict[str, list[LaneMeta]] = {}
        self.by_transcript: dict[str, LaneMeta] = {}
        self.by_subagent: dict[str, LaneMeta] = {}
        self.by_session: dict[str, LaneMeta] = {}
        self.by_worktree: list[tuple[str, LaneMeta]] = []

        session_claims: dict[str, dict[tuple[str, str], LaneMeta]] = {}
        for lane in lanes:
            lane_key = (lane.project, lane.lane_id)
            self._by_bare_id.setdefault(lane.lane_id, []).append(lane)
            for agent in lane.agents:
                tp = agent.get("transcript_path")
                if isinstance(tp, str) and tp:
                    self.by_transcript[os.path.realpath(tp)] = lane
                    self.by_transcript[tp] = lane
                sub = agent.get("subagent_id")
                if isinstance(sub, str) and sub:
                    self.by_subagent[sub] = lane
                sess = agent.get("session_id")
                if isinstance(sess, str) and sess:
                    session_claims.setdefault(sess, {})[lane_key] = lane
                tid = agent.get("thread_id")
                if isinstance(tid, str) and tid:
                    session_claims.setdefault(tid, {})[lane_key] = lane
            if lane.worktree:
                self.by_worktree.append((lane.worktree, lane))
        # Only attribute by session_id/thread_id when unambiguous (unique to one lane); shared coordinator
        # sessions fall back to transcript_path, subagent_id, or worktree match.
        for sess_id, claimed in session_claims.items():
            if len(claimed) == 1:
                self.by_session[sess_id] = next(iter(claimed.values()))
        # Longest worktree path first so nested paths match the most specific worktree.
        self.by_worktree.sort(key=lambda item: len(item[0]), reverse=True)

    def get_lane(self, lane_id: str, project: str = "") -> LaneMeta | None:
        if (project, lane_id) in self.by_id:
            return self.by_id[(project, lane_id)]
        if not project:
            matches = self._by_bare_id.get(lane_id) or []
            if len(matches) == 1:
                return matches[0]
        return None

    def match(self, record: RequestRecord) -> LaneMeta | None:
        # 1. Exact fathers#200 agent metadata matches (transcript_path -> subagent_id -> session_id).
        if record.transcript_path:
            if record.transcript_path in self.by_transcript:
                return self.by_transcript[record.transcript_path]
            resolved_tp = os.path.realpath(record.transcript_path)
            if resolved_tp in self.by_transcript:
                return self.by_transcript[resolved_tp]
        if record.agent_id and record.agent_id in self.by_subagent:
            return self.by_subagent[record.agent_id]
        if record.session_id and record.session_id in self.by_session:
            return self.by_session[record.session_id]
        # 2. Fallback: lane's worktree path against the request's project/cwd.
        if record.project:
            resolved_cwd = os.path.realpath(record.project)
            for wt, lane in self.by_worktree:
                if resolved_cwd == wt or resolved_cwd.startswith(wt + os.sep):
                    return lane
        return None

    def attribute(self, records: list[RequestRecord]) -> None:
        for record in records:
            lane = self.match(record)
            if lane is not None:
                record.lane_id = lane.lane_id
                record.lane_project = lane.project
