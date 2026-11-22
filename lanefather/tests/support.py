"""The fixture of every command test: replay orchestration incidents against lanefather in scratch Git repositories.

The tests began as Straylight's tools/test-lane.py (audit 5.1, B4) and moved with the tool (#648); the fixture
repository is shaped like Straylight's, with a .lanefather.toml naming its seams (FIXTURE_CONFIG). Each fixture
repository carries a stand-in verify.sh: `--changed --plan --json`
plans from a small check graph (structure-check reads everything, lib-check lib/ and
calls/, schema-check lib/schema/, native-check native/, host-journeys-check
docs/install.md; tooling/ is a change every check reads and unowned/ a path no check reads,
both the full gate; runner/ is the runner, which selects its canary lib-check since #638),
and `--checks` or `--suites` run checks/<suite>.sh for each check,
so a seam between two lanes and a frozen corpus can be broken in a few lines of text.
"""
from __future__ import annotations

import atexit
from collections.abc import Iterator
import contextlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import types
from typing import Any
import unittest
import unittest.mock

# The repository root: lanefather's package is importable from it.
ROOT = pathlib.Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Lanefather's machine-wide files (the event log, lanefather.events; the project registry, lanefather.registry;
# the machine settings, lanefather.settings) never reach this machine's in a test: the
# in-process tests write to a scratch directory, and each command test to its own (LaneCase.setUp).
MACHINE = pathlib.Path(tempfile.mkdtemp(prefix="lanefather-machine-"))
atexit.register(shutil.rmtree, MACHINE, True)
MACHINE_FILES = {"LANEFATHER_EVENTS": "events.jsonl", "LANEFATHER_REGISTRY": "projects.json",
                 "LANEFATHER_SETTINGS": "settings.json"}
for _name, _file in MACHINE_FILES.items():
    os.environ[_name] = str(MACHINE / _file)

# Straylight's seams as its .lanefather.toml names them, in the fixture's paths.
FIXTURE_CONFIG = textwrap.dedent("""\
    project = "Fixture"
    queue_name = "the #347 queue"
    report_doc = "docs/agents/orchestration.md#implementer-brief"
    rules_dir = "tools/agent-hooks/rules"
    unclaimed = ["release-notes/"]
    merge_config = ["merge.release-notes.name=Union of release notes entries",
                    "merge.release-notes.driver=git merge-file --union --diff3 -q %A %O %B"]

    [checks]
    verify = "verify.sh"
    plan_module = "tools/verification_plan.py"
    triage = ["python3", "tools/gate-triage.py"]
    toolchain = ["python3", "tools/rust-toolchain.py", "--json"]
    toolchain_pin = "rust-toolchain.toml"

    [lane]
    cache_name = "fixture"
    targets = ["lane-{id}-target"]
    cache_partitions = "cargo-v2"

    [gate]
    stamp_tool = "tools/verified-tree.sh"

    [promote]
    ship_refuses = "release-notes/unreleased"
    ship_refusal = "run node tools/release-notes.mjs release, commit, and ship that commit"
    install_env_drop = ["STRAYLIGHT_UNPROMOTED", "LANDING_TOKEN"]
    after = ["python3", "tools/after-promote.py", "{sha}", "--source", "promote {short}"]

    [[promote.install]]
    name = "install"
    argv = ["./install-control-app.sh"]

    [[promote.install]]
    name = "check-installed"
    argv = ["python3", "tools/check-installed.py", "--expect", "{sha}"]

    [[promote.deploy]]
    name = "link-package"
    argv = ["tools/straylight-link/build-package.sh", "{package}", "linux/amd64"]

    [[promote.deploy]]
    name = "deploy"
    argv = ["tools/straylight-link/deploy.sh", "{host}"]
    env = { STRAYLIGHT_LINK_PACKAGE = "{package}" }

    [hooks]
    process_scan = "tools/process_scan.py"
    """)

