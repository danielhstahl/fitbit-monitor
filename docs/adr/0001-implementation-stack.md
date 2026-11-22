# ADR-0001: the implementation stack - Python, uv, SQLite, and the six commands you type

- **Status:** accepted
- **Date:** 2026-11-22
- **Implements:** `pyproject.toml`, `uv.lock`, `.python-version`,
  `scripts/dev/*` and `scripts/ci/*`, `.github/workflows/ci.yml`,
  `.github/workflows/benchmark.yml`; enforced by `tests/test_tooling.py`
- **Related:** ADR-0002 (data source - the HTTP client named in §2 is its transport),
  ADR-0003 (storage model - this ADR picks the engine, that one picks the schema),
  ADR-0004 (plugin contract - the main reason the runtime has no dependencies),
  ADR-0005 (import), `README.md` (Development)

## Context

This is a single-user tool that a person installs on their own laptop to watch their
own health data. That one sentence decides more than any preference about frameworks:

* **The install has to be trivial and it has to be repeatable.** There is no app store,
  no ops team, no container. The user runs a command in a shell on macOS. Every
  runtime dependency is a thing that can break that command, and a thing the user then
  has to understand when it does.
* **The data outlives the program.** A year from now this schema will have proved wrong
  about something, and in five years nobody remembers why. The storage engine has to be
  one the user can open with a tool that is already on their machine, and the code has
  to be readable by whoever inherits it - possibly a future version of the author.
* **Plugins are written by people who are not us** (ADR-0004). The dev loop they join
  has to be small enough to describe in one README section and fast enough to run on
  every save.
* **The repository is worked on by several hands at once**, so a decision that is only
  written down in someone's head gets re-litigated in the next pull request.

The ticket's recommendation (Python 3.12+, uv, `src/` layout, Typer, httpx, SQLite,
pytest + coverage + pytest-benchmark, ruff + mypy) is evaluated below and **adopted,
adapted, or explicitly rejected** on each axis. The measure applied throughout is not
"what is nice to write today" but "what does the person maintaining this in 2031 have
to install, understand, and trust". Each rejection is recorded so the next ticket can
spend its time on something else.

## Decision

| axis | decided | rejected, and why in one line |
|---|---|---|
| language | Python 3.11+ | 3.10 (no `tomllib`), 3.12 floor (buys nothing, costs contributors) |
| runtime deps | **none** - stdlib only | httpx, requests, pydantic: each one is the user's problem when it breaks |
| env + lockfile | uv, `uv.lock`, `.python-version` | poetry, pip-tools, bare `requirements.txt` |
| build backend | setuptools (`[tool.setuptools]`) | hatchling: one more tool for no gain |
| layout | `src/fitbit_monitor/`, `tests/` outside it | flat layout, tests-inside-package |
| CLI | stdlib `argparse` | Typer, Click |
| storage | stdlib `sqlite3`, one file | DuckDB, Parquet/pyarrow, sqlmodel/SQLAlchemy, JSON blobs |
| tests | pytest, cases written as `unittest.TestCase` | bare unittest, nox/tox |
| coverage | coverage.py, branch, `fail_under = 85` | `--cov` alone, diff-cover, mutation testing |
| benchmark | `scripts/bench` vs `benchmarks/baseline.json` | pytest-benchmark, asv |
| lint | ruff, tier-1 rule set committed | pyflakes (retired), flake8+isort+black, pylint |
| format | ruff format, **enforcement deferred** to a named commit | black, "no formatter" |
| types | mypy, config committed | pyright-only, pytype, "types are optional" |
| console scripts | `fitbit-monitor` **and** `fm` | `fitbit`, long name only |
| commit hooks | `scripts/ci/post_commit.sh` (git post-commit, act-mcp) | the pre-commit framework |

### 1. Language and runtime floor: Python 3.11+

Python, because the work is data wrangling plus HTTP plus a CLI, because the plugin
system asks strangers to write code against our API, and because every candidate
contributor on a macOS laptop already has it. Not Go (its plugin story without
shipping a matching toolchain is "shell out to a subprocess"), not Rust (the plugin
author is writing a twenty-line monitor, not a compiler).

