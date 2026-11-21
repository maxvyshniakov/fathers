# tokenfather

- Standard-library Python 3.11+ only.
- Provider-specific transcript fields stay inside `tokenfather/adapters/` and never leak into the core normalized schema.
- Verification: `python3 tools/check.py` and `python3 -m unittest discover -s tests`.
- The rest is the repository's: [AGENTS.md](../AGENTS.md).
