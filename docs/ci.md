# CI: the two gates, and what stops a push

Two things are promised to stay green: the tests and their **coverage**, and the
**benchmarks** against a committed baseline. Both promises are kept by a shell script
under `scripts/ci/`, and both GitHub workflows do nothing but call one of those scripts
([`../.github/workflows/ci.yml`](../.github/workflows/ci.yml) and
[`../.github/workflows/bench.yml`](../.github/workflows/bench.yml)). No command line,
version number, floor or percentage is written in a workflow file - that is the rule in
[ADR-0001 §12](adr/0001-implementation-stack.md) and this repository's `AGENTS.md`, and
it is tested, not hoped for.

| gate | script (the gate) | workflow | stops a push when | artifacts |
| --- | --- | --- | --- | --- |
| coverage | `scripts/ci/coverage.sh` | `ci.yml` | total or a per-package floor in `pyproject.toml` is not met | `coverage.xml`, `coverage.json` |
| benchmarks | `scripts/ci/bench.sh` | `bench.yml` | a tracked case is more than 15% worse than `benchmarks/baseline.json` | `bench-results.json`, `bench-summary.md` |

```bash
./scripts/ci/coverage.sh --no-install    # the coverage gate, exactly as CI runs it
./scripts/ci/bench.sh --no-install       # the benchmark gate, exactly as CI runs it
./scripts/ci/post_commit.sh              # both, plus the type check - what runs after a commit
./scripts/ci/post_commit.sh --quick      # the same, skipping the benchmark
```

Neither job needs a secret, a network beyond the package index, or anything but the
interpreter in `.python-version`, so `act` runs the workflow files themselves rather
than a paraphrase of them:

```bash
act -j coverage --artifact-server-path /tmp/artifacts
act -j benchmark --artifact-server-path /tmp/artifacts
```

Under `act` the pull-request comment step is skipped (there is no pull request to
comment on); everything else, including both artifact uploads, runs. `bench.yml` is
matched job-for-job by `scripts/ci/post_commit.sh`, so a machine with no container
runtime still runs the identical gates.

## After every commit: act-mcp, then act, then the scripts

`AGENTS.md` asks for one thing beyond the two workflows: when an MCP action runner is
configured, the coverage and benchmark jobs run after **every commit**, not only on a
push. This workspace has one - the `act-mcp` entry in `.pi/mcp.json` - and the thing
that honours that rule is `scripts/ci/post_commit.sh`. It resolves the repository root
itself and passes it to the server as the workspace, because the failure it exists to
fix was a path failure and not a workflow failure:

```
act-mcp: workspace /Users/.../fitbit-monitor has no `.github` directory, so there are
no GitHub Actions workflows to run
  fix: point --workspace at the repository root that contains .github/workflows/*.yml
```

The root is resolved in this order, and the first candidate holding `.github/workflows`
wins: `--workspace`, then the working directory and each of its parents, then
`git rev-parse --show-toplevel`, then the tree the script file itself lives in. The
caller's tree is tried before the script's own so a test or a wrapper running this file
against another checkout gets the checkout it asked for; an explicit `--workspace` that
is not a repository root is refused with a `fix:` line naming the path, because an
ignored workspace is the very bug being fixed.

Both workflows are **act-safe** - `ci.yml` (jobs `lint`, `coverage`) and `bench.yml`
(job `benchmark`) need no secret, no network beyond the package index, and nothing but
the interpreter in `.python-version`, so a container runner can run them verbatim. Three
runners are tried in this order, and the first that works is reported in the output:

| runner | what runs | the call behind it |
| --- | --- | --- |
| `act-mcp` | the job, in the runner the MCP server manages | `scripts/ci/_act_mcp.py run --workspace "$PWD" --workflow ci.yml --job coverage` |
| `act` | the workflow file itself, locally | `act -j coverage --artifact-server-path /tmp/artifacts`, `act -j benchmark --artifact-server-path /tmp/artifacts` |
| `direct` | the two gate scripts, in this checkout | `./scripts/ci/coverage.sh --no-install`, then `./scripts/ci/bench.sh --no-install` |

The exact invocations, from a clean clone:

