# fitbit-monitor

Your Fitbit data, on your machine, watched by plugins you can read.

The program has three layers, each usable on its own: a **local store** (raw
payloads plus normalized series in SQLite, with a read API), a **plugin system**
(the contract, discovery, the runner, and the alert sinks), and the **`fm` command
line** that ties them together. Nothing in it sends your health data anywhere except
the alert webhook you configure - see [docs/data-policy.md](docs/data-policy.md).

> Status: the store core, the plugin system and the import path (`fm auth`,
> `fm import`) are implemented, including the shipped `resting_trend` example
> plugin. `fm dev seed` still writes a deterministic sample corpus, so every
> command below works before you have connected an account.

## Quickstart

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"

fm auth login                       # one-time browser consent; token stays local
fm import --dry-run                 # what would this fetch, and at what cost
fm import --since 2026-05-01 --until 2026-05-14    # pull two weeks
fm import history                   # what the last runs landed
fm import --source csv --path ~/Downloads/export.zip   # backfill from the web export

fm dev seed --days 45                 # deterministic sample data
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
| `fm plugins list [--dir DIR] [--json]` | discovered plugins with version, source, datasets, and any load error |
| `fm plugins show NAME [--json]` | one plugin's metadata, options, effective config, unused config keys |
| `fm plugins validate [NAME ...]` | load and configure plugins without running them |
| `fm plugins run [NAME ...] [--all] [--since D] [--until D] [--set NAME.KEY=VAL] [--json] [--timeout S] [--no-deliver]` | run plugins over a range and deliver their alerts |
| `fm plugin new NAME [--dir DIR] [--force]` | scaffold a plugin that already passes the contract tests |
| `fm alerts list [--since D] [--severity S] [--plugin P] [--json]` | read the stored alert history |
| `fm dev seed [--days N] [--end DATE] [--seed N] [--fresh]` | write deterministic sample data |
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
* [docs/data-import.md](docs/data-import.md) - auth, planning a pull, resuming an
  interrupted one, CSV backfill, and what each dataset means.
* [docs/adr/0004-plugin-contract.md](docs/adr/0004-plugin-contract.md) - the
  normative contract: interfaces, discovery, config, versioning, isolation.
* [docs/adr/0005-ad-hoc-import.md](docs/adr/0005-ad-hoc-import.md) - why chunks,
  watermarks and per-dataset failure are the shape of the importer.
* [docs/data-policy.md](docs/data-policy.md) - what leaves the machine (the API you
  ask, and one webhook).

## Development

```bash
./scripts/ci/lint.sh          # mypy + byte-compile + shell syntax
./scripts/ci/coverage.sh      # unittest + pytest + coverage floor
./scripts/ci/benchmarks.sh    # plugin system vs benchmarks/baseline.json
./scripts/ci/post_commit.sh   # all three, the way CI runs them after a commit
```

Python 3.11+. Runtime dependencies: none - stdlib only, so a plugin author needs
nothing but Python. Dev dependencies (`.[dev]`): pytest, coverage, mypy.

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
  store/       schema, writer, read-only reader, coverage, downsample, seed data, alert history, watermarks
  api/         http (transport, fixtures), auth (PKCE, token store), client (rate limits, ETags), catalog (endpoint shapes)
  ingest/      importer (plan + run), pipeline (dedupe, quarantine), csv_source (export/Takeout), report, runlog
  plugins/     contract.py models.py loader.py registry.py runner.py sinks.py scaffold.py testing.py
    builtin/   resting_trend.py - the reference plugin
  cli/         common.py auth_cmd.py config_cmd.py import_cmd.py plugins_cmd.py alerts_cmd.py dev_cmd.py
docs/          plugins.md, data-import.md, data-policy.md, adr/
scripts/       ci/{lint,coverage,benchmarks,post_commit}.sh, bench/, dev/, docs/
tests/         stdlib unittest - runs under pytest or `python -m unittest discover`
```
