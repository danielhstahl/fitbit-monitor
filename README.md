# fitbit-monitor

Your Fitbit data, on your machine, watched by plugins you can read.

The program has three layers, each usable on its own: a **local store** (raw
payloads plus normalized series in SQLite, with a read API), a **plugin system**
(the contract, discovery, the runner, and the alert sinks), and the **`fm` command
line** that ties them together. `fm` is the short alias of `fitbit-monitor`; both are
installed by this package and run the same code. Nothing in it sends your health data
anywhere except the alert webhook you configure - see
[docs/data-policy.md](docs/data-policy.md).

> Status: the store core, the plugin system and the import path (`fm auth`,
> `fm import`) are implemented, including the two shipped example plugins -
> `resting_trend` (metrics) and `hr-anomaly` (alerts). `fm dev seed` still writes a
> deterministic sample corpus, so every command below works before you have connected an
> account - and that corpus is synthetic data about nobody, never real health data and
> never sent anywhere.
>
> One command - `fm report` - is declared in the parser and in the table below but does
> not compute anything yet: it prints what will replace the commands it is meant to fold
> together and exits **72** (not implemented), so a script can tell "not built" from
> "failed". `fm doctor` used to be in that group; it now runs the whole diagnosis.
>
> Data source: the **Google Health API** (`https://health.googleapis.com/v4`), decided
> and dated in [ADR-0002](docs/adr/0002-health-api-source.md), with a Google Takeout
> archive as an equal first-class source (`fm import --source csv`). The legacy Fitbit
> Web API that the client code still targets was turned off on 2026-11-02 and returns no
> data; that ADR's section 10 is the port.

## Quickstart

```bash
# install + activate: bootstrap.sh runs `uv sync --extra dev` when uv is on PATH,
# `python -m venv` + `pip install -e ".[dev]"` when it is not (ADR-0001 §2, §3)
./scripts/dev/bootstrap.sh && . .venv/bin/activate
./scripts/dev/check.sh --quick      # every gate CI runs (lint, tests + coverage), minus the benchmark

fm auth login                       # one-time browser consent; token stays local
fm import --dry-run                 # what would this fetch, and at what cost
fm import --since 2026-05-01 --until 2026-05-14    # pull two weeks
fm import history                   # what the last runs landed
fm api probe                        # why an import lands nothing: scopes, datasets, rate limit
fm import --source csv --path ~/Downloads/export.zip   # backfill from the web export

fm dev seed --days 90                 # deterministic synthetic sample data (not real data)
fm store stats                        # rows, coverage and bytes per dataset
fm store verify                       # re-hash the retained raw payloads (nonzero exit on trouble)
fm store backup                       # one consistent snapshot file you can copy anywhere
fm plugins list                       # what is installed, and what is broken
fm plugins show resting_trend         # one plugin's options and effective config
fm plugins run --all --since 30d      # compute metrics, deliver alerts
fm plugins run resting_trend --since 8w --json
fm plugins run --name hr-anomaly --since 90d    # alert on what is unusual for you
fm alerts list --since 7d             # what has been flagged
fm plugins --api                      # the plugin API version this build serves
fm doctor                             # what is wrong with this install (read-only)
```

Write a plugin:

```bash
fm plugin new resting_vs_steps --dir ~/fm-plugins
cd ~/fm-plugins/resting_vs_steps     # the project root is itself a drop-in directory
fm plugins list --dir .
fm plugins run resting_vs_steps --since 30d --dir .
python -m pytest -q                  # the scaffolded contract suite, same one CI runs
```

## Commands