```bash
./scripts/ci/post_commit.sh                # every job, best runner available - exits 0 when all are green
./scripts/ci/post_commit.sh --quick        # same, skipping the benchmark (what the commit hook runs)
./scripts/ci/post_commit.sh --via act-mcp  # insist on the MCP runner, still falling back unless --no-fallback
./scripts/ci/post_commit.sh --dry-run      # print the resolved workspace, the runner and the calls, run nothing
./scripts/ci/_act_mcp.py discover          # the server, the tool it offers and that tool's input schema
./scripts/ci/_act_mcp.py plan --workspace "$PWD" --workflow ci.yml --job coverage
./scripts/git-hooks/install.sh             # opt-in: run post_commit.sh from git after each commit
./scripts/git-hooks/install.sh --remove    # take it back out
```

### When the server owns its own workspace

act-mcp binds its workspace when it is started, so its `run_workflow` tool has no
workspace argument at all - the path is a property of the running server, not of the
call:

```
inputSchema properties: category, commit, idempotency_key, inputs, log_tail_bytes, secrets, task, timeout_secs, wait, workflow
```

So the client asks the server which tree it owns - the `instructions` of the server's own
`initialize` answer name it - and only sends a run when that is the checkout being
gated. `discover` prints the answer, and its exit status is what `post_commit.sh` uses to
choose the runner:

```bash
$ ./scripts/ci/_act_mcp.py discover
server:    act-mcp (act-mcp) at http://host.docker.internal:60562/mcp
tool:      run_workflow
schema:    category, commit, idempotency_key, inputs, log_tail_bytes, secrets, task, timeout_secs, wait, workflow
error:     the act-mcp tool 'run_workflow' has no workspace argument, and the server runs /Users/you/fitbit-monitor rather than /workspace
  fix: start act-mcp against this checkout (act-mcp --workspace /workspace), or pass --server-bound ...
```

A build of somebody else's checkout is not this repository's gate, so a path the client
cannot match is refused until `--server-bound` says out loud that the two names mean one
tree - which is exactly the case when a container sees this checkout as a bind mount under
another path. What makes that trust safe is the commit: the note prints it, and act-mcp
will not run at all unless its own checkout is at that commit and clean. The same opt-in is
available per machine as `ACT_MCP_SERVER_BOUND=1`, which is how a container turned on the
MCP runner without editing a script:

One trap when a workflow really is run by act, locally or through act-mcp: act keeps its
own cache of every third-party action, and a failure to refresh it is *non-terminating*, so
a stale cached copy can fail a job that passes on GitHub. Old copies of
`actions/setup-python` read this repository's four-line `.python-version` as a single
version and answer `the version '3.14 3.13 3.12 3.11' ... was not found`; clear
`~/.cache/act` (or give the container network access to fetch the action) before believing
that failure. The current action resolves one line per interpreter, which is what both
jobs rely on: the band is installed, and uv is then told which of the installed interpreters
a given leg runs under.

When the server does own this checkout, the remaining arguments it insists on are derived
and printed before they are sent:

```bash
$ ./scripts/ci/_act_mcp.py run --workspace "$PWD" --workflow ci.yml --job coverage
call:      tools/call run_workflow {"category": "coverage", "commit": "58b3df1", "log_tail_bytes": 65536, "task": "coverage", "wait": true, "workflow": "ci.yml"}
```

* `task` is act-mcp's name for the job id - exactly what `act -j` takes.
* `category` comes from the read-only `list_tasks` tool (the coverage job's hint is
  `coverage`, the benchmark job's is `benchmark`), because the category is what decides
  whether act-mcp keeps any evidence from the run at all.
* `commit` is this checkout's `HEAD`, because act-mcp resolves commits locally and never
  fetches: that is what ties the run to the code being gated. A server that cannot run
  that commit in its own tree - a dirty worktree answers `isolation_failed` - is an
  unusable runner, not a red gate.
* `wait` and `log_tail_bytes` are what turn one call into a verdict with a readable log
  behind it, so the summary block can still print a coverage percentage when the reports
  stayed inside the container.

Three answers are then kept apart, because collapsing them would be the interesting bug:
`0` green, `1` red - including a payload that says `"status": "failed"` while the tool
call itself succeeded, which `isError` alone would have called green - and `2` for "the
runner would not start the job" (`invalid_arguments`, `workspace_busy`,
`isolation_failed`), which is no verdict at all and sends the gate back to the scripts.

