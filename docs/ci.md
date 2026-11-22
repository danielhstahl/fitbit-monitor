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