| command | what it does |
|---|---|
| `fm auth login [--scopes S] [--manual CODE] [--fixtures DIR]` | OAuth2 PKCE login in the browser; token stored `0600`, refreshed on use |
| `fm auth status [--json]` / `fm auth logout` | what is granted, and getting rid of it |
| `fm api probe [--dataset N] [--since D] [--until D] [--no-history] [--max-requests N] [--json]` | ask the API why an import lands nothing: granted vs wanted scopes, per-dataset data/403/404, rate-limit state, earliest date available |
| `fm import [--since D] [--until D] [--dataset N] [--dry-run] [--json]` | pull a date range of every dataset the account allows |
| `fm import --full-history` / `--resume` / `--retry` / `--max-requests N` | start at signup, finish an interrupted run, retry failures, bound a cron pull |
| `fm import --source csv --path EXPORT [--file F] [--glob G]` | backfill from a Fitbit export or Google Takeout |
| `fm import report [--run-id R]` / `history` / `watermarks` | what a run did, from its report and the `dataset_run` table |
| `fm config show [--json]` / `fm config init` | the effective configuration, and a commented starting point |
| `fm db init` / `fm db migrate` / `fm db status [--json]` | create the store, apply numbered SQL migrations, inspect the schema |
| `fm store stats [--json]` | per-dataset row counts, date coverage, raw vs normalized bytes, duplicate and quarantine counts |
| `fm store verify [--quick] [--sample N] [--json]` | re-hash every retained payload and re-check integrity; nonzero exit on trouble |
| `fm store export --format jsonl\|csv [--what series\|raw] [--dataset D] [--output F]` | portable copy of the series or of the original payloads |
| `fm store backup [--output F]` | one consistent snapshot via SQLite's online backup API |
| `fm store prune --days N [--what …] [--execute]` | retention - a **dry run** unless `--execute` is given |
| `fm plugins list [--dir DIR] [--json]` | discovered plugins with version, source, datasets, and any load error |
| `fm plugins show NAME [--json]` | one plugin's metadata, options, effective config, unused config keys |
| `fm plugins validate [NAME ...]` | load and configure plugins without running them |
| `fm plugins run [NAME ...] [--all] [--since D] [--until D] [--set NAME.KEY=VAL] [--json] [--timeout S] [--no-deliver]` | run plugins over a range and deliver their alerts |
| `fm plugin new NAME [--dir DIR] [--force] [--dry-run]` | scaffold a plugin that already passes the contract tests |
| `fm alerts list [--since D] [--severity S] [--plugin P] [--json]` | read the stored alert history |
| `fm doctor [--quick] [--check NAME] [--probe] [--json]` | one-pass install diagnosis: config, data dir, token, store, plugins, clock, last import (and, with `--probe`, what the API allows); nonzero exit only on a hard failure |
| `fm report [--since D] [--until D] [--json]` | **planned, not implemented** (exit 72): one document over a range; today: `fm import report`, `fm store stats`, `fm alerts list` |
| `fm dev seed [--days N] [--end DATE] [--seed N] [--anomalies N] [--no-gaps] [--fresh]` | write deterministic synthetic sample data, with labelled abnormal episodes |
| `fm dev info [--json]` | what is in the store right now |

Exit codes, the same for every command:

| code | meaning |
|---|---|
| 0 | success - including "ran and found nothing" and "no data for this range" |
| 1 | a plugin errored, misconfigured itself, or timed out; or an import landed nothing |
| 2 | bad arguments or unreadable config |
| 4 | no local store to read - create one with `fm dev seed` |
| 72 | the command exists in this build but is not implemented (`fm report`) |

`fm import` adds three of its own, because "it ran" is not a useful answer for a
job that costs thousands of rate-limited requests: **3** partial (some datasets
landed, some did not), **5** landed with warnings (rows quarantined), **130**
interrupted with the completed chunks stored. See
[docs/data-import.md](docs/data-import.md).

`fm plugins run` is deliberately not a daemon: there is no `--watch` or
`--interval`. Run it from cron or a timer and get the same dedup and cooldown.

## Reading the log, and `fm doctor`

`fm` is a cron job as often as it is a terminal command, so when an import lands nothing
or an alert never arrives, the log is the record. How loud it is comes from the flags or
the environment:

| what you say | level |
|---|---|
| (nothing) | `warning` |
| `-v` | `info` |
| `-vv` | `debug` |
| `-q` | `error` |
| `--log-level NAME`, or `FM_LOG_LEVEL` | any of `debug`, `info`, `warning`, `error`, `critical` |

A flag beats the environment, and `-q` beats `-v`. A text line carries a UTC timestamp
and the logger (module) name, which is what makes a plugin's own lines attributable:

```text
2026-11-23T18:04:11Z INFO    fitbit_monitor.api.client: access token refreshed; valid until 2026-11-24T02:04:11
2026-11-23T18:04:12Z WARNING fitbit_monitor.ingest.importer: skipping 2 dataset(s) with no granted scope: spo2, body
```

`--log-format json` (or `FM_LOG_FORMAT=json`) writes one JSON object per line - `ts`,
`level`, `logger`, `module`, `message`, plus any `extra` the emitter attached and a
redacted `exception` - for `jq` and for a log shipper:

```bash
fm --log-format json -v import --since 7d | jq -r 'select(.level=="warning") | .message'
```

