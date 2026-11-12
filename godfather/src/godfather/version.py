"""Identity of the exact runtime source, the footer's version and the changelog Godfather ships with."""
from __future__ import annotations

import contextlib
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any, Optional, cast

VERSION = "0.1.0"  # the release; CHANGELOG.md says what each one changed
NAME = "Godfather"
INSTALLED_RELEASE = re.compile(r"""sys\.path\.insert\(0, (?:'([^']+)'|"([^"]+)")\)""")

_identity: dict[str, Any] = {}


def info() -> dict[str, Any]:
    """Read immutable installed metadata, or derive it from this source checkout."""
    metadata = Path(__file__).with_name("version.json")
    if metadata.is_file():
        return dict(json.loads(metadata.read_text()), version=VERSION)
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("godfather_installer_version", root / "install.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot inspect godfather source identity")
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    return dict(installer.source_identity(root), version=VERSION)


def text(value: Optional[dict[str, Any]] = None) -> str:
    """A revision and content digest; a dirty checkout never masquerades as a clean commit."""
    value = info() if value is None else value
    commit = (value.get("commit") or "unversioned")[:7]
    return (f"godfather {value.get('version', VERSION)} ({commit}{'+dirty' if value.get('dirty') else ''}) "
            f"source {value['source_sha256']}")


def running_identity() -> dict[str, Any]:
    """The version metadata of the code this process runs, read once (info())."""
    if not _identity:
        try:
            _identity.update(info())
        except (OSError, ValueError, RuntimeError):
            _identity.update(commit=None, version=VERSION)
    return _identity


def running_version() -> str:
    """The footer's version: a semver where the package has one, else the short commit (+ for a dirty checkout)."""
    found = running_identity()
    if found.get("version"):
        return str(found["version"])
    commit = str(found.get("commit") or "")[:7]
    return commit + ("+" if commit and found.get("dirty") else "")


def running_source() -> Optional[str]:
    return running_identity().get("source_sha256")


def installed_source(entry: Optional[str] = None) -> Optional[str]:
    """The source digest of the release the installed `godfather` command runs now, from its entry point; None
    where it cannot be told (a checkout, a missing file). One other than running_source() means a newer install,
    and the footer shows ↻."""
    try:
        found = INSTALLED_RELEASE.search(Path(entry or sys.argv[0]).read_text())
        if found is None:
            return None
        data: Any = json.loads((Path(found[1] or found[2]) / "godfather" / "version.json").read_text())
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    digest = cast(dict[str, Any], data).get("source_sha256") if isinstance(data, dict) else None
    return str(digest) if digest else None


def installed_release() -> Optional[Path]:
    """The package directory the installed `godfather` command runs now, from its entry point."""
    with contextlib.suppress(OSError, UnicodeDecodeError):
        found = INSTALLED_RELEASE.search(Path(sys.argv[0]).read_text())
        if found:
            return Path(found[1] or found[2]) / "godfather"
    return None


def changelog_text() -> str:
    """The changelog bundled with the installed package; this package's own, or the checkout's, without one."""
    here = Path(__file__).resolve()
    for path in (installed_release(), here.parent, here.parents[2]):
        if path is not None and (path / "CHANGELOG.md").is_file():
            with contextlib.suppress(OSError, UnicodeDecodeError):
                return (path / "CHANGELOG.md").read_text()
    return ""