The floor is **3.11**, and it is not a floor by accident: `config.py` parses TOML with
`tomllib`, and `datetime.UTC` is used throughout, both 3.11 additions.

`requires-python = ">=3.11"`, `.python-version` lists the band newest-first
(`3.14, 3.13, 3.12, 3.11`) so uv takes the newest interpreter already on the machine
instead of downloading one, and the CI matrix runs all four. The newest end matters as
much as the floor: the interpreter used during development here is 3.14, where
`sqlite3.version` has been removed and `sqlite3.sqlite_version` is all that is left -
exactly the kind of change a floor-only policy discovers in production.

**Rejected: a 3.12 floor** (the ticket's recommendation). Nothing in 12 800 lines of
`src/` uses a 3.12-only feature, and the two 3.12 conveniences worth naming - PEP 695
type parameters and `itertools.batched` - are worth exactly one import line each. A
floor is paid for by everyone who is stuck on the older thing: Debian 12's system
Python is 3.11, and a person with a Debian box and a Fitbit is a user we want. When a
release finally needs 3.12, the change is one commit: `requires-python`, the matrix,
`.python-version`, and the classifiers, plus `tests/test_tooling.py` which will fail
if any of the four is forgotten.

**Rejected: 3.10 or older.** No `tomllib` means a third-party TOML parser as the first
runtime dependency of all time, and no `datetime.UTC` means a `timezone.utc` spray.

### 2. The zero-dependency rule: the runtime installs nothing

`[project] dependencies = []`, and a test asserts it stays empty. `pip install
fitbit-monitor` installs one package and nothing else at runtime (build isolation may
fetch a throwaway copy of setuptools into a temp dir, and that is all it ever sees).
This is a feature with a name: *the package cannot break because of something it did
not write*, and the plugin author's environment stays one they already trust.

Two libraries from the recommendation are therefore rejected on this axis, not on
merit:

* **httpx** (and `requests`). All network traffic in this repository is one HTTPS host
  and a handful of GETs; `api/http.py` is a transport interface with two
  implementations - `UrllibTransport` for the real world and a replay transport for
  the tests (ADR-0005: *no network in the tests*). Rate limits, ETags, retries, gzip
  and pagination already live in `api/client.py`, on top of the interface, where a
  recorded-fixture test can reach them. httpx would be the only runtime dependency in
  the project, in exchange for HTTP/2 that Fitbit does not speak. The honest cost of
  `urllib` is macOS trust stores: python.org builds ship their own OpenSSL and read
  `SSL_CERT_FILE`, not the keychain. The mitigation is documented, not a dependency:
  run the `Install Certificates.command` that comes with a python.org installer, or
  `pip install certifi` and point `SSL_CERT_FILE` at `python -m certifi`. If that ever
  becomes a support burden, the amendment is one line - `certifi` becomes the first and
  last runtime dependency - not a re-stack.
* **pydantic** (and sqlmodel, which drags it in). The plugin models are frozen
  dataclasses with validators written by hand and tested directly
  (`tests/test_models.py`); validation is at the trust boundary, which is the store and
  the API response, not every internal value.

Anything that wants to change this owes an amendment to this ADR, not a pull request
that quietly adds a line to `dependencies`.

### 3. Package manager, lockfile, build backend

**uv** manages the development environment and owns the lockfile. `uv sync --extra dev`
goes from a clean clone to a working environment in one command; `uv run` is what CI
uses. Two files are committed and both are pins:

* `.python-version` - the interpreter band, newest first (§1).
* `uv.lock` - every dev tool with a version *and a hash*, resolved against
  `requires-python = ">=3.11"`, so one lock is portable across the whole matrix.

The policy that keeps a lockfile from rotting: **edit `pyproject.toml`, run `uv lock`,
commit both files together**; CI runs `uv run --locked`, which fails if the two
disagree. uv is a dev tool and nothing in `src/` may import it or assume it exists.

**Rejected: poetry.** A second lockfile format, its own venv lifecycle outside the
repository, and a `pyproject.toml` dialect that every other tool has to be told about.
Its one genuine advantage over pip - a lockfile - is what uv just gave us in the format
the rest of the ecosystem is converging on.

**Rejected: pip-tools.** One requirements file per platform-and-version combination,
then a second step to install it. That is uv's job with one file.

**Rejected: no lockfile, `pip install -e ".[dev]"` only.** This is what the repository
did before this ADR, and it is still supported (§12) because refusing pip is refusing
contributors - but "works on my machine" is not a CI contract. The pip path stays as
the documented fallback and the CI path is locked, so drift is caught on the machine
that is allowed to fail.

**Build backend: setuptools, unchanged.** A backend change is a big diff for no user
visible benefit: setuptools is in the stdlib-adjacent tier of "will still be there",
the existing `[tool.setuptools.packages.find]` and `package-data` globs already do the
one clever thing we need (shipping `store/migrations/*.sql`), and uv builds it without
complaint. **Rejected: hatchling** - the default in uv's own templates, and a fine
project, but "the template uses it" is not a reason to churn a working build.

### 4. Package layout

```
src/fitbit_monitor/     the package: store/ api/ ingest/ plugins/ cli/ + config, timeutil, logsafe
tests/                  stdlib unittest cases, run by pytest
scripts/dev/            what a contributor types (§12)
scripts/ci/             the gates the workflows call (§12)
scripts/bench/          the benchmark harness (§9)
scripts/docs/           the generator behind docs/plugins.md's API reference
benchmarks/baseline.json  the committed perf baseline
docs/adr/               this file and its neighbours
```

The `src/` layout is not decoration. With a flat layout, `pytest` run from a clone
imports the *source directory* rather than the *installed package*, which means
entry-point declaration bugs - the exact bugs that matter here, since plugin discovery
is an entry-point group (ADR-0004) and `fm` is a console script - are invisible until
they are shipped. From `src/`, an import that works is an import from the installed
artifact, because there is nothing else to import from.

**Rejected: flat layout** (the above). **Rejected: tests inside the package** - they
would ship to the user, and `PluginContractTestCase` (`plugins/testing.py`, deliberately
*in* the package) is the only test code that has a reason to be importable by users.
**Rejected: a `packages/` monorepo with the plugin SDK split out**: two distributions,
two version numbers, and a chicken-and-egg install loop, all to protect a contract that
a test and a plugin API version integer already protect.

### 5. CLI framework: `argparse`, and Typer stays out

`fm` is nine command groups and 25 leaf commands, spread over eight modules under
`cli/`, each registering itself on a shared `ArgumentParser` - and the whole surface is
stdlib.

Why not Typer (the recommendation), which is Click with type hints:

* Click is a **runtime dependency**, and §2 says the runtime has none. Typer would
  install click plus its own typing machinery into the user's environment so that
  `fm store stats` prints a prettier table.
* The documented exit-code contract (0/1/2/3/4/5/130, README) is *behaviour*, and
  Click's exception-to-exit-code mapping would need overriding in the places where it
  has opinions - usage errors happen to be 2, but "no store yet" is 4, and the
  interrupted-import code 130 must survive `SystemExit` unwinding. argparse exits with
  the code we return, because we return it.
* The docs are tested against the parser: `tests/test_docs.py` feeds every `fm …` line
  in the README and docs through the real parser, so a command documented before it
  exists fails the build. That gate works because an argparse parser is a plain object
  the test can build and feed. Click's decorator-built commands are testable too, but
  only through Click's own runner - the point is that the plain-object version is what
  made the check cheap, and the check is the reason the docs are trustworthy.

**Rejected: Click without Typer** - same dependency, less sugar. **Rejected:
`clap`-style generated help text or hand-rolled `sys.argv` parsing** - argparse already
gives us subcommands, `--set k=v` repeated options, auto-`--help`, and translated
usage errors for free.

The one real argparse weakness - nested subcommand help is terse - is answered with
`fm <group> --help` and the README command table, not with a dependency.

### 6. Storage engine: SQLite through the stdlib, one file

`sqlite3`, one database at `<data-dir>/fitbit-monitor/store.sqlite3`, WAL,
`PRAGMA foreign_keys=ON`, `busy_timeout=5000` on every connection, `file:…?mode=ro`
plus `PRAGMA query_only=ON` for the read handle (ADR-0003 §1). Schema decisions are
ADR-0003; this ADR only picks the engine and defends that choice against the two
alternatives the ticket asked about.

SQLite wins on properties, not on speed:

* **Nothing to install.** `import sqlite3` is the whole client. macOS ships a `sqlite3`
  command-line shell, so the user - not us - can open their own data with a program
  they did not get from us. That is the difference between an archive and a lock-in.
* **One file, one copy = one backup.** `fm store backup` uses SQLite's online backup
  API to produce a consistent snapshot while the importer is writing, and
  `PRAGMA integrity_check` lets `fm store verify` answer "is my history intact" from
  the file itself rather than from our code's opinion.
* **Concurrency we actually need.** WAL lets the plugin read while the importer writes;
  a `flock` next to the file makes the single writer explicit.
* **The engine enforces the plugin promise.** `mode=ro` + `query_only` is why
  "a plugin cannot write to the store" is a fact rather than a code-review convention
  (ADR-0004).
* **Idempotence is a schema feature.** The `ON CONFLICT … DO UPDATE` upserts that make
  overlapping imports safe (ADR-0003 §4) need SQLite ≥ 3.24 (June 2018). That is the
  floor this repository depends on; the interpreter used during development links
  3.46. Every build worth caring about (python.org, Homebrew, uv-managed, Debian ≥ 11)
  links ≥ 3.34.

**Rejected: DuckDB.** It is the right tool for a different workload: columnar scans
over hundreds of millions of rows. The workload here is one person's 5 000-odd days -
tens of millions of intraday points at the very most, and a *write* pattern dominated
by small idempotent upserts, which is the shape DuckDB is weakest at. Against it,
concretely: a ~50 MB third-party wheel (§2 forbids it); no read-only enforcement
equivalent to `query_only`, so the plugin sandbox loses its teeth; an on-disk format
whose compatibility promise is bounded by major versions (the project documents that
files may not survive a major-version bump), which is the wrong bet for a file that
must outlive us; and no tool on any user's machine that can open it besides ours. Where DuckDB's strength is real - "let me analyse the whole history" - the answer
is `fm store export --format csv|jsonl` into whatever the user wants to analyse with,
which is a one-way door instead of a format marriage.

**Rejected: Parquet (+pyarrow).** Parquet is a file format, not a database: no
transactions, no upsert, no concurrent writer, no index - not one of the properties
ADR-0003 is built on. It would also be two dependencies (pyarrow is ~40 MB of native
code) to serve a workload measured in megabytes. It stays a *possible export target*,
which is where columnar formats belong in a tool like this.

**Rejected: sqlmodel / SQLAlchemy.** The schema is the product, and it is written as
numbered `.sql` migrations that a user can read (ADR-0003 §5). An ORM would put a
second description of the schema between the user and their data, add three runtime
dependencies, and make "what SQL does this emit" the interesting question at exactly
the moment nobody wants to answer it.

**Rejected: JSON lines / `shelve`.** "Easy to inspect" is the only argument, and
`fm store export` and `sqlite3 foo.sqlite3` already provide it with actual query power.

### 7. Test runner: pytest, with the cases written as `unittest.TestCase`

pytest is the runner (test selection, `-k`, `--lf`, assertion rewriting, one place for
fixtures). The cases themselves inherit from `unittest.TestCase`, which costs nothing
under pytest and buys one property we care about: `python -m unittest discover -s
tests` runs the whole suite with **nothing installed**, and the same is true of a
plugin author's own suite (ADR-0004's `PluginContractTestCase` is a unittest class, so
`python -m unittest` works in a bare scaffold directory).