FAKE_VERIFY = r'''#!/usr/bin/env python3
import json, os, pathlib, subprocess, sys, time, uuid
SUITES = ("structure", "lib", "native", "host-journeys")
# A stand-in verification graph: each check's suite and the path prefixes it reads.
CHECKS = {"structure-check": ("structure", ("",)), "lib-check": ("lib", ("lib/", "calls/")),
          "schema-check": ("lib", ("lib/schema/",)),
          "native-check": ("native", ("native/",)), "host-journeys-check": ("host-journeys", ("docs/install.md",)),
          # The perf suite (#579): only --suites perf runs it, never a plan or the full gate.
          "perf:budgets": ("perf", ())}
# The checks a runner change selects besides the structure check (tools/verification_plan.py's CANARY).
CANARY = ("lib-check",)
root = pathlib.Path(__file__).resolve().parent
args = sys.argv[1:]
if args[:1] == ["--changed"]:
    base = args[args.index("--base") + 1]
    paths = sorted(set(subprocess.check_output(["git", "diff", "--name-only", base, "--"], cwd=root, text=True).split()))
    # tooling/ stands for a change every check reads, the full gate (tools/verification_plan.py's plan() without a
    # base graph to compare). runner/ stands for the runner (verify.sh, tools/verify.py: RUNNER): since #638 the
    # plan selects its CANARY checks for it, not every check.
    tooling = any(path.startswith("tooling/") for path in paths)
    # A path no check reads selects the full gate too; only the structure check, which runs for every change, is
    # chosen by a path, and the plan's other checks are rows without paths, as verification_plan.plan() gives them.
    unowned = [p for p in paths if p.startswith("unowned/")]
    full = tooling or bool(unowned)
    rows = [{"id": id, "suite": suite, "paths": [p for p in paths if tooling or p.startswith(reads)
                                                 and (id == "structure-check" or p not in unowned)
                                                 or p.startswith("runner/") and id in CANARY]}
            for id, (suite, reads) in CHECKS.items() if suite in SUITES]
    rows = [dict(row, producer=not row["paths"]) for row in rows if full or row["paths"]]
    suites = [s for s in SUITES if any(row["suite"] == s and not row["producer"] for row in rows)]
    reasons = {s: sorted({p for row in rows if row["suite"] == s for p in row["paths"]}) for s in suites}
    plan = {"full": full, "paths": paths, "unowned": unowned, "checks": rows, "suites": suites, "reasons": reasons}
    if os.environ.get("FIXTURE_SUITE_PLAN"):
        # A tree from before the verification graph: its plan names whole suites.
        plan = {"full": full, "paths": paths, "suites": suites, "reasons": reasons}
    print(json.dumps(plan))
    sys.exit(0)
full = "--suites" not in args and "--checks" not in args
min_jobs_env = os.environ.get("LANEFATHER_SLOT_MIN_JOBS")
if min_jobs_env and not os.environ.get("SLOTFATHER_LEASE") and "--jobs" in args:
    max_jobs = args[args.index("--jobs") + 1]
    slot_cmd = os.environ.get("LANEFATHER_SLOT_CMD", "slotfather")
    os.execvp(slot_cmd, [slot_cmd, "run", "-n", f"{min_jobs_env}-{max_jobs}",
                         "--label", os.environ.get("LANEFATHER_SLOT_LABEL", "verify"), "--",
                         sys.executable, __file__, *args])
if "--checks" in args:
    ids = args[args.index("--checks") + 1].split(",")
    if any(id not in CHECKS for id in ids):
        # Like verify.py, an unknown check id refuses the whole selection.
        print("Unknown verification check or empty selection", file=sys.stderr)
        sys.exit(64)
    run_checks = [(id, CHECKS[id][0]) for id in ids]
    line = "checks " + ",".join(id for id, _ in run_checks)
else:
    suites = list(SUITES) if full else args[args.index("--suites") + 1].split(",")
    run_checks = [(id, suite) for id, (suite, _) in CHECKS.items() if suite in suites]
    line = ("FULL " if full else "") + ",".join(suites)
with open(os.environ["FIXTURE_VERIFY_LOG"], "a") as log:
    log.write(line + "\n")
with open(os.environ["FIXTURE_VERIFY_LOG"] + ".meta", "a") as log:
    log.write(os.environ.get("LANEFATHER_SLOT_META", "{}") + "\n")
with open(os.environ["FIXTURE_VERIFY_LOG"] + ".requests", "a") as log:
    log.write(json.dumps({"label": os.environ.get("LANEFATHER_SLOT_LABEL"),
                          "priority": os.environ.get("LANEFATHER_SLOT_PRIORITY"),
                          "ticket": os.environ.get("LANEFATHER_SLOT_TICKET"),
                          "meta": json.loads(os.environ.get("LANEFATHER_SLOT_META", "{}"))}) + "\n")
# Like verify.py, only the full gate stamps, through tools/verified-tree.sh start before and finish after.
start = subprocess.check_output([str(root / "tools/verified-tree.sh"), "start"], cwd=root, text=True).strip() if full else None
# verify.py takes its own slot lease (B10): record the label and --jobs the lane hands it.
with open(os.environ["FIXTURE_VERIFY_LOG"] + ".leases", "a") as log:
    # A run under a lease its caller holds (SLOTFATHER_LEASE) takes none of its own: record how many slots.
    held = f" held {os.environ['SLOTFATHER_COUNT']}" if os.environ.get("SLOTFATHER_LEASE") else ""
    log.write(os.environ.get("LANEFATHER_SLOT_LABEL", "-") + " --jobs " + args[args.index("--jobs") + 1] + held + "\n")
head = lambda: subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
detached = subprocess.call(["git", "symbolic-ref", "-q", "HEAD"], cwd=root, stdout=subprocess.DEVNULL) != 0
# Each run's label, commit and checkout: a lane's own run, its rerun alone, or its base's (a detached worktree).
with open(os.environ["FIXTURE_VERIFY_LOG"] + ".runs", "a") as log:
    log.write(" ".join((os.environ.get("LANEFATHER_SLOT_LABEL", "-"), head(), "detached" if detached else "branch",
                        str(root), line)) + "\n")
if detached and os.environ.get("FIXTURE_DETACHED_HOLD"):
    # Hold a run in a detached checkout (the base) until the test releases it.
    hold = pathlib.Path(os.environ["FIXTURE_DETACHED_HOLD"])
    pathlib.Path(str(hold) + ".started").write_text(str(root))
    while not hold.exists():
        time.sleep(0.05)
# FIXTURE_HOLD=<run label>:<file>: the run with that label notes its checkout in <file>.started and holds until the
# test creates the file.
hold_label, _, hold_file = os.environ.get("FIXTURE_HOLD", "").partition(":")
if hold_label and hold_label == os.environ.get("LANEFATHER_SLOT_LABEL"):
    pathlib.Path(hold_file + ".started").write_text(str(root))
    while not os.path.exists(hold_file):
        time.sleep(0.05)
if full and os.environ.get("FIXTURE_GATE_HOLD"):
    # Hold the gate until the test releases it, noting the gate worktree's HEAD before and after.
    hold = pathlib.Path(os.environ["FIXTURE_GATE_HOLD"])
    with open(str(hold) + ".heads", "a") as log:
        log.write("start " + head() + "\n")
    while not hold.exists():
        time.sleep(0.05)
    with open(str(hold) + ".heads", "a") as log:
        log.write("end " + head() + "\n")
rows, status = [], 0
run = root / ".build" / "verification-runs" / (time.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8])
run.mkdir(parents=True)
# FIXTURE_FLAKE_ONCE=<check id>:<flag file>: the check fails while the flag exists; that failure removes it.
flake_id, _, flake_flag = os.environ.get("FIXTURE_FLAKE_ONCE", "").partition(":")
environment = os.environ.get("FIXTURE_ENVIRONMENT")
stopped = None
for index, (id, suite) in enumerate(run_checks):
    if stopped:
        break
    script = root / "checks" / (suite + ".sh")
    check_log = run / (id + ".log")
    began = time.monotonic()
    with open(check_log, "w") as out:
        code = subprocess.call(["bash", str(script)], cwd=root, stdout=out, stderr=subprocess.STDOUT) if script.exists() else 0
    sys.stdout.write(check_log.read_text())
    if id == flake_id and os.path.exists(flake_flag):
        os.remove(flake_flag)
        code = 1
    if environment and suite == "native":
        # The run's Rust changed under it and the native check failed with it (#571).
        code = 1
    # Like verify.py, each row carries its duration (fathers#92).
    rows.append({"id": id, "status": "passed" if code == 0 else "failed", "exit": code, "log": str(check_log),
                 "seconds": round(time.monotonic() - began, 6)})
    status = status or code
    # Like verify.py (#639), the structure check is the cheap tier: red, it stops the run before the other checks.
    if id == "structure-check" and code and run_checks[index + 1:]:
        stopped = {"failed": [id], "not_run": len(run_checks) - index - 1}
# FIXTURE_STATUS=<run label>:<check>=<status>: that run reports the check so, as verify.py reports a check whose
# producer failed (blocked) or that Linux could not run (unrun).
status_label, _, status_row = os.environ.get("FIXTURE_STATUS", "").partition(":")
if status_label and status_label == os.environ.get("LANEFATHER_SLOT_LABEL"):
    status_id, _, forced = status_row.partition("=")
    for row in rows:
        if row["id"] == status_id:
            row.update(status=forced, exit=None, **({"blocked_by": ["fixture-build"]} if forced == "blocked" else {}))
            status = status or (1 if forced in ("blocked", "failed") else 0)
report = {"exit": status, "checks": rows, "stopped": stopped}
if os.environ.get("LANEFATHER_SLOT_MIN_JOBS") and os.environ.get("SLOTFATHER_COUNT"):
    report["jobs"] = int(os.environ["SLOTFATHER_COUNT"])
if environment:
    rows.append({"id": "environment:toolchain", "status": "failed", "exit": 1, "log": None})
    report.update(exit=1, environment={"class": environment, "detail": "environment: " + environment})
    status = 1
(run / "report.json").write_text(json.dumps(report))
print("Verification report: " + str(run / "report.json"))
# FIXTURE_EXIT=<run label>:<code>: that run exits so after its report, whose checks may all have passed, as verify.py
# exits for a failure outside any check (setup, the slot limiter).
exit_label, _, exit_code = os.environ.get("FIXTURE_EXIT", "").partition(":")
if exit_label and exit_label == os.environ.get("LANEFATHER_SLOT_LABEL"):
    sys.exit(int(exit_code))
if full and status == 0:
    if os.environ.get("FIXTURE_DIRTY_DURING_RUN"):
        (root / "README.md").write_text("edited while the gate ran\n")
    sys.stdout.flush()
    status = subprocess.call([str(root / "tools/verified-tree.sh"), "finish", start], cwd=root)
sys.exit(status)
'''

