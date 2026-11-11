# Contributing

Lanefather is one maintainer's tooling, shared as a reference. Issues with a reproduction are welcome. Pull
requests are read, but a change that adds scope or alters a command's behavior may be declined to keep the tool
small; open an issue first for anything beyond a small fix.

For a pull request:

- Keep the installed package free of third-party dependencies; it uses Python's standard library (3.11+).
- Add a focused test under `tests/` for a behavior change. Tests use real scratch Git repositories and executable
  stand-ins; see [ADR 0001](docs/adr/0001-real-adapters-before-general-ports.md).
- Leave `tests/fixtures/` unchanged: those files are frozen copies of the older tool and project seams that the
  compatibility tests run against (`tests/fixtures/PROVENANCE.json`).
- Keep the state formats compatible; see [State and its versions](docs/reference.md#state-and-its-versions).
- Run `python3 tools/check.py`: Ruff and strict Pyright at pinned versions through
  [uv](https://docs.astral.sh/uv/)'s `uvx`, then the tests. The pyright baseline only shrinks. The tests use
  [Slotfather](https://github.com/maxvyshniakov/slotfather) when it is on `PATH` and run without it otherwise.

Security reports go through [SECURITY.md](SECURITY.md), not issues. Contributions are licensed under the
[MIT License](LICENSE).