`tests/` sits outside the package (§4) and is not part of the coverage source (§8).
Recorded HTTP fixtures (`tests/fixtures/api`) keep the network out of the suite
(ADR-0005 §6); `scripts/dev/record_fixtures.py` makes those recordings, and
`--check` fails when they are stale.

**Rejected: bare unittest as the runner** - the assertion diffs and `-k` are worth more
than the dependency-free argument, which the `TestCase` base class already gives us for
the environments that need it. **Rejected: nox/tox** - the matrix is four interpreter
versions driven by one line of CI plus `scripts/ci/*`, and a runner whose only job is
to loop over that list is a fifth tool to learn. If the matrix ever grows an axis
(32-bit, Windows, free-threaded), that is the trigger to revisit it.

### 8. Coverage: coverage.py, branch mode, floor 85

coverage.py is the tool of record; `scripts/ci/coverage.sh` runs
`coverage run --branch --source=src/fitbit_monitor -m pytest`, then
`coverage report` (which enforces `[tool.coverage.report] fail_under = 85`) and
`coverage xml` for the artifact the CI job uploads. pytest-cov is installed in the dev
extra so `pytest --cov` works when someone wants it; the *gate* does not depend on it,
which is what makes the coverage number one number rather than two.

Branch coverage, not statement coverage - a `try/except` whose except arm is never
exercised is exactly the kind of promise this program makes about bad data.

