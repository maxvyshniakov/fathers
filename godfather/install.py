#!/usr/bin/env python3
"""Install godfather for this user: `python3 install.py [--python /path/to/python3] [--bin DIR]`.

The package goes into an immutable release, ~/.local/lib/godfather/releases/<source digest>-<descriptor>/, prepared
in a private directory and renamed into place whole; `godfather` in ~/.local/bin (or --bin DIR) is a thin entry
point into it, replaced atomically. A dashboard already running keeps its release, and its footer shows ↻ once a
newer one is installed. The entry point names one interpreter: the one running this installer (or --python), never a
virtual environment's, which disappears with its project; Python 3.9 or later, standard library only. Slotfather's
install rules, without its queue migration.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

MIN_PYTHON = (3, 9)
COMMAND = "godfather"
USAGE = "usage: python3 install.py [--python /path/to/python3] [--bin DIR]"
SOURCES = ("install.py", "CHANGELOG.md", "LICENSE")


class InstallError(Exception):
    pass


def options(argv: list[str]) -> dict[str, str]:
    """Install options, each once with a value."""
    found: dict[str, str] = {}
    while argv:
        if argv[0] not in ("--python", "--bin") or len(argv) < 2 or argv[0] in found:
            raise InstallError(USAGE)
        found[argv[0]], argv = argv[1], argv[2:]
    return found


def interpreter(python: str | None = None) -> str:
    """The interpreter the command will name: --python PATH, else this one unless it belongs to a virtual
    environment, whose interpreter disappears with the project. It must exist and be Python 3.9 or later."""
    if python:
        python = os.path.abspath(os.path.expanduser(python))
    elif sys.prefix != sys.base_prefix:
        raise InstallError("this Python belongs to a virtual environment; run install.py with a Python outside it, "
                           "or name one with --python")
    else:
        python = sys.executable  # as invoked, not resolved: a version-manager link keeps following upgrades
    try:
        found, _, inside = subprocess.run(
            [python, "-c", "import sys; print('%d.%d' % sys.version_info[:2], sys.prefix != sys.base_prefix)"],
            capture_output=True, text=True, timeout=20).stdout.strip().partition(" ")
        version = tuple(int(part) for part in found.split("."))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise InstallError(f"cannot run {python} as Python") from None
    if inside == "True":
        raise InstallError(f"{python} belongs to a virtual environment; name a Python outside it")
    if version < MIN_PYTHON:
        raise InstallError(f"{python} is Python {found}; godfather needs {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or later")
    return python


def source_files(root: Path) -> list[Path]:
    """The runtime payload: the package's modules and what ships beside them, no bytecode or checkout files."""
    files = [root / name for name in SOURCES if (root / name).is_file() or name == "install.py"]
    package = root / "src" / "godfather"
    if package.is_dir():
        files.extend(sorted(path for path in package.rglob("*.py") if "__pycache__" not in path.parts))
    return files


def source_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in source_files(root):
        if not path.is_file():
            raise InstallError(f"source file missing: {path}")
        name = str(path.relative_to(root)).encode()
        payload = path.read_bytes()
        digest.update(len(name).to_bytes(8, "big") + name)
        digest.update(len(payload).to_bytes(8, "big") + payload)
    return digest.hexdigest()


def source_identity(root: Path) -> dict[str, Any]:
    """The commit and whether the runtime files differ from it, with the digest of the exact bytes installed."""
    digest = source_digest(root)
    result = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True)
    commit = result.stdout.strip() if result.returncode == 0 else None
    changed = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=normal", "--",
                              *SOURCES, "src"], capture_output=True, text=True)
    return {"v": 1, "commit": commit, "dirty": bool(changed.stdout.strip()) or commit is None,
            "source_sha256": digest}


def install_package(home: Path, root: Path, bin_dir: Path | None = None) -> Path:
    """A complete immutable release, prepared before any command can reference it; an existing one with the same
    identity must hold exactly the same bytes."""
    identity = source_identity(root)
    suffix = hashlib.sha256(json.dumps({"identity": identity}, sort_keys=True).encode()).hexdigest()[:12]
    prefix = home / ".local" if bin_dir is None or home in bin_dir.parents else bin_dir.parent
    releases = prefix / "lib/godfather/releases"
    releases.mkdir(parents=True, exist_ok=True)
    release = releases / (identity["source_sha256"] + "-" + suffix)
    pending = Path(tempfile.mkdtemp(prefix=".pending-", dir=releases))
    try:
        package = pending / "godfather"
        source_package = root / "src/godfather"
        for path in sorted(source_package.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            target = package / path.relative_to(source_package)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
        (package / "version.json").write_text(json.dumps(identity, sort_keys=True) + "\n")
        for name in ("CHANGELOG.md", "LICENSE"):
            if (root / name).is_file():
                shutil.copyfile(root / name, package / name)
        for path in pending.rglob("*"):
            path.chmod(0o755 if path.is_dir() else 0o644)
        pending.chmod(0o755)
        if source_digest(root) != identity["source_sha256"]:
            raise InstallError("source changed during install; retry from a stable checkout")
        if release.exists():
            expected = {str(p.relative_to(pending)): p.read_bytes() for p in pending.rglob("*") if p.is_file()}
            actual = {str(p.relative_to(release)): p.read_bytes() for p in release.rglob("*")
                      if p.is_file() and "__pycache__" not in p.parts}
            if actual != expected:
                raise InstallError(f"immutable installed release differs from source: {release}")
        else:
            os.replace(pending, release)
        return release
    finally:
        if pending.exists():
            shutil.rmtree(pending)


def wrapper(release: Path, python: str) -> bytes:
    return ("#!" + python + "\nimport sys\nsys.path.insert(0, " + repr(str(release)) + ")\n"
            "from godfather.app import main\nif __name__ == '__main__':\n    sys.exit(main())\n").encode()


def install_entry(payload: bytes, target: Path) -> None:
    """Writes the entry point beside its target and renames it over the old one: never half written."""
    target.parent.mkdir(parents=True, exist_ok=True)
    pending = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".godfather-", delete=False) as handle:
            pending = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fchmod(handle.fileno(), 0o755)
            os.fsync(handle.fileno())
        os.replace(pending, target)
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)


def install(home: Path, root: Path, python: str, bin_dir: Path | None = None) -> Path:
    bin_dir = bin_dir or home / ".local/bin"
    release = install_package(home, root, bin_dir)
    install_entry(wrapper(release, python), bin_dir / COMMAND)
    return release


def main() -> int:
    if sys.argv[1:] in (["--help"], ["-h"]):
        print(USAGE)
        return 0
    try:
        found = options(sys.argv[1:])
        python = interpreter(found.get("--python"))
        bin_dir = Path(os.path.expanduser(found["--bin"])) if "--bin" in found else None
        if bin_dir is not None and not bin_dir.is_absolute():
            raise InstallError("--bin takes an absolute directory")
        root = Path(__file__).resolve().parent
        release = install(Path.home(), root, python, bin_dir)
        print(f"Installed {(bin_dir or Path.home() / '.local/bin') / COMMAND} for {python}; release {release}")
    except (InstallError, OSError) as error:
        print(f"godfather install: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
