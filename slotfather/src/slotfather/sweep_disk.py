"""Machine-wide disk hygiene for `slotfather sweep` (fathers#6).

Part A (Docker leftovers):
- `docker-anonymous`: dangling volumes labelled `com.docker.volume.anonymous` older than `docker_anonymous_age` (1 h).
- `docker-dangling-images`: `<none>` images older than `docker_dangling_age` (24 h) that no container uses.
- `docker-superseded`: image families matching `docker_superseded` patterns, keeping the newest
  `docker_superseded_keep` tags per repository and removing older tags that no container uses.
- Named volumes and unlisted image repositories are reported only, never removed.

Part B (scratchpads, package caches and low disk):
- `claude-scratch`: sessions under `/private/tmp/claude-<uid>` (or `/tmp/claude-<uid>`) larger than
  `claude_scratch_gb` (2 GB), untouched for `claude_scratch_age` (24 h) and used by no process; spares any
  directory that is the working directory of a running Junkfather entry (`junkfather list --json`, fathers#31).
- `package-caches`: sizes of uv, Homebrew, npm and bun caches; `--live` prunes uv (`uv cache prune`) and Homebrew
  (`brew cleanup`) once free disk space drops below `cache_free_gb`.
- `low-disk`: reports top disk consumers and, with `--live`, posts a low-disk notification when free disk space drops
  below `disk_free_gb` (100 GB).
"""
from __future__ import annotations

import contextlib
import datetime
import fnmatch
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any, Callable, Mapping, Sequence, cast

from . import config as _config
from .sweep_plan import Item, Snapshot, docker_time

DOCKER_CLASSES = ("docker-anonymous", "docker-dangling-images", "docker-superseded")
DISK_CLASSES = _config.SWEEP_DISK_CLASSES
TIMEOUT = 15.0
CACHE_PRUNE_TIMEOUT = 120.0

Run = Callable[..., "subprocess.CompletedProcess[str]"]


def gb_text(size_bytes: int | float) -> str:
    return f"{float(size_bytes) / 1e9:.1f} GB"


def _inside(child: str | None, root: str) -> bool:
    if not child:
        return False
    clean_root = root.rstrip("/")
    if child == clean_root or child.startswith(clean_root + "/"):
        return True
    real_child = os.path.realpath(child)
    real_root = os.path.realpath(clean_root)
    return real_child == real_root or real_child.startswith(real_root + "/")


