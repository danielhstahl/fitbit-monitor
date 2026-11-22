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
> `fm import`) are implemented, including the shipped `resting_trend` example
> plugin. `fm dev seed` still writes a deterministic sample corpus, so every
> command below works before you have connected an account - and that corpus is
> synthetic data about nobody, never real health data and never sent anywhere.
>
> Data source: the **Google Health API** (`https://health.googleapis.com/v4`), decided
> and dated in [ADR-0002](docs/adr/0002-health-api-source.md), with a Google Takeout
> archive as an equal first-class source (`fm import --source csv`). The legacy Fitbit
> Web API that the client code still targets was turned off on 2026-11-02 and returns no
> data; that ADR's section 10 is the port.

## Quickstart

```bash
./scripts/dev/bootstrap.sh && . .venv/bin/activate   # uv when present, else venv + pip

fm auth login                       # one-time browser consent; token stays local
fm import --dry-run                 # what would this fetch, and at what cost
fm import --since 2026-05-01 --until 2026-05-14    # pull two weeks
fm import history                   # what the last runs landed
fm import --source csv --path ~/Downloads/export.zip   # backfill from the web export

fm dev seed --days 90                 # deterministic synthetic sample data (not real data)
fm store stats                        # rows, coverage and bytes per dataset
fm store verify                       # re-hash the retained raw payloads (nonzero exit on trouble)
fm store backup                       # one consistent snapshot file you can copy anywhere
fm plugins list                       # what is installed, and what is broken
fm plugins show resting_trend         # one plugin's options and effective config
fm plugins run --all --since 30d      # compute metrics, deliver alerts
fm plugins run resting_trend --since 8w --json
fm alerts list --since 7d             # what has been flagged
fm plugins --api                      # the plugin API version this build serves
```

Write a plugin:

```bash
fm plugin new resting_vs_steps --dir ~/fm-plugins
fm plugins list --dir ~/fm-plugins
fm plugins run resting_vs_steps --since 30d --dir ~/fm-plugins
cd ~/fm-plugins/resting_vs_steps && python -m pytest -q
```

## Commands

| command | what it does |
|---|---|
| `fm auth login [--scopes S] [--manual CODE] [--fixtures DIR]` | OAuth2 PKCE login in the browser; token stored `0600`, refreshed on use |
| `fm auth status [--json]` / `fm auth logout` | what is granted, and getting rid of it |
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
| `fm plugin new NAME [--dir DIR] [--force]` | scaffold a plugin that already passes the contract tests |
| `fm alerts list [--since D] [--severity S] [--plugin P] [--json]` | read the stored alert history |
| `fm dev seed [--days N] [--end DATE] [--seed N] [--anomalies N] [--no-gaps] [--fresh]` | write deterministic synthetic sample data, with labelled abnormal episodes |
| `fm dev info [--json]` | what is in the store right now |

Exit codes, the same for every command:

| code | meaning |
|---|---|
| 0 | success - including "ran and found nothing" and "no data for this range" |
| 1 | a plugin errored, misconfigured itself, or timed out; or an import landed nothing |
| 2 | bad arguments or unreadable config |
| 4 | no local store to read - create one with `fm dev seed` |

`fm import` adds three of its own, because "it ran" is not a useful answer for a
job that costs thousands of rate-limited requests: **3** partial (some datasets
landed, some did not), **5** landed with warnings (rows quarantined), **130**
interrupted with the completed chunks stored. See
[docs/data-import.md](docs/data-import.md).

`fm plugins run` is deliberately not a daemon: there is no `--watch` or
`--interval`. Run it from cron or a timer and get the same dedup and cooldown.

## Plugins in one paragraph

A plugin subclasses `MonitorPlugin`, gets a read-only `ctx` (store, its own config
section, the window, a clock, a logger, `metric()` and `alert()`), and returns. It
is discovered from the `fitbit_monitor.plugins` entry-point group or from a
configured directory of `.py` files, runs on its own thread with a wall-clock
budget, and cannot write to the database even by reaching for the raw connection -
SQLite refuses it. Alerts are stored, deduplicated against a cooldown, printed,
appended to JSONL, and optionally POSTed to one HTTPS webhook.

