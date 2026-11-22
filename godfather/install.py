#!/usr/bin/env python3
"""Install godfather for this user: `python3 install.py [--python /path/to/python3] [--bin DIR]`.

The package goes into an immutable release, ~/.local/lib/godfather/releases/<source digest>-<descriptor>/, prepared
in a private directory and renamed into place whole; `godfather` in ~/.local/bin (or --bin DIR) is a thin entry
point into it, replaced atomically. A dashboard already running keeps its release, and its footer shows ↻ once a
newer one is installed. The entry point names one interpreter: the one running this installer (or --python), never a
virtual environment's, which disappears with its project; Python 3.11 or later, standard library only.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Any

_ROOT_DIR = Path(__file__).resolve().parent
for _extra in (_ROOT_DIR / "src", _ROOT_DIR.parent / "common"):
    if _extra.is_dir() and str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

from fathers_common.installer import (  # noqa: E402
    InstallError,
    copy_python_tree,
    files_digest,
    git_source_identity,
    install_lock,
    resolve_interpreter,
    stage_release,
    write_launcher,
)

MIN_PYTHON = (3, 11)
COMMAND = "godfather"
USAGE = "usage: python3 install.py [--python /path/to/python3] [--bin DIR]"
SOURCES = ("install.py", "CHANGELOG.md", "LICENSE")


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
    environment, whose interpreter disappears with the project. It must exist and be Python 3.11 or later."""
    return resolve_interpreter(python, MIN_PYTHON, tool="godfather", allow_venv=False, allow_explicit_venv=False)


def source_files(root: Path) -> list[Path]:
    """The runtime payload: the package's modules, `fathers_common`, and what ships beside them."""
    files = [root / name for name in SOURCES if (root / name).is_file() or name == "install.py"]
    for sub in ("godfather", "fathers_common"):
        package = root / "src" / sub
        if package.is_dir():
            files.extend(sorted(path for path in package.rglob("*.py") if "__pycache__" not in path.parts))
    return files


def source_digest(root: Path) -> str:
    return files_digest(root, source_files(root))


def source_identity(root: Path) -> dict[str, Any]:
    """The commit and whether the runtime files differ from it, with the digest of the exact bytes installed."""
    status_paths: list[str] = [*SOURCES, "src"]
    common_source = root.parent / "common" / "fathers_common"
    if common_source.exists():
        status_paths.append(str(common_source))
    return git_source_identity(root, source_files(root), status_paths)


def install_package(home: Path, root: Path, bin_dir: Path | None = None) -> Path:
    """A complete immutable release, prepared before any command can reference it; an existing one with the same
    identity must hold exactly the same bytes."""
    identity = source_identity(root)
    suffix = hashlib.sha256(json.dumps({"identity": identity}, sort_keys=True).encode()).hexdigest()[:12]
    prefix = home / ".local" if bin_dir is None or home in bin_dir.parents else bin_dir.parent
    releases = prefix / "lib/godfather/releases"
    release_name = f"{identity['source_sha256']}-{suffix}"

    def populate(pending: Path) -> None:
        copy_python_tree(root / "src/godfather", pending / "godfather")
        if (root / "src/fathers_common").is_dir():
            copy_python_tree(root / "src/fathers_common", pending / "fathers_common")
        (pending / "godfather" / "version.json").write_text(json.dumps(identity, sort_keys=True) + "\n")
        for name in ("CHANGELOG.md", "LICENSE"):
            if (root / name).is_file():
                shutil.copyfile(root / name, pending / "godfather" / name)
        if source_digest(root) != identity["source_sha256"]:
            raise InstallError("source changed during install; retry from a stable checkout")

    return stage_release(releases, release_name, populate, world_readable=True)


def wrapper(release: Path, python: str) -> bytes:
    return ("#!" + python + "\nimport sys\nsys.path.insert(0, " + repr(str(release)) + ")\n"
            "from godfather.app import main\nif __name__ == '__main__':\n    sys.exit(main())\n").encode()


def install_entry(payload: bytes, target: Path) -> None:
    """Writes the entry point beside its target and renames it over the old one: never half written."""
    write_launcher(target, payload, mode=0o755)


def install(home: Path, root: Path, python: str, bin_dir: Path | None = None) -> Path:
    bin_dir = bin_dir or home / ".local/bin"
    with install_lock(home / ".local/state/.godfather-install.lock"):
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