FAKE_PLAN = r'''"""Stand-in for tools/verification_plan.py: the fixture's suites, their checks and the per-land smoke."""
import os
from dataclasses import dataclass

SUITES = ("structure", "lib", "native", "host-journeys")
CHECKS = {"structure-check": "structure", "lib-check": "lib", "schema-check": "lib", "native-check": "native",
          "host-journeys-check": "host-journeys"}


@dataclass(frozen=True)
class Check:
    id: str
    suite: str


def checks(root, scratch, selected, keep_going, native_only, jobs=4, **_):
    return [Check(id, suite) for id, suite in CHECKS.items() if suite in selected]


def smoke(root):
    # native and host-journeys stand for the slow suites only the batch gate runs; FIXTURE_SMOKE names another smoke.
    return os.environ.get("FIXTURE_SMOKE", "structure-check,lib-check").split(",")
'''

RUST_STUB = r'''import json, os, sys
# Stand-in for tools/rust-toolchain.py (#571): FIXTURE_RUST names the Rust it serves, "missing" refuses.
rust = os.environ.get("FIXTURE_RUST", "1.98.1")
if rust == "missing":
    print("environment: toolchain not pinned: rust-toolchain.toml pins Rust 1.98.1, but PATH serves none",
          file=sys.stderr)
    sys.exit(1)
print(json.dumps({"pinned": "1.98.1", "source": "rustup", "bin": "/rust/" + rust,
                  "current": {"rustc": "rustc " + rust}}))
'''

FILES = {
    "README.md": "fixture\n",
    ".lanefather.toml": FIXTURE_CONFIG,
    # The after-promote command (Straylight: the Sid-Kit and Data Grid adoption check, #588) logs its call.
    "tools/after-promote.py": textwrap.dedent("""\
        import os, sys
        log = os.environ.get("FIXTURE_AFTER_PROMOTE_LOG")
        if log:
            with open(log, "a") as handle:
                handle.write(" ".join(sys.argv[1:]) + "\\n")
        print("after-promote: checked " + sys.argv[1][:10])
        sys.exit(int(os.environ.get("FIXTURE_AFTER_PROMOTE_EXIT", "0")))
        """),
    "CODING_STANDARDS.md": "# Coding standards\n\n1. Fixture rule: a shortcut keeps every check it skips.\n",
    "tools/verification_plan.py": FAKE_PLAN,
    # Mirrors the real .gitignore: importing the fake verification_plan writes tools/__pycache__.
    ".gitignore": ".build/\nnode_modules/\ntarget/\n.claude/\n__pycache__/\n",
    ".gitattributes": "release-notes/*.md merge=release-notes\n",
    "release-notes/1.0.md": "# 1.0\n",
    "lib/api.txt": "foo\n",
    "calls/main.txt": "foo\n",
    "native/straylight-core/corpus.txt": "v1\n",
    "checks/frozen-corpus.txt": "v1\n",
    "checks/structure.sh": "exit 0\n",
    "checks/native.sh": "cmp native/straylight-core/corpus.txt checks/frozen-corpus.txt\n",
    # Stand-in for tools/gate-triage.py (#404 flakes): names the run it was given.
    "tools/gate-triage.py": textwrap.dedent("""\
        import pathlib, sys
        print("Gate triage: " + pathlib.Path(sys.argv[1]).name + ": 1 red, 0 registered-flake, 1 unclassified")
        sys.exit(1)
        """),
    "checks/lib.sh": textwrap.dedent("""\
        status=0
        for name in $(cat calls/*.txt); do
          grep -qx "$name" lib/api.txt || { echo "calls $name, which lib/api.txt does not define"; status=1; }
        done
        exit $status
        """),
}

FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_GH_LOG"], "a") as log:
    log.write(" ".join(args) + "\n")
if args[:2] == ["issue", "create"] and os.environ.get("FAKE_GH_FAIL_CREATE"):
    print("HTTP 502: Bad Gateway", file=sys.stderr)
    sys.exit(1)
if args[:2] == ["issue", "create"]:
    # Each created issue gets the next number after the lines logged so far.
    print("https://github.com/fixture/straylight/issues/" + str(900 + len(open(os.environ["FAKE_GH_LOG"]).readlines())))
elif "--json" in args and "title" in args[args.index("--json") + 1]:
    print(json.dumps({"title": "Fixture ticket title", "body": "The fixture ticket body."}))
else:
    print(json.dumps({"comments": json.loads(open(os.environ["FAKE_GH_COMMENTS"]).read())}))
'''

# Stand-ins for the two reviewers (#570). codex answers `app-server` JSON-RPC with the weekly limit
# (FIXTURE_CODEX_USED percent used, FIXTURE_CODEX_REACHED a rateLimitReachedType) and `exec` with the
# review in FIXTURE_REVIEW_TEXT; FIXTURE_CODEX_EXEC=model-error|limit|empty|no-output fails the run as Sol can.
# Both log their argv and keep the prompt they were given; FIXTURE_REVIEW_HOLD holds a run until it exists.
# FIXTURE_PANEL_MEET (a FIFO) makes codex open it for reading and claude for writing, so each blocks until the
# other has started (fathers#151): a panel that ran them one after the other never meets, and the stand-in exits 3
# after 60 s. Codex writes its answer before it meets; FIXTURE_MEET_LIST (a directory) makes claude record what that
# directory holds once both met, in FIXTURE_MEET_LIST + ".seen". FIXTURE_CLAUDE_TEXT gives Claude another answer.
MEET = r'''
def meet(name):
    fifo = os.environ.get("FIXTURE_PANEL_MEET")
    if not fifo:
        return
    def late(*_):
        print(f"{name}: the other reviewer never started beside me", file=sys.stderr)
        os._exit(3)
    signal.signal(signal.SIGALRM, late)
    signal.alarm(60)
    with open(fifo, "r" if name == "codex" else "w"):
        pass
    signal.alarm(0)
    listed = os.environ.get("FIXTURE_MEET_LIST")
    if listed and name == "claude":
        pathlib.Path(listed + ".seen").write_text(json.dumps(sorted(os.listdir(listed))))
'''
FAKE_CODEX = r'''#!/usr/bin/env python3
import json, os, pathlib, signal, sys, time
''' + MEET + r'''
args = sys.argv[1:]
log = os.environ["FIXTURE_CODEX_LOG"]
with open(log, "a") as handle:
    handle.write(json.dumps([os.path.realpath(sys.argv[0]), os.path.realpath(os.getcwd()), *args]) + "\n")
if args[:1] == ["app-server"]:
    used = float(os.environ.get("FIXTURE_CODEX_USED", "27"))
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        message = json.loads(line)
        if "id" not in message:
            continue
        result = {}
        if message.get("method") == "account/rateLimits/read":
            result = {"rateLimits": {"limitId": "codex", "primary": {"usedPercent": used, "windowDurationMins": 10080},
                                     "secondary": None,
                                     "rateLimitReachedType": os.environ.get("FIXTURE_CODEX_REACHED") or None,
                                     "spendControlReached": False}}
        print(json.dumps({"id": message["id"], "result": result}), flush=True)
    sys.exit(0)
if args[:1] == ["exec"]:
    pathlib.Path(log + ".prompt").write_text(sys.stdin.read())
    hold = os.environ.get("FIXTURE_REVIEW_HOLD")
    while hold and not os.path.exists(hold):
        time.sleep(0.05)
    mode = os.environ.get("FIXTURE_CODEX_EXEC", "ok")
    out = pathlib.Path(args[args.index("-o") + 1])
    if mode in ("ok", "empty"):
        out.write_text("" if mode == "empty" else pathlib.Path(os.environ["FIXTURE_REVIEW_TEXT"]).read_text())
    meet("codex")
    if mode == "model-error":
        print("ERROR: The 'gpt-6.1-sol' model is not supported when using Codex with a ChatGPT account.", file=sys.stderr)
        sys.exit(1)
    if mode == "limit":
        print("ERROR: You've hit your usage limit. Try again later.", file=sys.stderr)
        sys.exit(1)
    if mode == "ok":
        print("codex exec transcript: rate limit and usage limit are words a lane diff may contain")
    sys.exit(0)
sys.exit(2)
'''

FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, os, pathlib, signal, sys
''' + MEET + r'''
log = os.environ["FIXTURE_CLAUDE_LOG"]
with open(log, "a") as handle:
    handle.write(json.dumps([os.path.realpath(os.getcwd()), *sys.argv[1:]]) + "\n")
pathlib.Path(log + ".prompt").write_text(sys.stdin.read())
meet("claude")
if os.environ.get("FIXTURE_CLAUDE_FAIL"):
    print("claude: API Error: overloaded", file=sys.stderr)
    sys.exit(1)
sys.stdout.write(pathlib.Path(os.environ.get("FIXTURE_CLAUDE_TEXT") or os.environ["FIXTURE_REVIEW_TEXT"]).read_text())
'''

