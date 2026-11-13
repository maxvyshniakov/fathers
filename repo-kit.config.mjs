// The check entry of the four packages (fathers#116): `bun run verify`, the pre-push gate on the pushed commits and
// Lanefather's lane checks (tools/lane-check) run these steps through @maxvyshniakov/repo-kit. The steps and which of
// them a change selects come from tools/checks.py: each changed package's lint gate and tests, every package for a
// change under common/, and always the cheap root checks. FATHERS_CHECKS=all runs every step.
import { execFileSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.dirname(fileURLToPath(import.meta.url));

export default {
  verify: {
    // Up to four Python suites side by side; each package's suite runs in a few minutes.
    slots: 2,
    steps: () => JSON.parse(execFileSync("python3", ["tools/checks.py", "steps"], { cwd: root, encoding: "utf8" })),
  },
  // Covers every pushed commit's verify, which runs under it.
  prePush: { slots: 2 },
  // fathers publishes main with an explicit `git push origin <sha>:refs/heads/main` and has no dev branch:
  // no reference-transaction guard (docs/agents/publishing.md).
  hooks: { managed: ["pre-push", "lane-check"] },
  // The packages live at the root (slotfather/, ...), not under packages/, and title their changelogs with the
  // capitalised name: Repo-Kit's check-changelogs and changelog-entry guard cannot address them. Each package's
  // tests/test_changelog.py checks its changelog and version instead.
};