**Rejected: statement-only coverage** (hides half-dead branches). **Rejected:
diff-cover / "85% of the diff"** (a diff-scoped floor never pays down the parts of the
store that need it most). **Rejected: mutation testing** (mutmut/cosmic-ray) - the
strongest of these options and genuinely tempting, but minutes-to-hours per run against
a gate that currently finishes in tens of seconds; it belongs in a scheduled job, if
it ever arrives. Neither is a decision this repository needs now, and neither is free.

### 9. Benchmarks: a small harness with a committed baseline, not pytest-benchmark

`scripts/bench/bench_plugin_system.py` measures five cases in the order a real run
pays for them - corpus write (rows/sec), intraday scan (points/sec), day downsampling
(points/sec), the shipped plugin end-to-end (ms), discovery (ms) - takes the best of
N repeats, and fails if any case moves more than 30 % against
`benchmarks/baseline.json`. `scripts/ci/benchmarks.sh` is the gate; `benchmark.yml`
runs it on every push and posts the numbers to the job summary.

The baseline is **committed JSON, with a unit and a direction per case**, so a
re-record is a one-line diff a reviewer can read and refuse:

```json
{ "cases": { "scan_intraday": 580789.948 },
  "units": { "scan_intraday": "points/sec" },
  "direction": { "scan_intraday": "higher" } }
```