def docker_timestamp(value: object) -> float | None:
    """Parse Docker's RFC 3339 or `YYYY-MM-DD HH:MM:SS +0000 UTC` CreatedAt timestamps into epoch seconds."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    parsed = docker_time(text)
    if parsed is not None:
        return parsed
    # `docker volume inspect` often writes `2026-11-10 12:00:00 +0000 UTC` or `+0200 EET`.
    parts = text.split()
    if len(parts) >= 3 and len(parts[0]) == 10 and len(parts[1]) >= 8:
        stamp = f"{parts[0]}T{parts[1][:8]}"
        offset_raw = parts[2]
        try:
            base = datetime.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S")
            if len(offset_raw) == 5 and offset_raw[0] in "+-" and offset_raw[1:].isdigit():
                sign = 1 if offset_raw[0] == "+" else -1
                hours, minutes = int(offset_raw[1:3]), int(offset_raw[3:5])
                tz = datetime.timezone(datetime.timedelta(seconds=sign * (hours * 3600 + minutes * 60)))
                return base.replace(tzinfo=tz).timestamp()
            return base.replace(tzinfo=datetime.UTC).timestamp()
        except ValueError:
            return None
    return None


def _docker_timeout() -> float:
    raw = os.environ.get("SLOTFATHER_SWEEP_DOCKER_TIMEOUT")
    if raw:
        with contextlib.suppress(ValueError):
            val = float(raw)
            if val > 0:
                return val
    return 30.0


def tree_bytes(root: Path) -> int:
    """Fallback allocated-bytes walk without following symlinks when `du` is unavailable."""
    total = 0
    pending = [str(root)]
    while pending:
        with contextlib.suppress(OSError), os.scandir(pending.pop()) as entries:
            for entry in entries:
                with contextlib.suppress(OSError):
                    info = entry.stat(follow_symlinks=False)
                    total += getattr(info, "st_blocks", 0) * 512 or info.st_size
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(entry.path)
    return total


def du_bytes(path: Path, run: Run = subprocess.run) -> int | None:
    """Directory size in bytes via `du -sk PATH` (1024-byte blocks on BSD and GNU); falls back to tree_bytes() if
    `du` is missing."""
    if not path.exists():
        return None
    try:
        result = run(["du", "-sk", str(path)], capture_output=True, text=True, timeout=TIMEOUT)
    except FileNotFoundError:
        return tree_bytes(path)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    first = (result.stdout.strip().split() or [""])[0]
    return int(first) * 1024 if first.isdigit() else None


def newest_mtime(root: Path) -> float | None:
    """Newest mtime at or below `root`, without following symlinks."""
    try:
        newest = os.lstat(root).st_mtime
    except OSError:
        return None
    pending = [str(root)]
    while pending:
        with contextlib.suppress(OSError), os.scandir(pending.pop()) as entries:
            for entry in entries:
                with contextlib.suppress(OSError):
                    info = entry.stat(follow_symlinks=False)
                    newest = max(newest, info.st_mtime)
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(entry.path)
    return newest


def free_disk_gb() -> float:
    configured = os.environ.get("SLOTFATHER_SWEEP_DISK_FREE_GB")
    if configured:
        with contextlib.suppress(ValueError):
            return float(configured)
    return shutil.disk_usage(Path.home()).free / 1e9


def junkfather_entries(run: Run = subprocess.run) -> list[dict[str, Any]]:
    """Active Junkfather entries from `junkfather list --json` (fathers#31); [] if unavailable."""
    try:
        result = run(["junkfather", "list", "--json"], capture_output=True, text=True, timeout=TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0 or not result.stdout.strip():
        return []
    try:
        parsed: object = json.loads(result.stdout)
    except ValueError:
        return []
    if not isinstance(parsed, dict):
        return []
    raw_entries = cast(dict[str, Any], parsed).get("entries")
    if not isinstance(raw_entries, list):
        return []
    active: list[dict[str, Any]] = []
    for raw in cast(list[object], raw_entries):
        if not isinstance(raw, dict):
            continue
        entry = cast(dict[str, Any], raw)
        state = entry.get("state")
        if state not in (None, "running", "starting"):
            continue
        entry_id = entry.get("id")
        if isinstance(entry_id, str) and entry_id:
            active.append(entry)
    return active


def _split_repo_tag(ref: str) -> tuple[str, str] | None:
    if not ref or ref == "<none>:<none>":
        return None
    last_slash = ref.rfind("/")
    colon = ref.rfind(":")
    if colon <= last_slash:
        return None
    repo, tag = ref[:colon], ref[colon + 1:]
    if not repo or not tag or repo == "<none>" or tag == "<none>":
        return None
    return repo, tag


def plan_docker(volumes: Sequence[Mapping[str, Any]], dangling_images: Sequence[Mapping[str, Any]],
                all_images: Sequence[Mapping[str, Any]], containers: Sequence[Mapping[str, Any]],
                settings: Mapping[str, Any], enabled: set[str], moment: float, live: bool = False) -> list[Item]:
    """Pure classifier for Part A Docker leftovers (`docker-anonymous`, `docker-dangling-images`,
    `docker-superseded`)."""
    used_images: dict[str, str] = {}
    used_volumes: set[str] = set()
    for cont in containers:
        cname = str(cont.get("Name") or cont.get("Id") or "container").lstrip("/")[:40]
        img_id = cont.get("Image")
        if isinstance(img_id, str) and img_id:
            used_images.setdefault(img_id, cname)
            used_images.setdefault(img_id.removeprefix("sha256:")[:12], cname)
        cfg = cont.get("Config")
        if isinstance(cfg, dict):
            cfg_img = cast(dict[str, Any], cfg).get("Image")
            if isinstance(cfg_img, str) and cfg_img:
                used_images.setdefault(cfg_img, cname)
        mounts = cont.get("Mounts")
        if isinstance(mounts, list):
            for mount in cast(list[object], mounts):
                if isinstance(mount, dict):
                    vname = cast(dict[str, Any], mount).get("Name")
                    if isinstance(vname, str) and vname:
                        used_volumes.add(vname)

    items: list[Item] = []

    if "docker-anonymous" in enabled:
        anon_age = float(settings["docker_anonymous_age"])
        for vol in volumes:
            name = str(vol.get("Name") or "").strip()
            if not name or name in used_volumes:
                continue
            labels = vol.get("Labels")
            is_anon = isinstance(labels, dict) and "com.docker.volume.anonymous" in cast(dict[str, Any], labels)
            created = docker_timestamp(vol.get("CreatedAt"))
            age = None if created is None else max(0.0, moment - created)
            usage_data = vol.get("UsageData")
            size_bytes = 0
            if isinstance(usage_data, dict):
                raw_sz = cast(dict[str, Any], usage_data).get("Size")
                if isinstance(raw_sz, int) and raw_sz > 0:
                    size_bytes = raw_sz
            if not is_anon:
                items.append({
                    "class": "docker-anonymous", "kind": "named-volume", "name": name, "id": name,
                    "age": None if age is None else round(age), "size_bytes": size_bytes,
                    "action": "report", "reason": "named volume (never removed)",
                })
                continue
            if age is None or age < anon_age:
                continue
            items.append({
                "class": "docker-anonymous", "kind": "volume", "name": name, "id": name[:16],
                "age": round(age), "size_bytes": size_bytes,
                "action": "remove" if live else "would-remove",
                "reason": f"dangling anonymous volume, age {_config.duration(age)}",
            })

    if "docker-dangling-images" in enabled:
        dangling_age = float(settings["docker_dangling_age"])
        seen_ids: set[str] = set()
        for img in dangling_images:
            img_id = str(img.get("Id") or "").strip()
            if not img_id or img_id in seen_ids:
                continue
            seen_ids.add(img_id)
            raw_tags = img.get("RepoTags")
            tags = [str(t) for t in cast(list[object], raw_tags) if t and str(t) != "<none>:<none>"] \
                if isinstance(raw_tags, list) else []
            if tags:
                continue
            short_id = img_id.removeprefix("sha256:")[:12] or img_id
            created = docker_timestamp(img.get("Created"))
            age = None if created is None else max(0.0, moment - created)
            if age is None or age < dangling_age or img_id in used_images or short_id in used_images:
                continue
            raw_size = img.get("Size")
            size_bytes = int(raw_size) if isinstance(raw_size, (int, float)) and raw_size > 0 else 0
            items.append({
                "class": "docker-dangling-images", "kind": "image", "name": short_id, "id": img_id,
                "age": round(age), "size_bytes": size_bytes,
                "action": "remove" if live else "would-remove",
                "reason": f"unused <none> image, age {_config.duration(age)}",
            })

    if "docker-superseded" in enabled:
        patterns = [str(p) for p in cast(Sequence[object], settings.get("docker_superseded") or [])]
        keep_n = max(1, int(float(settings.get("docker_superseded_keep") or 2)))
        by_repo: dict[str, list[tuple[float | None, str, str, int]]] = {}
        seen_refs: set[str] = set()
        for img in all_images:
            img_id = str(img.get("Id") or "").strip()
            created = docker_timestamp(img.get("Created"))
            raw_size = img.get("Size")
            size_bytes = int(raw_size) if isinstance(raw_size, (int, float)) and raw_size > 0 else 0
            raw_tags = img.get("RepoTags")
            if not isinstance(raw_tags, list):
                continue
            for raw_ref in cast(list[object], raw_tags):
                ref = str(raw_ref)
                if ref in seen_refs:
                    continue
                split = _split_repo_tag(ref)
                if split is None:
                    continue
                seen_refs.add(ref)
                by_repo.setdefault(split[0], []).append((created, ref, img_id, size_bytes))
        for repo in sorted(by_repo):
            entries = sorted(by_repo[repo], key=lambda row: (row[0] if row[0] is not None else -1.0, row[1]),
                             reverse=True)
            matched = any(fnmatch.fnmatchcase(repo, pat) for pat in patterns)
            if not matched:
                total_size = sum(row[3] for row in entries)
                items.append({
                    "class": "docker-superseded", "kind": "unlisted-image",
                    "name": f"{repo} ({len(entries)} tag(s))", "id": repo,
                    "age": None, "size_bytes": total_size,
                    "action": "report", "reason": "unlisted image family (never removed)",
                })
                continue
            for created, ref, img_id, size_bytes in entries[keep_n:]:
                short_id = img_id.removeprefix("sha256:")[:12]
                age = None if created is None else round(max(0.0, moment - created))
                holder = used_images.get(ref) or used_images.get(img_id) or used_images.get(short_id)
                if holder is not None:
                    items.append({
                        "class": "docker-superseded", "kind": "image-tag", "name": ref, "id": img_id,
                        "age": age, "size_bytes": size_bytes,
                        "action": "spared", "reason": f"superseded tag of {repo}; used by container {holder}",
                    })
                    continue
                items.append({
                    "class": "docker-superseded", "kind": "image-tag", "name": ref, "id": img_id,
                    "age": age, "size_bytes": size_bytes,
                    "action": "remove" if live else "would-remove",
                    "reason": f"older than newest {keep_n} tag(s) of {repo}",
                })

    return items


def _docker_json_list(argv: list[str], timeout: float, run: Run) -> list[dict[str, Any]] | None:
    try:
        res = run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if res.returncode != 0:
        return None
    try:
        parsed: object = json.loads(res.stdout or "[]")
    except ValueError:
        return None
    if not isinstance(parsed, list):
        return None
    return [cast(dict[str, Any], item) for item in cast(list[object], parsed) if isinstance(item, dict)]


def _docker_ids(argv: list[str], timeout: float, run: Run) -> tuple[list[str] | None, str | None]:
    try:
        res = run(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return None, "no docker command"
    except subprocess.TimeoutExpired:
        return None, f"docker did not answer in {timeout:g} s"
    except OSError as error:
        return None, f"docker: {error}"
    if res.returncode != 0:
        note = (res.stderr.strip().splitlines() or ["docker failed"])[0]
        return None, note
    seen: list[str] = []
    for token in res.stdout.split():
        if token not in seen:
            seen.append(token)
    return seen, None


def run_docker_hygiene(settings: Mapping[str, Any], enabled: set[str], moment: float, live: bool = False,
                       run: Run = subprocess.run) -> tuple[dict[str, Any], list[Item]]:
    """Inspect and, with `live`, remove eligible Docker anonymous volumes, dangling images, and superseded tags."""
    if not (enabled & set(DOCKER_CLASSES)):
        return {"available": None, "note": None}, []
    timeout = _docker_timeout()
    cids, err = _docker_ids(["docker", "ps", "-aq", "--no-trunc"], timeout, run)
    if cids is None:
        return {"available": False, "note": err}, []
    containers = (_docker_json_list(["docker", "inspect", *cids], timeout, run) or []) if cids else []

    volumes: list[dict[str, Any]] = []
    if "docker-anonymous" in enabled:
        vnames, verr = _docker_ids(["docker", "volume", "ls", "--filter", "dangling=true", "-q"], timeout, run)
        if vnames is None:
            return {"available": False, "note": verr}, []
        if vnames:
            volumes = _docker_json_list(["docker", "volume", "inspect", *vnames], timeout, run) or []

    dangling_images: list[dict[str, Any]] = []
    if "docker-dangling-images" in enabled:
        dids, derr = _docker_ids(["docker", "image", "ls", "--filter", "dangling=true", "-q", "--no-trunc"],
                                 timeout, run)
        if dids is None:
            return {"available": False, "note": derr}, []
        if dids:
            dangling_images = _docker_json_list(["docker", "image", "inspect", *dids], timeout, run) or []

    all_images: list[dict[str, Any]] = []
    if "docker-superseded" in enabled:
        iids, ierr = _docker_ids(["docker", "image", "ls", "-q", "--no-trunc"], timeout, run)
        if iids is None:
            return {"available": False, "note": ierr}, []
        if iids:
            all_images = _docker_json_list(["docker", "image", "inspect", *iids], timeout, run) or []

    items = plan_docker(volumes, dangling_images, all_images, containers, settings, enabled, moment, live=live)
    if live:
        for item in items:
            if item["action"] != "remove":
                continue
            target = str(item["id"] if item["class"] == "docker-dangling-images" else item["name"])
            cmd = ["docker", "volume", "rm", target] if item["class"] == "docker-anonymous" \
                else ["docker", "image", "rm", target]
            try:
                rm = run(cmd, capture_output=True, text=True, timeout=timeout)
                if rm.returncode == 0:
                    item["action"] = "removed"
                else:
                    item["action"] = "failed"
                    detail = (rm.stderr.strip().splitlines() or ["docker rm failed"])[0]
                    item["reason"] = f"{item['reason']}; {detail}"
            except (OSError, subprocess.TimeoutExpired) as error:
                item["action"] = "failed"
                item["reason"] = f"{item['reason']}; {error}"
    return {"available": True, "note": None}, items


def scratch_root(uid: int | None = None) -> Path:
    configured = os.environ.get("SLOTFATHER_SWEEP_SCRATCH_ROOT")
    if configured:
        return Path(configured)
    resolved_uid = os.geteuid() if uid is None else uid
    private = Path(f"/private/tmp/claude-{resolved_uid}")
    if sys.platform == "darwin" or private.exists():
        return private
    return Path(f"/tmp/claude-{resolved_uid}")


def _scratch_process_users(session: Path, snap: Snapshot) -> list[int]:
    target = str(session)
    real_target = os.path.realpath(target)
    users: list[int] = []
    for pid, proc in sorted(snap.procs.items()):
        if pid in snap.me:
            continue
        cwd = snap.cwds.get(pid)
        if (cwd is not None and _inside(cwd.path, target)) or target in proc.cmd \
                or (real_target != target and real_target in proc.cmd):
            users.append(pid)
    return users


def sweep_claude_scratch(snap: Snapshot, settings: Mapping[str, Any], live: bool = False,
                         run: Run = subprocess.run) -> list[Item]:
    """Inspect and, with `live`, remove idle >2 GB Claude scratchpad sessions unused by processes or Junkfather."""
    root = scratch_root(snap.uid)
    if not root.is_dir():
        return []
    floor_bytes = int(float(settings["claude_scratch_gb"]) * 1_000_000_000)
    age_floor = float(settings["claude_scratch_age"])
    jf_entries: list[dict[str, Any]] | None = None
    items: list[Item] = []
    for session in sorted(p for p in root.glob("*/*") if p.is_dir() and not p.is_symlink()):
        newest = newest_mtime(session)
        if newest is None:
            continue
        idle = max(0.0, snap.moment - newest)
        if idle < age_floor:
            continue
        size = du_bytes(session, run=run)
        if size is None or size <= floor_bytes:
            continue
        proc_users = _scratch_process_users(session, snap)
        if proc_users:
            items.append({
                "class": "claude-scratch", "kind": "scratchpad", "name": str(session), "path": str(session),
                "age": round(idle), "size_bytes": size, "action": "spared",
                "reason": f"{gb_text(size)}, untouched {_config.duration(idle)}; "
                          f"in use by pid {', '.join(map(str, proc_users[:3]))}",
            })
            continue
        if jf_entries is None:
            jf_entries = junkfather_entries(run=run)
        spared_jf = next(
            (str(entry["id"]) for entry in jf_entries
             if isinstance(entry.get("cwd"), str) and _inside(str(entry["cwd"]), str(session))),
            None,
        )
        if spared_jf is not None:
            items.append({
                "class": "claude-scratch", "kind": "scratchpad", "name": str(session), "path": str(session),
                "age": round(idle), "size_bytes": size, "action": "spared", "junkfather_entry": spared_jf,
                "reason": f"{gb_text(size)}, untouched {_config.duration(idle)}; "
                          f"cwd of running junkfather entry {spared_jf}",
            })
            continue
        item: Item = {
            "class": "claude-scratch", "kind": "scratchpad", "name": str(session), "path": str(session),
            "age": round(idle), "size_bytes": size,
            "action": "remove" if live else "would-remove",
            "reason": f"{gb_text(size)}, untouched {_config.duration(idle)}",
        }
        if live:
            try:
                shutil.rmtree(session)
                item["action"] = "removed"
            except OSError as error:
                item["action"] = "failed"
                item["reason"] = f"{item['reason']}; {error}"
        items.append(item)
    return items


def _command_output(argv: list[str], run: Run) -> str | None:
    if shutil.which(argv[0]) is None:
        return None
    try:
        res = run(argv, capture_output=True, text=True, timeout=TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = res.stdout.strip()
    return out.splitlines()[0].strip() if res.returncode == 0 and out else None


def discover_package_caches(run: Run = subprocess.run) -> list[tuple[str, Path, list[str] | None]]:
    """Return `(kind, directory, prune_argv)` for uv, Homebrew, npm, and bun caches."""
    home = Path(os.environ.get("SLOTFATHER_SWEEP_CACHE_HOME") or Path.home())
    uv_env = os.environ.get("UV_CACHE_DIR")
    uv_dir = Path(uv_env) if uv_env else (
        Path(found) if (found := _command_output(["uv", "cache", "dir"], run))
        else (home / "Library/Caches/uv" if sys.platform == "darwin" else home / ".cache/uv")
    )
    brew_env = os.environ.get("HOMEBREW_CACHE")
    brew_dir = Path(brew_env) if brew_env else (
        Path(found) if (found := _command_output(["brew", "--cache"], run))
        else home / "Library/Caches/Homebrew"
    )
    npm_dir = Path(os.environ.get("NPM_CONFIG_CACHE") or (home / ".npm"))
    bun_dir = Path(os.environ.get("BUN_INSTALL_CACHE_DIR") or (home / ".bun/install/cache"))

    uv_prune = ["uv", "cache", "prune"] if shutil.which("uv") else None
    brew_prune = ["brew", "cleanup"] if shutil.which("brew") else None
    return [
        ("uv", uv_dir, uv_prune),
        ("homebrew", brew_dir, brew_prune),
        ("npm", npm_dir, None),
        ("bun", bun_dir, None),
    ]


def sweep_package_caches(settings: Mapping[str, Any], live: bool = False,
                         run: Run = subprocess.run) -> list[Item]:
    """Report uv, Homebrew, npm, and bun cache sizes; with `live`, prune uv and Homebrew below `cache_free_gb`."""
    free_gb = free_disk_gb()
    threshold_gb = float(settings["cache_free_gb"])
    low = free_gb < threshold_gb
    items: list[Item] = []
    for kind, directory, prune_argv in discover_package_caches(run=run):
        if not directory.is_dir():
            continue
        before = du_bytes(directory, run=run)
        if before is None:
            continue
        if low and prune_argv is not None:
            item: Item = {
                "class": "package-caches", "kind": kind, "name": kind, "path": str(directory),
                "age": None, "size_bytes": before, "freed_bytes": 0,
                "action": "prune" if live else "would-prune",
                "reason": f"{gb_text(before)} at {directory}; free disk {free_gb:.0f} GB is under {threshold_gb:g} GB",
            }
            if live:
                try:
                    res = run(prune_argv, capture_output=True, text=True, timeout=CACHE_PRUNE_TIMEOUT)
                    if res.returncode == 0:
                        after = du_bytes(directory, run=run)
                        after_bytes = before if after is None else after
                        item["freed_bytes"] = max(0, before - after_bytes)
                        item["after_bytes"] = after_bytes
                        item["action"] = "pruned"
                    else:
                        item["action"] = "failed"
                        detail = (res.stderr.strip().splitlines() or [f"{prune_argv[0]} failed"])[0]
                        item["reason"] = f"{item['reason']}; {detail}"
                except (OSError, subprocess.TimeoutExpired) as error:
                    item["action"] = "failed"
                    item["reason"] = f"{item['reason']}; {error}"
            items.append(item)
        else:
            note = f"free disk {free_gb:.0f} GB (prunes below {threshold_gb:g} GB)" if prune_argv \
                else "reported only"
            items.append({
                "class": "package-caches", "kind": kind, "name": kind, "path": str(directory),
                "age": None, "size_bytes": before, "freed_bytes": 0,
                "action": "report", "reason": f"{gb_text(before)} at {directory}; {note}",
            })
    return items


def _applescript(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _consumer_candidates(uid: int, run: Run) -> list[Path]:
    configured = os.environ.get("SLOTFATHER_SWEEP_CONSUMER_PATHS")
    if configured is not None:
        return [Path(p) for p in configured.split(":") if p and Path(p).exists()]
    found: list[Path] = []
    sroot = scratch_root(uid)
    if sroot.is_dir():
        found.extend(sorted(p for p in sroot.glob("*/*") if p.is_dir() and not p.is_symlink()))
    home = Path.home()
    with contextlib.suppress(OSError):
        for wt_root in sorted(home.glob("*/.claude/worktrees")):
            if wt_root.is_dir():
                found.extend(sorted(p for p in wt_root.glob("*") if p.is_dir() and not p.is_symlink()))
    for _, cache_dir, _ in discover_package_caches(run=run):
        if cache_dir.is_dir() and cache_dir not in found:
            found.append(cache_dir)
    return found


def sweep_low_disk(uid: int, settings: Mapping[str, Any], live: bool = False,
                   run: Run = subprocess.run) -> tuple[dict[str, Any], list[Item]]:
    """Check free disk against `disk_free_gb`; when below, rank top consumers and notify under `live`."""
    free_gb = free_disk_gb()
    floor_gb = float(settings["disk_free_gb"])
    base: dict[str, Any] = {"free_gb": round(free_gb, 1), "floor_gb": floor_gb, "low": free_gb < floor_gb,
                            "notified": False, "top": []}
    if free_gb >= floor_gb:
        return base, []
    sized: list[tuple[Path, int]] = []
    for path in _consumer_candidates(uid, run):
        sz = du_bytes(path, run=run)
        if sz is not None:
            sized.append((path, sz))
    sized.sort(key=lambda pair: -pair[1])
    top: list[dict[str, Any]] = [
        {"path": str(path), "size_bytes": sz, "size_gb": round(sz / 1e9, 2)} for path, sz in sized[:10]
    ]
    base["top"] = top
    summary = ", ".join(f"{path.name} {gb_text(sz)}" for path, sz in sized[:3])
    reason = f"{free_gb:.0f} GB free, under {floor_gb:g} GB" + (f"; top: {summary}" if summary else "")
    message = f"{free_gb:.0f} GB free (budget {floor_gb:g} GB)" + (f". Top: {summary}" if summary else "")
    action = "would-notify"
    if live:
        notifier = os.environ.get("SLOTFATHER_SWEEP_NOTIFY")
        argv: list[str] | None = None
        if notifier:
            argv = [notifier, message]
        elif sys.platform == "darwin" and shutil.which("osascript"):
            argv = ["osascript", "-e", f'display notification {_applescript(message)} with title "Slotfather sweep"']
        elif shutil.which("notify-send"):
            argv = ["notify-send", "Slotfather sweep", message]
        if argv is None:
            action = "report"
        else:
            try:
                res = run(argv, capture_output=True, text=True, timeout=TIMEOUT)
                if res.returncode == 0:
                    action = "notified"
                    base["notified"] = True
                else:
                    action = "failed"
                    detail = (res.stderr.strip().splitlines() or ["notifier failed"])[0]
                    reason = f"{reason}; {detail}"
            except (OSError, subprocess.TimeoutExpired) as error:
                action = "failed"
                reason = f"{reason}; {error}"
    item: Item = {
        "class": "low-disk", "kind": "disk", "name": f"{free_gb:.0f} GB free",
        "age": None, "size_bytes": 0, "action": action, "reason": reason, "top": top,
    }
    return base, [item]


def sweep_disk(snap: Snapshot, settings: Mapping[str, Any], live: bool = False,
               only: set[int] | None = None, run: Run = subprocess.run) -> dict[str, Any]:
    """Run all enabled disk hygiene classes (`only is None`) and return the structured disk report."""
    enabled = set(cast(Sequence[str], settings.get("disk_classes") or ()))
    if only is not None or not enabled:
        return {
            "enabled": sorted(enabled) if only is None else [],
            "docker": {"available": None, "note": None},
            "low_disk": None,
            "items": [],
            "found": {c: 0 for c in DISK_CLASSES},
            "acted": {c: 0 for c in DISK_CLASSES},
            "would_act": 0,
            "failed": 0,
            "freed_bytes": 0,
            "freed_gb": 0.0,
        }
    docker_info, docker_items = run_docker_hygiene(settings, enabled, snap.moment, live=live, run=run)
    scratch_items = sweep_claude_scratch(snap, settings, live=live, run=run) if "claude-scratch" in enabled else []
    cache_items = sweep_package_caches(settings, live=live, run=run) if "package-caches" in enabled else []
    low_info, low_items = sweep_low_disk(snap.uid, settings, live=live, run=run) \
        if "low-disk" in enabled else (None, [])

    items = [*docker_items, *scratch_items, *cache_items, *low_items]
    found = {c: sum(i["class"] == c for i in items) for c in DISK_CLASSES}
    acted = {c: sum(i["class"] == c and i["action"] in ("removed", "pruned", "notified") for i in items)
             for c in DISK_CLASSES}
    would_act = sum(i["action"] in ("would-remove", "would-prune", "would-notify") for i in items)
    failed = sum(i["action"] == "failed" for i in items)
    freed_bytes = (
        sum(int(i.get("size_bytes") or 0) for i in items if i["action"] == "removed")
        + sum(int(i.get("freed_bytes") or 0) for i in items if i["action"] == "pruned")
    )
    return {
        "enabled": [c for c in DISK_CLASSES if c in enabled],
        "docker": docker_info,
        "low_disk": low_info,
        "items": items,
        "found": found,
        "acted": acted,
        "would_act": would_act,
        "failed": failed,
        "freed_bytes": freed_bytes,
        "freed_gb": round(freed_bytes / 1e9, 2),
    }
