# Tool Changelog

Intro prose before the first release heading.

- A bullet before any release heading is ignored.

## Unreleased

- A bullet before any section heading is ignored.

### Added

- `tool changelog --json` **parses** [the contract](docs/contracts/changelog.md)
  across wrapped lines,
  with a nested list:
  - nested item one

* A star bullet is ignored and ends the open bullet.
  So this indented line does not join the bullet above.

-

### Fixed

  Indented prose before a bullet under Fixed is ignored.
- Loose prose after a bullet ends continuation.

Loose prose outside the bullet.
  Indented line after loose prose is ignored.

#### A level-four heading

- Still under Fixed after the level-four heading.

## 0.10.0 - 2026-12-01

### Breaking

- Replaced `--old` with `--new`.

## 0.9.1 - 2026-11-25

## [0.2.0] - 2026-11-15

### Added

- First in 0.2.0.
- Second in 0.2.0.

### Notes

- Custom section name stays as written.

## 0.1.0+1138486 - 2026-11-12

### Added

- Newer build on 0.1.0.

## 0.1.0+7eca44a - 2026-11-11

### Fixed

- Earlier build on 0.1.0.

## 0.1.0 - 2026-11-10

### Added

- Initial release.

## Notes

### Added

- Heading without a version and date is not a release.

# Appendix

### Added

- Top-level heading resets release state until another release heading.
