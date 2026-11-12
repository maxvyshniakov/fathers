# Commit map

Slotfather, Lanefather, junkfather and Godfather moved here on 2026-11-12 from their own repositories
(`maxvyshniakov/slotfather`, `lanefather`, `junkfather`, `godfather`), each from its `main` at the commit below, with
`git filter-repo --to-subdirectory-filter` and one merge per package. Every commit hash changed. The old repositories
keep their history, so an old hash still resolves there.

Before the move, the history was cleaned of private data, which also changed some file contents in old commits:

- slotfather: `tests/fixtures/upgrade/slotfather-fd422b00.tar.gz` no longer records a home directory in its
  `version.json`; `FIXTURE_SHA256` in `tests/test_live_cutover.py` and the fixture README follow it.
- lanefather: a homelab address in `tests/fixtures/legacy-lane` became `user@host.example`; seven commits made with
  a personal mail address carry the project address.
- godfather: the parity fixture `tests/fixtures/parity/slotfather/replies.json` no longer names a real host or a
  temporary path; `docs/slice-1/` (screenshots showing local paths) is gone from history.
- No package had tags.

The full maps, old hash then new hash, are [sha-map/](sha-map) (one TSV per package).

| Package | Old commit | New commit | What it is |
| --- | --- | --- | --- |
| slotfather | `81abd86c309c71ff206691dd4f527915096b51a7` | `78c2da5c922eb0ddd00420b5c3fba1f8f78b57bd` | tip (origin/main) |
| slotfather | `253d3dc3a3634ea795b821433f8f85fef2320bf7` | `fb7e57cfb53a26893920ca6a4ec5f7a3574f1081` | Release 0.1.0 |
| slotfather | `9391654601dc78032166dd41184b04cfa8d2a246` | `8860927f673a177edb8b2f955e0a7bea850ba3b3` | VM 110 pin (`slotfather_commit`) |
| slotfather | `37c4df70b3b20ca4671f4046ffa60d753eaec533` | `6694f07d5fddd1139991808875ac3a58e03d6a98` | Godfather parity fixture source |
| slotfather | `445be5826346e94163238420b687baa3c9a646d2` | `5c6939f748bb011ba3defe11ca423bdb9ed21439` | linked from lanefather/docs/admission.md |
| lanefather | `3ff62bcc222fb9059b8d775b177d437fda180522` | `113848618eca7b3686bb83cc91d00b1bdf3f4102` | tip (origin/main) |
| lanefather | `b5f856202c1ff36edf138d387047af88c44a9a30` | `7eca44a306abc89579d1bf2ba134d1b4ec62e821` | Straylight pin, `tools/lanefather.json` `source_commit` |
| lanefather | `993ff0ab70dae630d4fc52980662ab6443f7a9c6` | `48d81d7694c19d70592466a4896a205892e34465` | Straylight pin, `canonical_hook.source_commit` |
| junkfather | `5d36971c7fcbe48b0dbb561d9abb5c1e6fdc0570` | `3e2e22dd1c0c5b9bb35d138e4ca6aab1f967f8db` | tip (origin/main) |
| junkfather | `396f7eacca05bb0d5add0b35cee9fd117f781e8b` | `ebd488921d7714a4e71c844a420c148e35c6880f` | junkfather 0.1.0 |
| godfather | `faee68bde4d061a9a76835b79c000e68fd1a9c62` | `5311717286c3520a3638a7df161268dbe71e0c94` | tip (main) |
| godfather | `5e95ab79c134d76bb8018375053476b860123112` | `21fc7fba34cbfa4c56f0d9bc95bd1fd7afb3afe2` | Godfather 0.1.0 |
