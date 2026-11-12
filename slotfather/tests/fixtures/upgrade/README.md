# Upgrade compatibility fixture

`slotfather-556f912.py.gz` is the unmodified installed-command source at commit
`556f912dd1354e6142b1204db9e0ca220495430e`, before the package extraction.
Its decompressed SHA-256 is `c320a2ca95e4da03fa73b5b86c836264b5a9716de0d6fbbcfacc5d8cd57be8b8`.
The installer test runs this real old-format client and proves that a breaking
request-format upgrade refuses while it holds a lease. The refusal leaves its
queue entry and published commands unchanged. The old client then finishes and
releases with its own code; installation succeeds after that drain. Its history
is converted by the finite installer migration, rather than a runtime fallback.
The compressed copy keeps the test self-contained in shallow clones and source archives.

`slotfather-fd422b00.tar.gz` is the package of the release installed on the Mac when
the live request-format cutover ran (#8): source digest
`fd422b00cd60905a3a75bfc92e5c52e6bcf35af595d95038f9d2b4ba092e7994`, commit 446575e with
local overlays recorded in its `version.json`, so it exists in no commit. It writes
top-level priority and ticket and mirrors them into metadata. The tar is deterministic
(sorted, mtime 0, no bytecode); its uncompressed SHA-256 is
`d7e5945497a18eafe2ea11ae59169c595777eb5fc5d86203e543a33a17ef750c`.
`tests/test_live_cutover.py` runs it against converted, old and mixed queues.
