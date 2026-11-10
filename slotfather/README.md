# slotfather

One queue for expensive work on a shared machine. Builds, tests and agents request capacity before starting,
so independent terminals, projects and automation share one budget.

## Why use it?

A test runner, a compiler and several coding agents can each decide to use every core. Started together, they
compete for memory, make the machine unresponsive and turn benchmark results into noise. Per-project worker
settings do not coordinate those separate processes.

Slotfather gives them one place to wait and a visible answer to “what is using the machine?”:

- Wrap a build or test command and it waits until its requested slots are available.
- Give an interactive check priority over unattended batch work.
- Reserve all cooperating slots for a benchmark instead of timing it beside another wrapped build.
- Inspect the dashboard, process/lease ancestry and history when work seems stuck.
- Reuse a parent's lease through nested scripts instead of reserving the same work twice.

Use it on a development workstation or shared runner when expensive commands overlap. It runs any command and
does not require a specific build system, repository layout or AI agent.

## How the tools fit

Slotfather coordinates **execution capacity**. [Lanefather](https://github.com/maxvyshniakov/lanefather) coordinates
repository tasks, path claims, integration and cleanup. [Repo-Kit](https://github.com/maxvyshniakov/repo-kit) runs
configured checks and package releases. Slotfather works on its own; the others can use its leases for heavy phases.
A long-lived lane is not a long-lived machine reservation.

The budget is cooperative: a slot is **not a CPU or memory limit**, and unwrapped commands can still compete with
leased work. `-n all` is exclusive among participating Slotfather jobs. The queue is local to one machine and shared
state location; it is not a distributed scheduler or a sandbox.

## Dashboard

![Slotfather dashboard: navigating running and waiting requests](assets/dashboard.webp)

## Install

Requires Git for source installation and Python 3.9+ on macOS or Linux. The installed commands use only the Python
standard library; Linux needs procps-compatible `ps`.

```sh
git clone https://github.com/maxvyshniakov/slotfather.git
cd slotfather
python3 install.py
```

Put `~/.local/bin` on your `PATH`. Re-running the installer updates the commands
without cancelling running work. It pins them to the installer’s interpreter,
so a project’s virtual environment does not change their Python.

[Installation, migration and service accounts](docs/installation.md)

## Run work

```sh
slotfather run -n 1 --label tests --priority interactive -- python3 -m unittest discover
slotfather run -n 2 -- make build
slotfather run -n all -- ./benchmark.sh
```

A request waits until it can start, holds its slots while the command runs, then
returns the command’s exit status. Nested calls reuse the ancestor’s lease.
Use `--wait 60` to give up after a minute with exit code 75.
`--priority` is `interactive`, `normal` (the default) or `batch`; `--ticket ID`
optionally associates the request with an issue.

A slot is a scheduling budget, **not a CPU or memory limit**. By default, capacity
is the lower of one slot per three cores and one per 4 GiB of memory, with a
minimum of one. On Linux, a limited cgroup can determine the memory budget.

```sh
slotfather slots           # show capacity and how it was chosen
slotfather slots 6         # save a shared capacity of six; applies at the next grant
slotfather slots reset     # return to automatic sizing
```

Requests normally follow arrival order. Short work can pass a request that does
not fit, subject to the queue’s backfill rules. Low memory or swap can delay new
work; an otherwise idle machine can still start its first request.

[Capacity and configuration](docs/configuration.md) ·
[Ordering, priorities and cooperative yielding](docs/scheduling.md)

## Watch the queue

Run `slotfather` in a terminal to open the live dashboard.

| Widget | Shows |
| --- | --- |
| SLOTS | Occupied and free capacity; colours identify running requests. |
| REQUESTS | Running and waiting requests. Select either to see details. |
| REQUEST DETAILS | Command, status, resource use and the busiest processes. |
| MACHINE | Admission status, load, memory, swap and occupied-slot history. |
| EVENTS | Recent grants, releases and abandoned requests. |

Use **↑/↓** or click to select, the mouse wheel to scroll a table, **m** to
freeze updates for native text selection, **?** for help and **q** to quit.
Press **s** for Settings: a read-only status of the queue above shared capacity,
queue policy and the local refresh interval. A change is saved at once and applies
at the next grant, also while work runs; Esc closes.

```sh
slotfather status                  # a plain-text queue summary
slotfather status --json --pid 4242 # find a process’s lease
slotfather procs --json --pid 4242  # read-only process, lease and session ancestry
slotfather dashboard --once         # a dashboard snapshot for scripts
```

[Reading the dashboard](docs/dashboard.md) ·
[JSON status and metadata](docs/api.md)

## Inspect usage and leftover processes

```sh
slotfather history 20
slotfather stats --since 24h --by kind
slotfather available
slotfather advise --max 4
slotfather sweep
```

`available` reports queue space before memory checks. Dashboard admission also
applies memory policy. `advise` recommends a worker budget; it does not reserve slots. `sweep` is a dry run by
default. Only `sweep --live` stops eligible leftover processes.

[Statistics](docs/statistics.md) · [Sweep](docs/sweep.md) ·
[Linux and cgroups](docs/linux.md)

[Domain glossary](GLOSSARY.md) · [Naming and compatibility](docs/terminology.md)

## Architecture

The commands are thin entry points into `src/slotfather/`: queue files, leases,
scheduling, OS inspection and views have separate modules. State upgrades preserve
active leases; CLI and query-version changes follow explicit migration contracts.
[Modules, state versions and safe changes](docs/architecture.md).
`slotfather version` identifies the installed source; [remote installation](docs/installation.md)
uses the same verified source snapshot.

## Development

```sh
slotfather run -n 1 -- python3 -m unittest discover -s tests -v
```

[Lint and strict type checks](docs/agents/checks.md) use a baseline that can only shrink.

The animation uses the real dashboard and input handling, a historical queue,
and separately recorded workload metrics. It is an illustrative demo, not a
recording of those historical processes. [Regenerate media](docs/media.md).

[Verification](docs/testing.md) · [Scheduler experiments](docs/scheduler-experiments.md)

Lifetime workflow registrations can use the separate, stateless
[`lane-admission --json` contract](docs/lane-admission.md); it does not reserve
slots or reinterpret the execution worker budget. Receiver migration and caller
activation remain explicit delivery steps.

## License

[MIT](LICENSE). The initial extraction retains its [source provenance](PROVENANCE.json).