**Rejected: pytest-benchmark** (the recommendation). Its statistics - rounds,
outliers, mean and stddev - are worth having for microsecond benchmarks; these are
end-to-end scenarios where the setup (seed a 90-day corpus) is comparable to the
measured work, so the honest statistic is best-of-N and the honest tolerance on a
shared CI runner is ±30 % regardless of how many decimals the tool reports. Worse, its
comparison history is a machine-oriented blob that cannot be reviewed in a diff, and
reviewable diff *is* the point of the gate: this repository's benchmark job caught a
real 25 % ingest regression (`seed_corpus` fell ~149k → ~112k rows/sec when a per-row
`SELECT` crept into the hot write path), and what made it reviewable was a baseline a
human had signed off on. Adopting pytest-benchmark would also turn every benchmark into
a test collected by the default `pytest` run, paying corpus setup on each one.

**Rejected: asv (airspeed velocity)** - the right tool for a library that publishes
perf trends across many commits and machines; too much machinery (its own JSON history
directory, its own matrix) for one perf gate. **Rejected: no benchmark job** - the
read API and the ingest path are O(rows) by construction and it is easy to write an
O(n²) by accident; a job that catches that is the cheapest reviewer in the repository.
(AGENTS.md requires both a coverage and a benchmark job, and both exist.)

### 10. Lint, format, type check