FAKE_ANTIGRAVITY = r'''#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
log = os.environ["FIXTURE_ANTIGRAVITY_LOG"]
with open(log, "a") as handle:
    handle.write(json.dumps([os.path.realpath(sys.argv[0]), os.path.realpath(os.getcwd()), *args]) + "\n")
prompt = sys.stdin.read() if not sys.stdin.isatty() else ""
pathlib.Path(log + ".prompt").write_text(prompt)
if os.environ.get("FIXTURE_ANTIGRAVITY_FAIL"):
    print("antigravity: quota exceeded or backend error", file=sys.stderr)
    sys.exit(1)
if os.environ.get("FIXTURE_ANTIGRAVITY_EMPTY"):
    sys.exit(0)
text = pathlib.Path(os.environ.get("FIXTURE_ANTIGRAVITY_TEXT") or os.environ["FIXTURE_REVIEW_TEXT"]).read_text()
if "-o" in args:
    pathlib.Path(args[args.index("-o") + 1]).write_text(text)
else:
    sys.stdout.write(text)
'''

# A stand-in for Mastack's `bin/role` (fathers#150), installed beside a models file by LaneCase.with_role: it logs
# its argv and the MASTACK_ variables and answers from the review role of the models file MASTACK_MODELS names, its
# choices in order (when.not_family, when.limit), with FIXTURE_CODEX_USED and FIXTURE_CODEX_REACHED as the weekly
# Codex limit; it never reads a real limit. FIXTURE_ROLE_EFFORT overrides its choice's effort; FIXTURE_ROLE_EXIT=2
# fails it as an unreadable table does. The shared cases also run Mastack's own bin/role (test_review_panel.REAL_ROLE, when present).
FAKE_ROLE = r'''#!/usr/bin/env python3
import json, os, sys, tomllib
args = sys.argv[1:]
with open(os.environ["FIXTURE_ROLE_LOG"], "a") as handle:
    handle.write(json.dumps({"argv": args, "models": os.environ.get("MASTACK_MODELS"),
                             "codex": os.environ.get("MASTACK_CODEX")}) + "\n")
if os.environ.get("FIXTURE_ROLE_EXIT"):
    print("role: cannot read the table", file=sys.stderr)
    sys.exit(int(os.environ["FIXTURE_ROLE_EXIT"]))
table = tomllib.loads(open(os.environ["MASTACK_MODELS"]).read())
family = args[args.index("--implementer-family") + 1] if "--implementer-family" in args else None
left = int(100 - float(os.environ.get("FIXTURE_CODEX_USED", "27")))
reached = os.environ.get("FIXTURE_CODEX_REACHED") or None
unknown = "implementer family not given (pass --implementer-family)"
skipped, quotas = [], {}
for choice in table["roles"]["review"]["choices"]:
    name = choice["model"]
    model = table["models"][name]
    when = choice.get("when") or {}
    reason = None
    if when.get("not_family") and family is None:
        reason = unknown
    elif when.get("not_family") and model.get("family") == family:
        reason = f"same family as the implementer ({family})"
    else:
        for quota, need in (when.get("limit") or {}).items():
            quotas[quota] = {"left": left, "reached": reached, "error": None, "resets_at": None}
            if left < need:
                reason = f"{quota} {left}% left, needs {need:g}%"
            elif reached:
                reason = f"{quota} {left}% left, but Codex reports {reached}"
    if reason:
        skipped.append({"model": name, "reason": f"{name}: {reason}"})
        continue
    caution = (unknown + ": choices that need another family were skipped, so a note about the family may not apply"
               if any(s["reason"].endswith(unknown) for s in skipped) else None)
    print(json.dumps({"role": "review", "model": name, "id": model["id"], "family": model.get("family"),
                      "cli": model["cli"], "effort": os.environ.get("FIXTURE_ROLE_EFFORT") or choice.get("effort", "medium"),
                      "argv": {}, "note": choice.get("note"), "caution": caution, "skipped": skipped, "quotas": quotas}))
    sys.exit(0)
print(json.dumps({"role": "review", "error": "no choice qualifies", "skipped": skipped, "quotas": quotas}))
sys.exit(1)
'''

# The review role as Mastack's models.toml has it (2026-11-16).
MODELS_TABLE = textwrap.dedent("""\
    schema = 1
    [models.opus]
    id = "claude-opus-5-5"
    family = "anthropic"
    cli = "claude"
    [models.sol]
    id = "gpt-6.1-sol"
    family = "openai"
    cli = "codex"
    [roles.review]
    choices = [
      { model = "sol", effort = "medium", when = { not_family = true, limit = { codex_weekly = 15 } } },
      { model = "opus", effort = "medium", when = { not_family = true } },
      { model = "opus", effort = "medium", note = "same family as the implementer: fresh context only" },
    ]
    """)
NO_FINDINGS = "No findings.\n\n```json\n{\"findings\": []}\n```\n"
TWO_FINDINGS = textwrap.dedent("""\
    1. P1 lib/api.txt:2 baz is defined but nothing calls it.
    2. P2 lib/api.txt:1 foo lost its consumer test.

    ```json
    {"findings": [
      {"severity": "P1", "file": "lib/api.txt", "line": 2, "title": "baz is defined but nothing calls it",
       "detail": "calls/main.txt still calls foo only, so the new entry is dead."},
      {"severity": "P2", "file": "lib/api.txt", "line": 1, "title": "foo lost its consumer test",
       "detail": "No test covers the caller."}
    ]}
    ```
    """)
