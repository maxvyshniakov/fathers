# Publishing

Branches, the push, CI and installs are the repository's: [publishing](../../../docs/agents/publishing.md).
Lanefather adds only its changelog rules and its install below.

## Changelog and versions

[CHANGELOG.md](../../CHANGELOG.md) uses the Sid-Kit format: the title `# Lanefather Changelog`, one `## Unreleased`,
then releases as `## x.y.z - YYYY-MM-DD`, newest first, with sections only when they have entries, in the order
Breaking, Added, Changed, Deprecated, Removed, Fixed, Security. Entries are user-facing. Before 1.0 a breaking or
broad behaviour change takes a minor version, anything else a patch. A release moves the Unreleased entries under the
new version and sets it in `pyproject.toml` and `lanefather/__init__.py`; `tests/test_changelog.py` checks the layout
and that both match the newest release. An installed release also names its commit (`lanefather --version`).

## Install

Only on an explicit request, `python3 install.py` from a clean, committed `main`: a new immutable release and the
command swapped in one rename; running commands keep their release. Check it with `lanefather --version` and
`lanefather capabilities --json`, whose `installed` is true and whose commit is the pushed one. State compatibility
comes first: [State and its versions](../../README.md#state-and-its-versions).
