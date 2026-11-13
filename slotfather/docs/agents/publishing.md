# Publishing

Branches, the push, CI and installs are the repository's: [publishing](../../../docs/agents/publishing.md). Slotfather
adds only its changelog rules and the cautions of an install below.

## Changelog and versions

CHANGELOG.md uses the Sid-Kit format (`~/sid-kit/docs/VERSIONING.md`, Format and Rules): the title
`# Slotfather Changelog`, one `## Unreleased`, then releases as `## x.y.z - YYYY-MM-DD`, newest first, with
sections only when they have entries, in the order Breaking, Added, Changed, Deprecated, Removed, Fixed,
Security. Entries are user-facing. Before 1.0 a breaking or broad behaviour change takes a minor version,
anything else a patch. A release moves the Unreleased entries under the new version and sets it in
`pyproject.toml` and in `VERSION` in `src/slotfather/version.py`; `tests/test_changelog.py` checks the layout and
that both match the newest release (Repo-Kit's `check-changelogs` reads only packages under `packages/`).
`install.py` ships CHANGELOG.md with the package, where the dashboard's changelog (click the footer's version, or
`v`) reads it.

A push does not install the CLI, change its immutable installed package, migrate
the live queue or deploy to another machine. Those actions require their own
explicit scope and the compatibility rules in [installation](../installation.md).