# Findings in a claimed file, in a file the lane's check widened to, outside the repository and without a file.
SPREAD_FINDINGS = textwrap.dedent("""\
    ```json
    {"findings": [
      {"severity": "P1", "file": "lib/api.txt", "line": 2, "title": "baz is defined but nothing calls it"},
      {"severity": "P2", "file": "./docs/guide.md", "line": 1, "title": "the guide still names foo"},
      {"severity": "P3", "file": "../elsewhere/notes.txt", "title": "a path outside the repository"},
      {"severity": "P3", "title": "no file at all"}
    ]}
    ```
    """)


def load_lane() -> types.SimpleNamespace:
    """lanefather's modules, for the tests that call or patch one in process."""
    from lanefather import checks, claims, config, host, plan, promote, review, state
    return types.SimpleNamespace(checks=checks, claims=claims, config=config, host=host, plan=plan, promote=promote,
                                 review=review, state=state)


@contextlib.contextmanager
def project(repo: pathlib.Path) -> Iterator[Any]:
    """The fixture's config as the run's, for a test that calls lanefather in process."""
    from lanefather import config
    config.use(config.load(repo / ".lanefather.toml"))
    try:
        yield config.current()
    finally:
        config.use(None)


_TEMPLATE: list[pathlib.Path] = []


FAKE_PROCESS_SCAN = f'''"""Test stand-in for tools/process_scan.py (fathers#209).

Returns an empty process table by default so unit tests do not run host-wide lsof + ps on every
worktree candidate. Set FIXTURE_REAL_PROCESS_SCAN=1 (LaneCase.use_real_process_scan) to delegate
to the frozen real scanner in tests/fixtures/process_scan.py.
"""
import importlib.machinery
import importlib.util
import os

_REAL_PATH = {str(FIXTURES / "process_scan.py")!r}
_REAL = None


def _real():
    global _REAL
    if _REAL is None:
        loader = importlib.machinery.SourceFileLoader("lanefather_fixture_real_process_scan", _REAL_PATH)
        spec = importlib.util.spec_from_loader(loader.name, loader)
        assert spec is not None
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        _REAL = module
    return _REAL


ScanError = _real().ScanError


def table(deadline=None):
    if os.environ.get("FIXTURE_REAL_PROCESS_SCAN") == "1":
        return _real().table(deadline)
    return {{}}


def in_worktree(root, processes):
    return _real().in_worktree(root, processes)


def inside(path, root):
    return _real().inside(path, root)


def sort_for_removal(root, processes=None):
    if processes is None and os.environ.get("FIXTURE_REAL_PROCESS_SCAN") != "1":
        return [], []
    return _real().sort_for_removal(root, processes)


def stop(items, root=None):
    if not items:
        return []
    return _real().stop(items, root)
'''


def template_repository() -> pathlib.Path:
    """The fixture repository with its base commit, built once per test process; each test copies it (fathers#95)."""
    if _TEMPLATE:
        return _TEMPLATE[0]
    repo = pathlib.Path(tempfile.mkdtemp(prefix="lane-template-")) / "straylight"
    atexit.register(shutil.rmtree, repo.parent, True)
    repo.mkdir()
    environment = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, env=environment)

    git("init", "-q", "-b", "main")
    git("config", "user.name", "Lane Test")
    git("config", "user.email", "lane@example.com")
    for name, text in FILES.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    (repo / "verify.sh").write_text(FAKE_VERIFY)
    (repo / "verify.sh").chmod(0o755)
    # Fast stand-in for tools/process_scan.py (fathers#209): skips host-wide lsof + ps unless a test opts in
    # with LaneCase.use_real_process_scan() (FIXTURE_REAL_PROCESS_SCAN=1), which delegates to fixtures/process_scan.py.
    (repo / "tools" / "process_scan.py").write_text(FAKE_PROCESS_SCAN)
    # The real stamp tooling, which verify.py runs after a green full gate.
    for name in ("tools/verified-tree.sh", "tools/verified-tree-ledger.py"):
        shutil.copy2(FIXTURES / pathlib.Path(name).name, repo / name)
    git("add", ".")
    git("commit", "-qm", "base")
    _TEMPLATE.append(repo)
    return repo


class InProcessTimeout(Exception):
    """A command run in process outlived its timeout, as subprocess.run(timeout=) would raise."""


def run_in_process(argv: list[str], cwd: Any, env: dict[str, str], timeout: float = 120) -> tuple[int, str]:
    """`python -m lanefather ARGV` in this process (fathers#95): the same environment, working directory and
    file descriptors 1 and 2, so the output of the commands lanefather starts is captured as a process's would be.
    Returns (exit status, stdout followed by stderr). The run's configuration, modules and sys.path do not outlive
    it, and a run past `timeout` seconds raises InProcessTimeout (a SIGALRM in the main thread)."""
    import signal

    from lanefather import claims, cli, config
    saved_env, saved_cwd, saved_path = dict(os.environ), os.getcwd(), list(sys.path)
    saved_modules = set(sys.modules)
    saved_config = config._current  # pyright: ignore[reportPrivateUsage]

    def expired(_signum: int, _frame: Any) -> None:
        raise InProcessTimeout(f"lanefather {' '.join(argv)} ran longer than {timeout:g} s")

    previous_alarm = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, timeout)
    outputs = [tempfile.TemporaryFile(), tempfile.TemporaryFile()]  # noqa: SIM115 - closed after the run
    saved_fds = [os.dup(1), os.dup(2)]
    sys.stdout.flush()
    sys.stderr.flush()
    code = 1
    try:
        os.environ.clear()
        os.environ.update(env)
        os.chdir(cwd)
        os.dup2(outputs[0].fileno(), 1)
        os.dup2(outputs[1].fileno(), 2)
        config.use(None)
        claims._head_kinds.clear()  # pyright: ignore[reportPrivateUsage]
        try:
            code = cli.main(argv)
        except SystemExit as error:
            if isinstance(error.code, str):
                print(error.code, file=sys.stderr)  # as the interpreter prints a string exit code
            code = error.code if isinstance(error.code, int) else 0 if error.code is None else 1
        except InProcessTimeout:
            raise
        except Exception:  # a crash, as the process would print it
            import traceback
            traceback.print_exc()
            code = 1
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_alarm)
        sys.stdout.flush()
        sys.stderr.flush()
        os.dup2(saved_fds[0], 1)
        os.dup2(saved_fds[1], 2)
        for fd in saved_fds:
            os.close(fd)
        os.chdir(saved_cwd)
        os.environ.clear()
        os.environ.update(saved_env)
        sys.path[:] = saved_path
        # A project hook's sibling modules come from that test's scratch repository: none stays cached for the next.
        for name in set(sys.modules) - saved_modules:
            origin = str(getattr(sys.modules[name], "__file__", None) or "")
            if origin and not origin.startswith((sys.prefix, sys.base_prefix, str(ROOT))):
                del sys.modules[name]
        config.use(saved_config)
    texts: list[str] = []
    for handle in outputs:
        handle.seek(0)
        texts.append(handle.read().decode(errors="replace"))
        handle.close()
    return code, texts[0] + texts[1]


