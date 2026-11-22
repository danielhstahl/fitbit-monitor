# fitbit-monitor

Your Fitbit data, on your machine, watched by plugins you can read.

The program has three layers, each usable on its own: a **local store** (raw
payloads plus normalized series in SQLite, with a read API), a **plugin system**
(the contract, discovery, the runner, and the alert sinks), and the **`fm` command
line** that ties them together. Nothing in it sends your health data anywhere except
the alert webhook you configure - see [docs/data-policy.md](docs/data-policy.md).

> Status: the store core and the plugin system are implemented, including the shipped
> `resting_trend` example plugin. API import (`fm auth`, `fm import`) is the next
> epic; until it lands, `fm dev seed` writes a deterministic sample corpus so every
> command below works today.

## Quickstart

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"

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
| 1 | a plugin errored, misconfigured itself, or timed out |
| 2 | bad arguments or unreadable config |
| 4 | no local store to read - create one with `fm dev seed` |

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
* [docs/adr/0004-plugin-contract.md](docs/adr/0004-plugin-contract.md) - the
  normative contract: interfaces, discovery, config, versioning, isolation.
* [docs/data-policy.md](docs/data-policy.md) - what leaves the machine (one webhook).

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
  store/       schema, writer, read-only reader, coverage, downsample, seed data, alert history
  plugins/     contract.py models.py loader.py registry.py runner.py sinks.py scaffold.py testing.py
    builtin/   resting_trend.py - the reference plugin
  cli/         common.py plugins_cmd.py alerts_cmd.py dev_cmd.py
docs/          plugins.md, data-policy.md, adr/
scripts/       ci/{lint,coverage,benchmarks,post_commit}.sh, bench/, docs/
tests/         stdlib unittest - runs under pytest or `python -m unittest discover`
```