**Two kinds of thing never appear in a log line, in either format**: health readings and
credentials. Both are stripped by filters on the handler, not by each call site -
`fitbit_monitor.logsafe` rewrites a reading to `resting_hr=<redacted>` and
`fitbit_monitor.logsecrets` rewrites a credential to `[REDACTED]`. Secrets are masked
two ways: by **shape** (`access_token=…`, `"client_secret": "…"`, `{'Authorization':
'Bearer …'}`, `?access_token=…` in a URL) and by **identity** - a `TokenSet` registers
its own access and refresh tokens, and the client secret from `config.toml` registers
itself, the moment they are read, so anything read from the token store is masked
however it happens to be formatted. Tracebacks go through the same filter. The tests are
`tests/test_log_redaction.py` (and `tests/test_data_policy.py` for readings); the policy
is [docs/data-policy.md](docs/data-policy.md).

When something looks off, run `fm doctor` first. It is read-only (it writes one probe
file into the data dir to test writability, then deletes it) and prints a verdict per
check:

```text
[ok]      config       path=/home/u/.config/fitbit-monitor/config.toml exists=yes parsed=yes
[ok]      data_dir     path=/home/u/.local/share/fitbit-monitor exists=yes writable=yes
[warn]    token        path=/home/u/.local/share/fitbit-monitor/credentials.json present=no expired=-
       hint: run `fm auth login` (an import needs a token)
[skipped] api          probed=no requests=0
       hint: `fm doctor --probe` (or `fm api probe`) asks the API what this account allows
[warn]    store        path=/home/u/.local/share/fitbit-monitor/store.sqlite3 exists=no schema_version=-
       hint: `fm db init` creates an empty store; `fm dev seed --days 90` fills one with sample data
[ok]      plugins      dirs="-" dirs_missing="-" count=2 usable=2
[ok]      clock        utc=2026-11-23T18:04:11+00:00 local_offset=+00:00 future_rows=0
doctor: 4 ok, 2 warn, 0 fail, 2 skipped - no hard failures
```

Verdicts are `ok`, `warn`, `fail` and `skipped`, and **only a `fail` makes the exit code
nonzero**: a machine that has never logged in is a warning, a store file that is not a
store is a failure. The checks are `version`, `config`, `data_dir`, `token`, `api`,
`store`, `plugins`, `clock` and `imports`; each line is `key=value` so it greps, and
`--json` is the same report for a script:

```bash
fm doctor --json | jq '.checks[] | select(.status != "ok")'
```

`--quick` skips the two store scans (SQLite `integrity_check` and the
future-timestamp count), and `--check NAME` runs one check.

The `api` check is the only one that talks to the network, so it is `skipped` unless
`--probe` asks for it - `fm doctor` has to stay free to run in a cron job or on a plane.
With `--probe` it spends about one request per dataset and reports how many answered with
data, which did not, which scopes are missing and how many requests are left. For the
full diagnosis - earliest date of available data per dataset included - use
`fm api probe`, which is the same questions with room to answer
([docs/data-import.md](docs/data-import.md)).

## Plugins in one paragraph

A plugin subclasses `MonitorPlugin`, gets a read-only `ctx` (store, its own config
section, the window, a clock, a logger, `metric()` and `alert()`), and returns. It
is discovered from the `fitbit_monitor.plugins` entry-point group or from a
configured directory of `.py` files, runs on its own thread with a wall-clock
budget, and cannot write to the database even by reaching for the raw connection -
SQLite refuses it. Alerts are stored, deduplicated against a cooldown, printed,
appended to JSONL, and optionally POSTed to one HTTPS webhook.

* [docs/plugins.md](docs/plugins.md) - authoring guide, API reference, and the two
  built-in plugins explained.
* [docs/storage.md](docs/storage.md) - the store: schema, migrations, the read API,
  the UTC convention, verify/backup/export, and retention.
* [docs/detection.md](docs/detection.md) - the anomaly engine: personal baselines, the
  sustained-rate and irregularity rules, what a confidence is made of, and what the
  shipped thresholds were measured to cost on the labelled corpus.
* [docs/data-import.md](docs/data-import.md) - auth, planning a pull, resuming an
  interrupted one, CSV backfill, and what each dataset means.
* [docs/adr/0001-implementation-stack.md](docs/adr/0001-implementation-stack.md) - the
  stack, once: Python floor, uv, `src/` layout, SQLite, the test/coverage/benchmark
  tools, and why the CLI is argparse.
* [docs/adr/0002-health-api-source.md](docs/adr/0002-health-api-source.md) - where the
  data comes from: the **Google Health API** (`health.googleapis.com/v4`) and a Takeout
  archive as two first-class sources - and the record that the legacy Fitbit Web API the
  client still calls was turned off on 2026-11-02, so `fm auth login` / `fm import`
  against `api.fitbit.com` cannot work until the port in that ADR's section 10 lands.