class LaneCase(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="lane-tool-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "straylight"
        self.state = self.tmp / "state" / "lanes"
        self.cache = self.tmp / "cache"
        self.verify_log = self.tmp / "verify.log"
        self.verify_log.touch()
        self.comments = self.tmp / "comments.json"
        self.comments.write_text(json.dumps([{"body": "RED -> GREEN", "createdAt": "2999-01-01T00:00:00Z"}]))
        gh = self.tmp / "bin" / "gh"
        gh.parent.mkdir()
        gh.write_text(FAKE_GH)
        gh.chmod(0o755)
        # The reviewers (#570, fathers#201) are stand-ins first on PATH; no real model runs. An empty CODEX_HOME keeps the
        # ChatGPT app's CLI (CODEX_CLI_PATH in ~/.codex/config.toml) out of the tests.
        for name, text in (("codex", FAKE_CODEX), ("claude", FAKE_CLAUDE), ("agy", FAKE_ANTIGRAVITY),
                           ("antigravity", FAKE_ANTIGRAVITY)):
            stub = self.tmp / "bin" / name
            stub.write_text(text)
            stub.chmod(0o755)
        # The stand-in slotfather (#619): no test reaches the machine's queue; it reports 4 free slots.
        shutil.copy2(FIXTURES / "slotfather", self.tmp / "bin" / "slotfather")
        (self.tmp / "codex-home").mkdir()
        self.review_text = self.tmp / "review.md"
        self.review_text.write_text(NO_FINDINGS)
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("STRAYLIGHT_", "LANEFATHER_", "SLOTFATHER_", "GIT_", "FIXTURE_",
                                         "ANTIGRAVITY_", "AGY_", "T3_", "CLAUDE_", "CODEX_"))
                    and k not in ("CODEX_CLI_PATH", "PYTHONPATH", "ANTHROPIC_MODEL", "AGENT_TRANSCRIPT_PATH",
                                  "THOUGHT_LEVEL")
                    and not k.startswith("MASTACK_")}
        self.env.update(PATH=f"{self.tmp / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}", PYTHONPATH=str(ROOT),
                        CODEX_HOME=str(self.tmp / "codex-home"), FIXTURE_CODEX_LOG=str(self.tmp / "codex.log"),
                        FIXTURE_CLAUDE_LOG=str(self.tmp / "claude.log"),
                        FIXTURE_ANTIGRAVITY_LOG=str(self.tmp / "antigravity.log"),
                        FIXTURE_REVIEW_TEXT=str(self.review_text),
                        # land starts no background review here: the review tests start one, or run it in front.
                        LANEFATHER_REVIEW_START="manual",
                        # No models file and so no Mastack `role`: the real table and quota stay out (fathers#150).
                        LANEFATHER_MODELS=str(self.tmp / "no-mastack" / "models.toml"))
        self.addCleanup(self.stop_reviews)
        # 6 cores and 20 GB derive a cap of 3 lanes and a load15 limit of 9 (#572).
        self.env.update({name: str(self.tmp / "machine" / file) for name, file in MACHINE_FILES.items()})
        self.events = self.tmp / "machine" / "events.jsonl"
        self.env.update(LANEFATHER_STATE=str(self.state), LANEFATHER_DISK_FREE_GB="500",
                        LANEFATHER_LOAD15="0.1", LANEFATHER_CORES="6", LANEFATHER_MEMORY_GB="20",
                        LANEFATHER_GH=str(gh), LANEFATHER_BUILD_CACHE=str(self.cache),
                        FIXTURE_VERIFY_LOG=str(self.verify_log), FAKE_GH_COMMENTS=str(self.comments),
                        FAKE_GH_LOG=str(self.tmp / "gh.log"), SLOTFATHER_LOADAVG="0.1",
                        FAKE_SLOTFATHER_LOG=str(self.tmp / "slotfather.log"))
        # The base repository is built once per test process and copied for each test (fathers#95): a copy is a
        # fraction of git init, the fixture files and a commit.
        shutil.copytree(template_repository(), self.repo, symlinks=True)
        self.integration = self.repo / ".claude" / "worktrees" / "integrate-q"
        self.git(self.repo, "worktree", "add", "-q", "-b", "integrate/q", str(self.integration))

    # Helpers

    def use_real_process_scan(self) -> None:
        """Opt this test into the real lsof + ps process_scan fixture (fathers#209)."""
        self.env["FIXTURE_REAL_PROCESS_SCAN"] = "1"

    def git(self, cwd: Any, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()

    def write(self, base: Any, name: str, text: str) -> pathlib.Path:
        path = pathlib.Path(base) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    # A class that needs the command as its own process (signals, locks held across calls, the launcher) sets this;
    # LANEFATHER_TEST_PROCESS=1 runs every call as a process, the end-to-end form of the whole suite.
    process_cli = False

    def lane(self, *args: str, cwd: Any = None, ok: bool | None = True, timeout: float = 120,
             process: bool | None = None) -> str:
        if process is None:
            process = self.process_cli or bool(os.environ.get("LANEFATHER_TEST_PROCESS"))
        if process:
            proc = subprocess.run([sys.executable, "-m", "lanefather", *args], cwd=cwd or self.integration,
                                  env=self.env, capture_output=True, text=True, timeout=timeout)
            code, output = proc.returncode, proc.stdout + proc.stderr
        else:
            code, output = run_in_process(list(args), cwd or self.integration, self.env, timeout)
        if ok is not None:
            self.assertEqual(code == 0, ok, f"lane {' '.join(args)} exit {code}:\n{output}")
        return output

    def start(self, lane_id: str, *claims: str, extra: tuple[str, ...] = (), ok: bool = True) -> str:
        return self.lane("start", lane_id, "--ticket", "404", "--base", "integrate/q", "--claims", *claims, *extra, ok=ok)

    def created(self, lane_id: str, *claims: str) -> str:
        self.start(lane_id, *claims, extra=("--create",))
        return self.record(lane_id)["worktree"]

    def commit(self, worktree: Any, changes: dict[str, str], message: str = "lane change") -> str:
        for name, text in changes.items():
            self.write(worktree, name, text)
        self.git(worktree, "add", "-A")
        self.git(worktree, "commit", "-qm", message)
        return self.git(worktree, "rev-parse", "HEAD")

    def record(self, lane_id: str) -> dict[str, Any]:
        return json.loads((self.state / f"{lane_id}.json").read_text())

    def tip(self) -> str:
        return self.git(self.integration, "rev-parse", "HEAD")

    def integration_moves(self):
        """The integration branch moves on after the lane checks, with a change every check reads: what it brings
        in selects the full gate, so a merge runs its whole smoke as before #640 instead of the lane's receipt
        standing for it."""
        self.commit(self.integration, {"tooling/moved.txt": "moved on\n"}, "the integration moves on")

    def verify_runs(self):
        return self.verify_log.read_text().splitlines()

    def review(self, label: str) -> dict[str, Any]:
        return json.loads((self.state / f"{label}.review.json").read_text())

    def wait_review(self, label, timeout=60):
        """The review receipt once its background run finished."""
        path = self.state / f"{label}.review.json"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            receipt = json.loads(path.read_text()) if path.exists() else None
            if receipt and receipt["status"] != "running":
                return receipt
            time.sleep(0.05)
        self.fail(f"the review {label} did not finish within {timeout} s")

    def stop_reviews(self):
        """Background reviews end before the fixture goes: release a held one and wait for its process."""
        if self.env.get("FIXTURE_REVIEW_HOLD"):
            pathlib.Path(self.env["FIXTURE_REVIEW_HOLD"]).write_text("")
        for path in self.state.glob("*.review.json") if self.state.exists() else []:
            receipt = json.loads(path.read_text())
            pid, deadline = receipt.get("pid"), time.monotonic() + 30
            while receipt.get("status") == "running" and pid and time.monotonic() < deadline:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)

    def background_land(self, lane_id, hold, **env):
        """lanefather land in the background; its post-merge smoke holds until `hold` exists."""
        process = subprocess.Popen([sys.executable, "-m", "lanefather", "land", lane_id], cwd=self.integration,
                                   env={**self.env, "FIXTURE_HOLD": f"post-merge-{lane_id}:{hold}", **env},
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

        def cleanup_process() -> None:
            if process.poll() is None:
                process.kill()
                process.wait()
            if process.stdout is not None:
                process.stdout.close()

        self.addCleanup(cleanup_process)
        self.addCleanup(hold.touch)
        return process

    def wait_started(self, hold, process, timeout=60):
        """The checkout the held run started in, once it started."""
        started = pathlib.Path(str(hold) + ".started")
        deadline = time.monotonic() + timeout
        while not started.exists():
            # load-tolerant: every caller waits the 60 s default for a run that starts in about a second.
            self.assertLess(time.monotonic(), deadline, f"{hold.name}: the held run did not start within {timeout} s")
            if process.poll() is not None:
                self.fail(f"exited before its held run: {process.stdout.read()}")
            time.sleep(0.05)
        return started.read_text()

    def with_role(self, table: str = "", real: pathlib.Path | None = None) -> pathlib.Path:
        """A Mastack checkout in scratch: a models file (the review role of models.toml unless `table` is given)
        and `bin/role` beside it, the stand-in or a link to `real` (Mastack's own, which then reads the stand-in
        codex and keeps its state in scratch); returns the stand-in's log."""
        root = self.tmp / ("mastack-real" if real else "mastack")
        (root / "bin").mkdir(parents=True, exist_ok=True)
        (root / "models.toml").write_text(table or MODELS_TABLE)
        role = root / "bin" / "role"
        role.unlink(missing_ok=True)
        if real:
            role.symlink_to(real)
            (self.tmp / "mastack-config").mkdir(exist_ok=True)
            self.env.update(MASTACK_HOOK_STATE=str(self.tmp / "mastack-state"),
                            MASTACK_CONFIG=str(self.tmp / "mastack-config"))
        else:
            role.write_text(FAKE_ROLE)
            role.chmod(0o755)
        log = self.tmp / "role.log"
        self.env.update(LANEFATHER_MODELS=str(root / "models.toml"), FIXTURE_ROLE_LOG=str(log))
        return log

    def codex_calls(self):
        log = self.tmp / "codex.log"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def claude_calls(self):
        log = self.tmp / "claude.log"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def antigravity_calls(self) -> list[list[str]]:
        log = self.tmp / "antigravity.log"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


