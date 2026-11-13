# Publishing

Branches, the push, CI and installs are the repository's: [publishing](../../../docs/agents/publishing.md).
Junkfather adds only its changelog rules and its install below.

## Changelog and versions

CHANGELOG.md uses the Sid-Kit format (`~/sid-kit/docs/VERSIONING.md`, Format): the title `# Junkfather Changelog`,
one `## Unreleased`, then releases as `## x.y.z - YYYY-MM-DD`, newest first, with sections only when they have
entries, in the order Breaking, Added, Changed, Deprecated, Removed, Fixed, Security. Entries are user-facing.
Before 1.0 a breaking or broad behaviour change takes a minor version, anything else a patch. A release moves the
Unreleased entries under the new version and sets it in `pyproject.toml` and `junkfather/__init__.py`;
`tests/test_changelog.py` checks the layout and that both match the newest release.

## Install

Only on an explicit request, `python3 install.py --launchd --mastack` from a clean, committed `main`: a new release
under `~/.local/share/junkfather`, the command in `~/.local/bin`, the hourly sweep reloaded, the hooks registered
through Mastack. A running supervisor keeps the release it started from.