`scripts/ci/_act_mcp.py` is the MCP client and `scripts/ci/_gate_summary.py` is the
block the run ends with, and both are usable on their own. The client reads the argument
*names* out of the tool's own `inputSchema` rather than guessing them, prints the
workspace before it sends anything, and exits `2` when act-mcp cannot be used - which is
what sends the run back to the gate scripts instead of letting a job that never ran be
reported as a pass. The summary block takes the reports and logs the runner left behind
and prints one line per gate plus one overall `result` line, and its exit status becomes
the run's:

```
workspace    /abs/fitbit-monitor  (resolved from the working directory)
runner       direct (scripts/ci/coverage.sh, scripts/ci/bench.sh - the command lines the two jobs are)
gate         status detail
lint         PASS
coverage     PASS   92.0% covered (floor 85.0%)
benchmarks   PASS   no case regressed more than 15%
result       PASS   3 passed, 0 failed
```

Because the reports a container writes stay in the container, a number that was not
measured here is taken from the job's own log and marked as such (`[job log]`); when
neither the report nor the log carries one, the row says
`no coverage number reported` rather than implying a percentage. The hook is opt-in and
installed by `scripts/git-hooks/install.sh`, which only ever symlinks
`scripts/git-hooks/post-commit` into `.git/hooks/` and refuses to clobber a hook it did
not install; a red gate there reports and does not undo the commit (`POST_COMMIT_STRICT=1`
makes it exit nonzero as well, `POST_COMMIT_SKIP=1` skips once).

One guard is worth knowing about when a test reaches for these scripts:
`scripts/ci/coverage.sh` runs this repository's test suite, and the suite runs
`scripts/ci/post_commit.sh`, so a nested run that resolves back to the repository whose
suite is already running would call the gate that calls the suite forever. coverage.sh
exports `FITBIT_CI_SUITE` as the root whose suite is live and `post_commit.sh` refuses
exactly that root, with a `fix:` line; a test points the runner at a stub repository
instead (`tests/test_ci_post_commit.py` does).

## What the benchmark gate measures

`scripts/bench/bench_plugin_system.py` builds a synthetic 90-day corpus in a temporary
directory - the same corpus the store tests use - and times the five things that get
slow silently:

| case | what it is | reading |
| --- | --- | --- |
| `seed_corpus` | writing the corpus, i.e. the ingest path | rows/sec (higher is better) |
| `scan_intraday` | the read API iterating every stored point | points/sec (higher) |
| `downsample_to_days` | bucketing every intraday point by day | points/sec (higher) |
| `plugin_resting_trend` | the shipped plugin run over the whole corpus | ms (lower) |
| `discover_plugins` | registry discovery, validation, error isolation | ms (lower) |

Each case is sampled several times (nine rounds; a sub-millisecond case takes extra
samples per round so "best" means something - the multiplier is fixed per case so a
best sample is always compared with a best sample over the same number of tries).
**The best sample is what gates.** It is the statistic the baseline was recorded with,
it is the one that survives a busy shared runner, and 15% is the tolerance on it: big
enough to shrug off a noisy neighbour, small enough that an accidental `O(n²)`, a
per-point `SELECT` or a re-parse in a hot loop is caught. `tests/test_tooling.py` pins
the committed baseline's shape; `tests/test_ci_bench_gate.py` pins the gate and the
workflow.

The reports do not stop at the number that gated. Every case also carries `mean`,
`stddev` and `iterations`, which is how you tell a real 12% regression from a run where
the machine was busy - and the environment the numbers came from (interpreter, OS, CPU
count and model, memory, git commit, runner) is recorded alongside them, because a perf
number without its environment is a rumour.

## Refreshing the baseline - on purpose, in a diff

The baseline is committed JSON at `benchmarks/baseline.json`: one entry per case with
its unit and direction, the sample statistics from the recording, and the environment
that produced it. It is written by exactly one command, and by nobody automatically:

```bash
./scripts/dev/bench.sh --update-baseline
git diff benchmarks/baseline.json      # read it: this is the review
git add benchmarks/baseline.json
```