**Type check: mypy**, configured in `pyproject.toml` and run first in the lint gate
against `src/fitbit_monitor` (53 modules). mypy is not style policing here: the plugin
contract is typed on purpose so a plugin author's editor shows the same truth as CI
(ADR-0004). Today's config is permissive in two named ways - `disallow_untyped_defs =
false`, `ignore_missing_imports = true` - and tightening either is a policy change to
this section, not a surprise in a pull request.
**Rejected: pyright-only** (a second runtime, Node, for a repo that promises a
no-install dev loop; mypy runs from the same venv). **Rejected: pytype** - same, and
weaker on the `Protocol`/`Literal` usage the contract leans on.

**Lint: ruff**, replacing pyflakes. pyflakes' rules are ruff's `F` set, so this is a
strict superset of the old gate in one fast binary. The rule set is committed, and it
is deliberately staged:

```toml
[tool.ruff.lint]
select = ["E4", "E7", "E9", "W", "F"]   # tier 1 - what the tree passes today
```

Tier 1 is **clean today** - the swap retired pyflakes without importing a single new
violation, which is the only way a linter swap survives contact with an in-flight
ticket. Tier 2 (`I`, `UP`, `SIM`, `B`, `C4`, `RUF100`) is measured, not guessed: **156
findings, 119 of them fixed by `ruff check --fix`**, leaving 37 sites for a human -
mostly `B017 assertRaises(Exception)` in tests and `B023` closures over loop
variables. The tier-2 commit is that sweep and nothing else.

**Format: ruff format, chosen now, enforced in one named commit.** Running it today
would touch **74 of 82 Python files and about 3 000 diff lines** at
`line-length = 100`. A reformat that size in a mixed commit is unreviewable and, while
several tickets are in flight, a conflict generator. So: the *choice* is made here and
the *settings* are committed in `[tool.ruff.format]` now, and the trigger for
enforcement is stated: **the first commit after the foundations epic closes, or any
single commit that already touches 20+ source files - whichever comes first - will be
formatting-only and will add `ruff format --check` to `scripts/ci/lint.sh`.** Until
then, new code follows the surrounding style by hand and nothing is reformatted as a
drive-by.

**Rejected: black + isort + flake8** - three tools, three configs, three slow steps to
say what ruff says in one. **Rejected: pyflakes alone** - retired by this ADR: its rules
are now ruff's `F` set, it has far less to say beyond them, it takes no configuration
worth having, and there is no formatter attached to it. **Rejected: pylint** - its default rule set
assumes a different language's idioms and needs a suppressions file roughly the size of
the source to get off the ground. **Rejected: adding formatting to `ruff check` today**
- see the trigger above.

### 11. Console script names: `fitbit-monitor` and `fm`

Two `[project.scripts]` entries, one function:

| console script | entry point | who types it |
|---|---|---|
| `fitbit-monitor` | `fitbit_monitor.cli:main` | docs, crontabs, other programs |
| `fm` | `fitbit_monitor.cli:main` | a human, forty times a day |

`fitbit-monitor` is the name that goes in documentation, in a crontab line, in a
systemd unit, and in another program's `subprocess` call: unambiguous, greppable, and
it survives a `ps` listing. `fm` is what a human types forty times a day; the whole
book of documentation here is written with it. Both are declared in the same
distribution, so they cannot drift, and a test pins both to the same entry point.

**Rejected: `fitbit-monitor` only** - a 14-character prefix on every command is a tax
paid by the only person who uses this tool, and nobody has ever typed `fitbit-monitor
store verify` happily. **Rejected: `fitbit`** - it shadows the vendor's own
namespace, invites confusion with the vendor's SDK, and reads as a claim we are not
entitled to make about their trademark. **Rejected: `fitbit-monitor` plus a shell alias
in the README** - an alias only exists on the machine where it was written, and the
plugin scaffold cannot assume it.

### 12. Dev entrypoints, and how the gates are wired

The rule is one sentence: **`scripts/dev/*` is what you type, `scripts/ci/*` is the
gate, and the workflows call the gates with `--no-install`.** Nothing else may hold a
command line.

| entrypoint | what it does | gate underneath |
|---|---|---|
| `scripts/dev/bootstrap.sh` | create `.venv`: `uv sync --extra dev` if uv is on PATH, else `python -m venv` + `pip install -e ".[dev]"` | - |
| `scripts/dev/lint.sh` | type check + lint + byte-compile + YAML/shell parse | `scripts/ci/lint.sh --no-install` |
| `scripts/dev/test.sh` | full suite + coverage floor | `scripts/ci/coverage.sh --no-install` |
| `scripts/dev/bench.sh` | perf gate vs the baseline; passes through `--update-baseline`, `--days`, `--json` | `scripts/ci/benchmarks.sh --no-install` |
| `scripts/dev/check.sh` | all three, the way CI runs them; `--quick` skips the benchmark | `scripts/ci/post_commit.sh` |
| `scripts/dev/docs.sh` | regenerate the generated API-reference block in `docs/plugins.md` | `scripts/docs/api_reference.py` |
| `scripts/dev/record_fixtures.py` | (re)record the API fixtures; `--check` fails if they are stale | the importer tests replay `tests/fixtures/api` |

Every script under `scripts/` - the gates included - resolves `$PYTHON` through
`scripts/_python.sh`: an explicit `$PYTHON` (what CI sets), else an activated
virtualenv, else this repository's `.venv`, else `python3`. That is what makes
`./scripts/ci/coverage.sh --no-install` and `./scripts/dev/test.sh` the *same* command
from a fresh clone, rather than a wall of `ModuleNotFoundError: fitbit_monitor`
reported by whichever python3 happened to be first on `PATH`. The `scripts/dev` set
above is exactly what the README names, and `tests/test_tooling.py` fails if an
entrypoint is added without being documented or documented without existing.

Both workflows stay thin: `ci.yml` runs `uv run --locked --extra dev --python <matrix>
./scripts/ci/{lint,coverage}.sh --no-install` over 3.11-3.14 and uploads
`coverage.xml`; `benchmark.yml` does the same with `benchmarks.sh`. `--locked` is what
makes a stale `uv.lock` a CI failure, which is the difference between a pin and a
suggestion.

After every commit, `scripts/ci/post_commit.sh` runs the same trio locally (and is
what an act-mcp style runner calls, since it needs the repository root that holds
`.github/workflows`); install it as a `git` `post-commit` hook with the symlink
documented in the script header.

**Rejected: the pre-commit framework** - it installs a second framework whose hooks
would duplicate the gate scripts, into an environment the user did not ask for;
a two-line `post-commit` symlink to an existing script does the same job with no new
tool. **Rejected: "run the gates manually, trust people"** - the whole value of a gate
is that forgetting is cheap.

## Consequences

* A follow-up ticket can build the scaffold without inventing anything: package name
  `fitbit-monitor`, `src/fitbit_monitor/`, scripts `fm` and `fitbit-monitor`, entry
  point group `fitbit_monitor.plugins`, dev tooling in `[project.optional-dependencies]
  dev`, dev commands in `scripts/dev/*`. That is the point of this file.
* The zero-dependency rule (§2) is now load-bearing and tested. Every future
  dependency needs an amendment here, and the honest test for one is: does it protect
  the user, or does it spare the author?
* Two debts are owed on purpose, both with a named trigger and a measured size: the
  tier-2 ruff rule set (156 findings, 119 auto-fixable) and the single formatting-only
  commit (74 files, ~3 000 lines) that turns on `ruff format --check`.
* The CI matrix is four Python versions x (lint + coverage) plus a benchmark job. That
  is a few minutes of CI per push, paid so that the floor and the ceiling both stay
  true.
* uv is now the documented fast path, and `uv.lock` is the source of truth for tool
  versions - which means `pyproject.toml` + `uv.lock` must be committed together from
  now on, or `--locked` says so loudly.
* The scripts named in README, ADR, and this repository's tests are the same set. If a
  later ticket wants a different command surface, it changes this ADR and the table in
  §12 together.