* [docs/adr/0003-storage-model.md](docs/adr/0003-storage-model.md) - why raw payloads
  are the record, one `series` table plus typed views, and what makes an overlapping
  import idempotent.
* [docs/adr/0004-plugin-contract.md](docs/adr/0004-plugin-contract.md) - the
  normative contract: the exact interface and the fields of every emitted model,
  discovery and precedence, config, versioning, isolation, and the suite a
  third-party plugin must pass.
* [docs/adr/0005-ad-hoc-import.md](docs/adr/0005-ad-hoc-import.md) - why chunks,
  watermarks and per-dataset failure are the shape of the importer.
* [docs/data-policy.md](docs/data-policy.md) - what leaves the machine (the API you
  ask, and one webhook).
* [docs/ci.md](docs/ci.md) - the two gates, what each one stops, the artifacts they
  write, the one explicit command that refreshes the benchmark baseline, and running
  both jobs after every commit through act-mcp (`scripts/ci/post_commit.sh`).

## Development

Install once, then one entrypoint per action. `scripts/dev/*` is what a contributor
types; `scripts/ci/*` is the gate underneath it, which the workflows call with
`--no-install` so a laptop and a runner fail the same way (ADR-0001 §3, §12).

```bash
./scripts/dev/bootstrap.sh      # create .venv (uv when present, else venv + pip)
./scripts/dev/lint.sh           # mypy + ruff + byte-compile + workflow/shell parse
./scripts/dev/typecheck.sh      # mypy alone - the fastest "did I break a type" answer
./scripts/dev/test.sh           # unittest + pytest + the coverage floor
./scripts/dev/coverage.sh       # the same gate, named the way CI names it
./scripts/dev/bench.sh          # plugin system vs benchmarks/baseline.json
./scripts/dev/check.sh          # all three - what CI runs after every commit
./scripts/dev/docs.sh           # regenerate the API reference block in docs/plugins.md
python scripts/dev/record_fixtures.py --check   # recorded API fixtures are current
python scripts/dev/record_accuracy_fixtures.py --check  # labelled accuracy fixture is current
fm dev accuracy --wearers 1     # the detector's confusion matrix and accuracy floors, printed
```

The gates underneath take the same arguments and are safe to call directly - and they
are what the workflows call, which is why no build or test command line exists
anywhere else in this repository (not in `.github/workflows/*.yml`, not in a Makefile):

```bash
./scripts/ci/lint.sh --no-install         # typecheck.sh, then ruff, byte-compile, YAML, shell
./scripts/ci/typecheck.sh --no-install    # mypy alone
./scripts/ci/coverage.sh --no-install     # unittest + pytest, then the floors; prints `coverage: NN.N%`
./scripts/ci/bench.sh --no-install     # compare against benchmarks/baseline.json
./scripts/ci/post_commit.sh --quick       # skip the benchmark
./scripts/git-hooks/install.sh            # opt-in: run those gates after every commit (AGENTS.md rule)
./scripts/git-hooks/install.sh --remove   # take the hook back out
```