`--update-baseline` is the only thing that writes the file, and **CI never passes it**.
A run can therefore never re-baseline its own regression away: a green job means the
numbers moved toward the committed ones, not that the committed ones moved. The
workflow's call is fixed at

```
uv run --locked --extra dev ./scripts/ci/bench.sh --no-install \
    --json-out bench-results.json --markdown-out bench-summary.md \
    --artifact-name bench-results-json --markdown-artifact-name bench-summary-md
```

which is exactly `./scripts/ci/bench.sh --no-install` with the report names spelled out.

Re-record the baseline in the commit that *earns* it, for one of these reasons:

* a deliberate, understood cost ("the import now validates every row, it costs 18%");
* the interpreter moved (the baseline names the interpreter that recorded it, and the
  report prints a note when a run is compared across CPython versions);
* the machine moved - the committed numbers came from a developer machine, so the first
  run on a different class of hardware is worth re-recording once, on purpose, rather
  than after three flaky gates;
* a case was added or its measurement changed, which is also the only time the *set* of
  cases in the baseline should change.

A re-record is a two-line reviewable diff in the commit that needs it, never a
drive-by. If a gate is red and the cost is not understood, the answer is the diff that
caused it, not the baseline.

Two more refusals, both so a green means something: with no baseline file to compare
against the gate exits `2` rather than passing an unmeasured run, and a run with a
corpus size other than the baseline's (`--days 30`) reports numbers but declines to gate
them - throughput on a different corpus is a different quantity. Both refusals are
printed, and both are recorded in `bench-results.json`.

## The two reports

`bench-results.json` - the artifact a regression is investigated from (trimmed; every
key is real, `samples` is every reading taken):

```json
{
  "schema": 1,
  "generated_utc": "2026-11-22T17:54:55+00:00",
  "harness": "scripts/bench/bench_plugin_system.py",
  "config":  { "days": 90, "repeats": 9, "statistic": "best of repeats",
               "tolerance_percent": 15.0, "baseline": "benchmarks/baseline.json",
               "baseline_days": 90 },
  "environment": { "python": "3.14.5", "machine": "x86_64", "cpu_count": 8,
                   "memory_gib": 16.0, "git": { "short": "1a2b3c4", "dirty": false } },
  "notes": [],
  "cases": [
    { "case": "scan_intraday", "what": "read API over every stored point",
      "unit": "points/sec", "direction": "higher", "iterations": 9,
      "mean": 571993.5, "stddev": 8756.8, "best": 578638.2, "worst": 550564.8,
      "gate_value": 578638.2, "baseline": 578638.2, "delta_percent": 0.0,
      "regression_percent": 0.0, "tracked": true, "status": "ok", "samples": [ ... ] }
  ],
  "regressions": [],
  "gate": { "gated": true, "passed": true, "tracked_cases": 5,
            "regressed_cases": [], "untracked_cases": [], "tolerance_percent": 15.0 }
}
```

`bench-summary.md` - the same numbers as a table, with a `PASS`/`FAIL` line on top. It
is generated by the same script, which is what lets `bench.yml` post it verbatim as the
pull-request comment (`peter-evans/create-or-update-comment`, tagged `bench-results` so
each branch carries one comment that is updated in place), append it to the job summary
(`$GITHUB_STEP_SUMMARY`), and upload it as an artifact without containing a line of
formatting or arithmetic itself.

A case that the baseline does not name is reported with `status: "untracked"` and is
never gated: measuring a case is not the same as a reviewer agreeing to a threshold for
it, so that agreement arrives with the baseline diff that names it.

## Changing a threshold

The coverage floors live in `pyproject.toml` (`[tool.coverage.report] fail_under` and
`[tool.fitbit_monitor.coverage]`) and the benchmark tolerance is `DEFAULT_TOLERANCE` in
the harness; the benchmark corpus is `--days`, the sample count `--repeats`. The
scripts take `--floor`, `--package-floor`, `--tolerance` and friends so a ratchet can be
*tried* locally, but the number that stops a push is always the number in git. Raising a
floor or tightening a tolerance belongs in the commit that has earned it, in the same
diff that shows it holds.