* [docs/plugins.md](docs/plugins.md) - authoring guide, API reference, and the
  built-in plugin explained.
* [docs/storage.md](docs/storage.md) - the store: schema, migrations, the read API,
  the UTC convention, verify/backup/export, and retention.
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

## Development

Install once, then one entrypoint per action. `scripts/dev/*` is what a contributor
types; `scripts/ci/*` is the gate underneath it, which the workflows call with
`--no-install` so a laptop and a runner fail the same way (ADR-0001 §3, §12).

```bash
./scripts/dev/bootstrap.sh      # create .venv (uv when present, else venv + pip)
./scripts/dev/lint.sh           # mypy + ruff + byte-compile + workflow/shell parse
./scripts/dev/test.sh           # unittest + pytest + the coverage floor
./scripts/dev/bench.sh          # plugin system vs benchmarks/baseline.json
./scripts/dev/check.sh          # all three - what CI runs after every commit
./scripts/dev/docs.sh           # regenerate the API reference block in docs/plugins.md
python scripts/dev/record_fixtures.py --check   # recorded API fixtures are current
```

The gates underneath take the same arguments and are safe to call directly:

```bash
./scripts/ci/lint.sh --no-install         # mypy, ruff, byte-compile, YAML, shell
./scripts/ci/coverage.sh --no-install     # unittest + pytest + coverage floor
./scripts/ci/benchmarks.sh --no-install   # compare against benchmarks/baseline.json
./scripts/ci/post_commit.sh --quick       # skip the benchmark
```

**Without uv**, `pip` installs the same toolchain - `uv sync --extra dev` is the fast
path and `uv.lock` is the pin, but nothing in the repository depends on uv being
installed:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
```

Python 3.11+ (`.python-version` is the interpreter uv uses for the dev environment;
CI runs 3.11, 3.12, 3.13 and 3.14). Runtime dependencies: none - stdlib only, so a
plugin author needs nothing but Python. Dev dependencies (`.[dev]`): pytest,
pytest-cov, coverage, mypy, ruff, pyyaml.

The CI gates live in `scripts/ci/*.sh` and the workflows only call them
([`.github/workflows/ci.yml`](.github/workflows/ci.yml),
[`.github/workflows/benchmark.yml`](.github/workflows/benchmark.yml)), so a laptop
and a runner fail the same way.

Things the tests hold to, worth knowing before you change them:

* `docs/plugins.md` contains a **generated** API reference
  (`python scripts/docs/api_reference.py --write`), the worked example in it is
  executed, and every `fm …` line in the docs is parsed by the real parser
  (`tests/test_docs.py`).
* `PluginContractTestCase` in `fitbit_monitor.plugins.testing` is the same suite
  third-party plugin authors inherit; the shipped plugin runs against it in CI.
* `benchmarks/baseline.json` is updated on purpose, with
  `./scripts/ci/benchmarks.sh --update-baseline`.

### Layout

```
src/fitbit_monitor/
  store/       migrations/*.sql + runner, writer, read-only reader (coverage, gaps, raw access), raw retention,
               dataset/unit catalog, downsample, seed data, maintenance (stats, verify, export, backup, prune),
               alert history, watermarks
  api/         http (transport, fixtures), auth (PKCE, token store), client (rate limits, ETags), catalog (endpoint shapes)
  ingest/      importer (plan + run), pipeline (dedupe, quarantine), csv_source (export/Takeout), report, runlog
  plugins/     contract.py models.py loader.py registry.py runner.py sinks.py scaffold.py testing.py
    builtin/   resting_trend.py - the reference plugin
  cli/         common.py auth_cmd.py config_cmd.py db_cmd.py store_cmd.py import_cmd.py plugins_cmd.py alerts_cmd.py dev_cmd.py
  logsafe.py   health-value redaction for everything that reaches a log handler
docs/          storage.md, plugins.md, data-import.md, data-policy.md, adr/
scripts/       dev/{bootstrap,lint,test,bench,check,docs}.sh + record_fixtures.py - what you type;
               ci/{lint,coverage,benchmarks,post_commit}.sh - the gates; bench/, docs/
tests/         stdlib unittest - runs under pytest or `python -m unittest discover`
tooling        configured in pyproject.toml, pinned in uv.lock - no config files to drift
```