`scripts/ci/post_commit.sh` is the one command for "the gates, now": it resolves the
repository root itself and runs the lint, coverage and benchmark jobs through the best
runner it finds - `act-mcp` (configured in `.pi/mcp.json`), then `act`, then the gate
scripts directly - and ends with one PASS/FAIL block carrying the coverage percentage
and the benchmark verdict. See [docs/ci.md](docs/ci.md#after-every-commit-act-mcp-then-act-then-the-scripts)
for the exact act-mcp invocation and which workflows are act-safe. One wrinkle that lives
there rather than here: act-mcp binds its workspace when it is started and its tool then
has no workspace argument, so the client asks the server which tree it owns and only
sends a job when that tree is this checkout - otherwise the gates run directly.

**Without uv**, `pip` installs the same toolchain - `uv sync --extra dev` is the fast
path and `uv.lock` is the pin, but nothing in the repository depends on uv being
installed:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
```

Python 3.11+ (`.python-version` is the band: the interpreter uv uses for the dev
environment and the one `actions/setup-python` installs in CI, so no version number is
written in the workflow files; the coverage job runs one leg per entry, 3.11 through
3.14). Runtime dependencies: none - stdlib only, so a plugin author needs nothing but
Python. Dev dependencies (`.[dev]`): pytest, pytest-cov, coverage, mypy, ruff, pyyaml.

The CI gates live in `scripts/ci/*.sh` and the workflows only call them
([`.github/workflows/ci.yml`](.github/workflows/ci.yml),
[`.github/workflows/bench.yml`](.github/workflows/bench.yml)), so a laptop
and a runner fail the same way. The coverage gate ends by printing one machine-readable
line - `coverage: 92.1%` - and a line per package, and fails below any floor committed
in `pyproject.toml`: `[tool.coverage.report] fail_under` for the whole tree and
`[tool.fitbit_monitor.coverage]` for each directory under `src/fitbit_monitor`. It
writes `coverage.xml` and `coverage.json`, both uploaded as artifacts. The benchmark
gate compares the five cases in `scripts/bench/bench_plugin_system.py` with
`benchmarks/baseline.json`, fails a case more than 15% past it, and writes
`bench-results.json` (case, mean, stddev, iterations, environment) plus
`bench-summary.md` - the markdown the pull request is commented with, so no arithmetic
lives in the YAML. Both jobs need no secrets and no network beyond the package index, so
they run under `act`
(`act -j coverage --artifact-server-path /tmp/artifacts`) exactly as they run on GitHub;
without a container runtime, `./scripts/ci/post_commit.sh` runs the same gates. The
full story, including how to re-record the baseline without letting a run re-baseline
its own regression away, is [docs/ci.md](docs/ci.md).

Things the tests hold to, worth knowing before you change them:

* `docs/plugins.md` contains a **generated** API reference
  (`python scripts/docs/api_reference.py --write`), the worked example in it is
  executed, and every `fm …` line in the docs is parsed by the real parser
  (`tests/test_docs.py`).
* `PluginContractTestCase` in `fitbit_monitor.plugins.testing` is the same suite
  third-party plugin authors inherit; the shipped plugin runs against it in CI.
* `benchmarks/baseline.json` is updated on purpose, with
  `./scripts/dev/bench.sh --update-baseline` - CI never passes that flag, so a green
  benchmark job cannot mean "the baseline moved".
* `.github/workflows/ci.yml` is checked, not trusted: it must call
  `scripts/ci/coverage.sh` and nothing else, take its interpreter from
  `.python-version`, cache dependencies, upload both coverage reports and carry no
  secrets, and its matrix must match the committed band
  (`tests/test_ci_coverage_gate.py`, which also tests the floor arithmetic against
  synthetic reports). `bench.yml` is held to the same standard, and so is the benchmark
  gate under it - the tolerance, the regression verdict, both reports and the
  script-generated comment (`tests/test_ci_bench_gate.py`).

### Layout

```
src/fitbit_monitor/
  store/       migrations/*.sql + runner, writer, read-only reader (coverage, gaps, raw access), raw retention,
               dataset/unit catalog, downsample, seed data, maintenance (stats, verify, export, backup, prune),
               alert history, watermarks
  api/         http (transport, fixtures), auth (PKCE, token store), client (rate limits, ETags), catalog (endpoint shapes)
  ingest/      importer (plan + run), pipeline (dedupe, quarantine), csv_source (export/Takeout), report, runlog
  plugins/     contract.py models.py loader.py registry.py runner.py sinks.py scaffold.py testing.py
    builtin/   resting_trend.py - the reference metric plugin
               hr_anomaly.py       - the reference alert plugin, over anomaly/
  cli/         common.py auth_cmd.py config_cmd.py db_cmd.py store_cmd.py import_cmd.py plugins_cmd.py alerts_cmd.py report_cmd.py doctor_cmd.py dev_cmd.py
  logsecrets.py credential redaction - by shape and by value - for every log record
  logsafe.py   health-value redaction for everything that reaches a log handler
  logfmt.py    log levels (flags + FM_LOG_LEVEL), the text and JSON formatters, handler setup
docs/          storage.md, plugins.md, data-import.md, data-policy.md, ci.md, adr/
scripts/       _python.sh - the one interpreter resolver; no script guesses at python3
               dev/{bootstrap,lint,typecheck,test,coverage,bench,check,docs}.sh + record_fixtures.py - what you type;
               ci/{lint,typecheck,coverage,bench,post_commit}.sh + _coverage_report.py, _act_mcp.py,
               _gate_summary.py - the gates; bench/, docs/
               git-hooks/{post-commit,install}.sh - the opt-in after-commit runner, installed by hand
tests/         stdlib unittest - runs under pytest or `python -m unittest discover`
tooling        configured in pyproject.toml, pinned in uv.lock - no config files to drift
```
