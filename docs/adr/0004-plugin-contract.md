# ADR-0004: the plugin contract - discovery, interfaces, configuration, versioning

- **Status:** accepted
- **Date:** 2026-11-22
- **Implements:** `fitbit_monitor.plugins` (the contract in code),
  [docs/plugins.md](../plugins.md) (the author guide)
- **Related:** ADR-0003 (storage model - what `ctx.store` reads),
  ADR-0002 (evidence, where that work lands), ADR-0001 (lint/coverage gates that keep
  this contract honest)

> **This document is normative and tested.** Every signature, field name, constant
> and status string below is copied from the code, and
> `tests/test_docs.py::PluginContractIsDecidedOnce` fails if the code grows a
> plugin-facing field, method or error class that this file does not name. The
> sample plugin in §10 is executed against the same fixture store the contract
> suite uses. Prose belongs in `docs/plugins.md`; *decisions* belong here.

## Context

The reason this program exists is that the user's health data is local and their
questions are personal: *is my resting HR creeping up*, *did my HRV drop before the
bad night*, *was my heart rate abnormal while I was sitting still*. The core cannot
answer those - it can store, read, and deliver. So the storage layer must be
separable from the interpretation layer, and the interpretation layer must be
writable by someone other than the core's author.

Four requirements follow, and they are in tension:

* a plugin must be **small** to write - a class, not a project;
* it must be **unable to do damage** - it reads someone's medical history;
* it must be **unable to break the host** - a plugin failure during an import run
  must not lose the import;
* it must be **distributable**, in-tree as one file or out-of-tree as its own package.

Epic 5 (metrics) and Epic 6 (alerts) are written against whatever this file decides,
so the decisions are made here once, with their exact shapes, rather than invented
twice.

## Decision

### 1. One base class, two kinds, one context object

There is **one** plugin interface: `MonitorPlugin`. There are **two kinds** of plugin,
and the kind is decided by what a plugin emits, not by which class it subclasses:

| kind | emits | via | returned model | typical question |
|---|---|---|---|---|
| metric-producing | a value over a window | `ctx.metric(...)` | `MetricResult` | "what was my mean resting HR this week" |
| alerting | an episode the user should hear about | `ctx.alert(...)` | `Alert` | "my resting HR crossed 65 bpm" |

A plugin MAY do both (an alert usually quotes the metric behind it). What it may not
do is return its findings any other way: there is no plugin-writable table, no
"metrics" queue, no printing to stdout. **Emit through the context, and the host
owns storage, delivery, dedup and the report.** That single funnel is what makes §11
(dedup) and §8 (isolation) possible.

`run(ctx)` receives a `PluginContext` and nothing else. The context *is* the
capability set: if it is not on the context, a plugin cannot reach it.

### 2. The base class, exactly

`fitbit_monitor.plugins.MonitorPlugin` (ABC). Attributes are class-level and are the
plugin's declaration of itself; the two methods below are the whole lifecycle.

```python
class MonitorPlugin(abc.ABC):
    name: str = ""                        # stable id: `--name`, [plugins.<name>]; never rename
    summary: str = ""                     # one line, shown by `fm plugins list`
    version: str = "0.1.0"                # the plugin's own version, semver-ish string
    requires_api: int = PLUGIN_API_VERSION     # §7
    datasets: Sequence[str] = ()          # datasets this plugin reads, §6
    options: Sequence[PluginOption] = ()  # its config schema, §6

    def configure(self, config: PluginConfig) -> None: ...
    @abc.abstractmethod
    def run(self, ctx: PluginContext) -> Iterable[Any] | None: ...

    @classmethod
    def declared_defaults(cls) -> dict[str, Any]: ...
    @classmethod
    def option_map(cls) -> dict[str, PluginOption]: ...
    @classmethod
    def describe(cls) -> dict[str, Any]: ...
```

* `configure(self, config: PluginConfig) -> None` - validate and cache options
  (`self.config` is set for you by the default implementation). Called once before
  `run`, and once per discovery pass by `fm plugins validate`. Anything expensive or
  fallible about configuration belongs here, **not** in `run`: a bad option becomes a
  `config_error` outcome here rather than a `run` failure later. Raise
  `PluginConfigError` to reject your own section.
* `run(self, ctx: PluginContext) -> Iterable[Any] | None` - compute over
  `ctx.window`. Either call `ctx.metric()` / `ctx.alert()` **or** return an iterable
  of `MetricResult` / `Alert` (the host adopts it via `ctx.adopt`); not both. Return
  `None` when there is nothing to say - "nothing to report" is a normal answer, see §8.
* `name` must match `^[a-z][a-z0-9_.-]{0,63}$`. It is a user-visible key in
  `config.toml` and in cron lines, so renaming it is a breaking change.
* A class that does not satisfy this is refused at discovery by
  `validate_plugin_class()` with a `PluginRegistrationError` that names the plugin and
  says what is missing (no `name`, `run` still abstract, `requires_api` too new,
  duplicate option names, blank dataset entry). That check is the first error a
  plugin author sees, so its messages are written for them.

### 3. What a plugin may touch: `PluginContext` and `ReadOnlyStore`

`PluginContext` is built by the host, never by a plugin, and is `__slots__`-bounded.
Its entire public surface is this table; the contract suite (§9) and
`tests/test_contract.py` fail if it grows:

| member | signature | gives | may |
|---|---|---|---|
| `store` | `ReadOnlyStore` | the local store | read only |
| `config` | `PluginConfig` | this plugin's own `[plugins.<name>]` section | read only |
| `window` | `DateRange` | half-open `[since, until)`, tz-aware UTC | read only |
| `now()` | `() -> datetime` | the host's clock (injectable in tests) | read only |
| `clock` | `Callable[[], datetime]` | the raw clock `now()` uses | read only |
| `log` | `logging.LoggerAdapter` named `fitbit_monitor.plugin.<name>` | the run's logs | write to logs |
| `metric(...)` | see below | emit a `MetricResult` | append to this run's results |
| `alert(...)` | see below | emit an `Alert` | append to this run's results |
| `metrics` / `alerts` | `-> tuple[...]` | what has been emitted so far | read only |
| `adopt(results)` | `(Iterable[Any]) -> None` | take ownership of models `run()` returned | append |
| `deadline` | `datetime \| None` | when the wall-clock budget expires | read only |
| `time_remaining()` | `() -> float \| None` | seconds left | read only |
| `check_timeout()` | `() -> None` | cooperative checkpoint; raises `PluginTimeout` | raise |
| `cancelled` | `bool` | the host has asked this plugin to stop | read only |
| `request_cancel()` | `() -> None` | host-side cancellation (a plugin only reads `cancelled`) | internal |
| `plugin_name` / `api_version` | `str` / `int` | the host's view of who you are | read only |
| `as_dict()` | `() -> dict[str, Any]` | the run record for this plugin | read only |

`metric` and `alert` are the emission signatures, keyword-only after the first
arguments, exactly as implemented:

```python
def metric(self, key: str, value: float | int | str, *,
           unit: str | None = None, confidence: float | None = None,
           window_start: datetime | None = None, window_end: datetime | None = None,
           labels: Mapping[str, str] | None = None,
           details: Mapping[str, Any] | None = None,
           summary: str | None = None) -> MetricResult: ...

def alert(self, key: str, title: str, message: str, *,
          occurred_at: datetime, severity: Severity | str = Severity.WARNING,
          confidence: float | None = None, evidence: Iterable[Evidence] | None = None,
          window_start: datetime | None = None, window_end: datetime | None = None,
          dedup_key: str | None = None, labels: Mapping[str, str] | None = None,
          details: Mapping[str, Any] | None = None) -> Alert: ...
```

**The read side is `ReadOnlyStore` (`ctx.store`), and the rule is absolute: a plugin
MUST NOT write to the store.** Not "should not". The host's own writer is never
imported on this path, and the connection is opened by
`schema.connect_ro()` as `file:<path>?mode=ro` with `PRAGMA query_only=ON`, so the
refusal is enforced by SQLite rather than by manners - a plugin that reaches for
`ctx.store.conn.execute("INSERT ...")` gets `sqlite3.OperationalError`. Anything a
plugin wants to leave behind is a `MetricResult` or an `Alert`; the host persists
those (§11).

The methods a plugin may call (full descriptions in
[docs/plugins.md § API reference](../plugins.md)):

| accessor | shape |
|---|---|
| `datasets()` / `has_dataset(name)` | `-> tuple[str, ...]` / `-> bool` |
| `stored_datasets()` | `-> tuple[DatasetInfo, ...]` - every dataset as stored, with counts and units |
| `query(dataset, since=None, until=None, *, fields=None, order="asc", limit=None)` | `-> Iterator[SeriesPoint]` |
| `series(dataset, window, **kw)` | `query` with `ctx.window` already applied |
| `first_ts` / `last_ts(dataset)` | `-> datetime \| None` |
| `latest(dataset, *, fields=None)` | `-> SeriesPoint \| None` |
| `count(dataset, since=None, until=None)` | `-> int` |
| `coverage(dataset, since, until)` | `-> Coverage` (`points`, `fraction`, `has_data`) |
| `has_any_data(datasets, window)` | `-> bool` - what §6's no-data gate calls |
| `days_with_data(dataset=None)` / `day(dataset, day)` | `-> list[date]` / `-> list[SeriesPoint]` |
| `gaps(...)` / `completeness(...)` | `-> list[Gap]` / `-> Completeness` |
| `describe(dataset)` / `units(dataset)` / `grain_seconds(dataset)` | catalog metadata for that dataset |
| `iter_raw(...)` / `raw_payload(...)` / `raw_for(...)` / `raw_totals(...)` | raw retained payloads, read only |
| `assert_read_only()` | `-> None`; raises if this connection could write |

`SeriesPoint` is `(ts: datetime, values: dict[str, float], dataset: str)` with
`p["field"]`, `p.get(name, default)`, `p.unit(name)` and `p.items_with_units()`.
Time-only helpers `downsample`, `daily_values` and `aggregate` are re-exported from
`fitbit_monitor.plugins` (§10), because every plugin ends up bucketing by day.

A plugin MUST NOT be given - and `PluginContext.__slots__` makes it impossible to
hand it - a writable connection, a filesystem handle, a network client, the host
object, or another plugin's configuration.

### 4. What a plugin returns: `MetricResult`, `Alert`, `Evidence`, `Severity`

All four live in `fitbit_monitor.plugins.models`, are exported from
`fitbit_monitor.plugins`, and are **frozen dataclasses that validate in
`__post_init__`**. Validation at the boundary is the decision: a plugin learns about
a bad emit at the `ctx.metric(...)` call that caused it, with a message naming the
field - not three layers later inside a sink. All three models round-trip (`as_dict()`,
`as_json()`, `from_dict()`), which is what the JSONL history and the webhook depend
on.

`Severity` - `str` enum, exactly three levels, `rank` 0 < 1 < 2, `at_least(other)`
for "warn me at warning and above":

| member | value | accepted aliases (parsed, case-insensitive) |
|---|---|---|
| `Severity.INFO` | `"info"` | `info`, `informational`, `notice` |
| `Severity.WARNING` | `"warning"` (the default) | `warn`, `warning`, `moderate` |
| `Severity.CRITICAL` | `"critical"` | `severe`, `error`, `critical`, `high` |

Anything else raises `ValidationError` naming the offending value.

`Evidence` - one data point an alert quotes: *when*, *what value*, *from where*.
Every evidence timestamp is a tz-aware `datetime`, normalised to UTC:

| field | type | required | rule enforced at construction |
|---|---|---|---|
| `at` | `datetime` | yes | naive or unparseable is rejected; stored as UTC |
| `value` | `float` | yes | must be a real number; `bool` is rejected; `int` is widened |
| `dataset` | `str \| None` | no | which stored dataset it came from |
| `detail` | `str \| None` | no | free-text qualifier, e.g. `"stage: deep"` |

`MetricResult` - the metric kind's return value:

| field | type | required | rule |
|---|---|---|---|
| `plugin` | `str` | yes (`ctx.metric` fills it) | non-empty |
| `key` | `str` | yes | stable per metric, non-empty; the identity you chart against |
| `value` | `float \| int \| str` | yes | a number or a string; `bool` is rejected |
| `window_start` | `datetime` | yes (defaults to `ctx.window.since`) | tz-aware, normalised to UTC |
| `window_end` | `datetime` | yes (defaults to `ctx.window.until`) | tz-aware; must not precede `window_start` |
| `unit` | `str \| None` | no | display unit, e.g. `"bpm"` |
| `confidence` | `float \| None` | no | `0.0 .. 1.0` inclusive; out of range raises |
| `labels` | `Mapping[str, str]` | no, `{}` | copied into an immutable mapping |
| `details` | `Mapping[str, Any]` | no, `{}` | copied into an immutable mapping; the audit trail |
| `produced_at` | `datetime \| None` | no (`ctx.now()`) | tz-aware if set |
| `summary` | `str \| None` | no | one-line human reading of the value |

`Alert` - the alerting kind's return value:

| field | type | required | rule |
|---|---|---|---|
| `plugin` | `str` | yes (filled) | non-empty |
| `key` | `str` | yes | non-empty; the kind of episode, e.g. `"high"` |
| `title` | `str` | yes | non-empty, one line |
| `message` | `str` | yes | non-empty; what the user reads |
| `occurred_at` | `datetime` | yes | tz-aware, normalised to UTC - *when the data says it happened* |
| `severity` | `Severity` | no, `Severity.WARNING` | parsed and normalised, see the table above |
| `confidence` | `float \| None` | no | `0.0 .. 1.0` inclusive |
| `evidence` | `tuple[Evidence, ...]` | no, `()` | must be in chronological order; accepts `Evidence`, `{"at","value",...}` mappings or `(at, value)` pairs |
| `window_start` | `datetime \| None` | no (defaults to `ctx.window.since`) | tz-aware, normalised to UTC |
| `window_end` | `datetime \| None` | no (defaults to `ctx.window.until`) | tz-aware; must not precede `window_start` |
| `dedup_key` | `str \| None` | no | identity used for cooldown; defaults to `effective_dedup_key` = `<plugin>/<key>/<severity>` |
| `labels` | `Mapping[str, str]` | no, `{}` | immutable copy |
| `details` | `Mapping[str, Any]` | no, `{}` | immutable copy |
| `alert_id` | `int \| None` | host-filled | row id from the alert store |
| `first_emitted_at` | `datetime \| None` | host-filled | first sighting of this dedup key |
| `last_seen_at` | `datetime \| None` | host-filled | latest sighting of this dedup key |
| `occurrences` | `int` | host-filled, `1` | how many times the key has been seen |

Two of these rules are load-bearing rather than tidy:

* **Every timestamp crossing the boundary is tz-aware and normalised to UTC.** A
  health series crossing a DST change is exactly where naive arithmetic produces a
  confidently wrong answer, so `naive datetime` is a `ValidationError`, not a warning.
* **Evidence must already be chronological.** Sorting it silently would hide a plugin
  that built its timeline backwards; the contract suite re-checks the ordering of
  every emitted alert.

`Alert.dedup_key` SHOULD be set when several keys describe one episode (say `hr_high`
and `hr_very_high`), because cooldown is applied per dedup key by the dispatcher
(§11), not per alert.

### 5. Discovery: two sources, one stated precedence

A plugin is found in exactly three places, in this order of precedence (highest
first):

| # | kind | where it comes from | why it exists |
|---|---|---|---|
| 1 | `directory` | every `*.py` file (not `_`- or `.`-prefixed) and every `<pkg>/plugin.py` in each configured plugins directory, `sorted()` per directory, directories in configured order | one file, edited in place, no packaging |
| 2 | `entry_point` | installed distributions publishing classes to the entry-point group named by `ENTRY_POINT_GROUP` | a plugin that lives in its own repo and is installed with `pip` |
| 3 | `builtin` | `fitbit_monitor.plugins.builtin.BUILTIN_PLUGINS`, imported lazily | the examples and reference plugins that ship with `fm` |

The entry-point group name is a constant, not a string authors should retype:

```python
from fitbit_monitor.plugins import ENTRY_POINT_GROUP   # "fitbit_monitor.plugins"
```

Directories are configured in `config.toml` and may be appended from the environment,
so a checkout can be tried without editing anything the user owns:

```toml
[plugins]
dirs = ["~/.config/fitbit-monitor/plugins"]
```

`FM_PLUGIN_DIRS` (or the singular `FM_PLUGIN_DIR`) is appended to that list,
`os.pathsep`-split, with `~` and `$VAR` expanded in both, and a per-invocation `--dir`
is appended after both (repeatable). There is deliberately **no implicit default
directory**: a drop-in folder the user has not named is not searched, and
`fm plugins list` says `(none configured)` instead of inventing a path.

**On a name collision the winner is `directory` > `entry_point` > `builtin`, and
within one kind the first candidate found wins.** The loser is never dropped
silently: it is recorded as a `Collision` and printed by `fm plugins list`, and it
appears under `"collisions"` in `fm plugins list --json`. Letting a local directory
beat an installed distribution is deliberate - it is how you develop a replacement
for an installed plugin without uninstalling it.

A directory that does not exist is skipped (a default that is absent is not an
error); a path that exists but is not a directory, and a file that defines no
`MonitorPlugin` subclass, become candidates whose load fails, so they show up in
`fm plugins list` instead of vanishing. Entry-point *group* discovery itself is
guarded: broken metadata in `site-packages` is reported as one failing candidate,
never as a crash.

Multiple plugin classes in one module are allowed and kept in source-declaration
order (not alphabetical), because that is the order an author wrote them in.

### 6. Configuration: `[plugins.<name>]`, declared options, declared datasets

The host parses `config.toml` once. A plugin never sees the file: it sees a
`PluginConfig`, which is a read-only `Mapping` over its own section plus the defaults
it declared. It cannot read another plugin's section, the webhook URL, or the store
path.

```toml
[plugins.defaults]              # optional: shared defaults, minus `enabled`
timeout_seconds = 90

[plugins.resting_hr_drift]
enabled = true                  # host-reserved switch, valid for every plugin
timeout_seconds = 30            # host-reserved: this plugin's wall-clock budget
high_bpm = 70.0                 # the plugin's own option
```

The path from that table to the plugin is fixed:

1. `AppConfig.plugin_config(name, defaults)` merges
   `[plugins.defaults]` < declared defaults < `[plugins.<name>]` into a
   `PluginConfig(plugin_name=..., settings=..., defaults=..., declared=...)`.
2. The runner coerces every value to the type the plugin declared, so `configure` and
   `run` receive typed values, not TOML surprises.
3. If `config.enabled` is false the plugin is `skipped`, with the reason printed.
   Turning a plugin off is a supported state, not an error.
4. `plugin.configure(plugin_config)` runs - and `self.config` is available in `run`.

A plugin **declares its config keys** with `PluginOption`, which is also the schema
`fm plugins show` and `fm plugins validate` render:

```python
@dataclass(frozen=True)
class PluginOption:
    name: str
    type: str = "float"          # int | float | bool | str | duration | list
    default: Any = None
    help: str = ""               # required by the contract suite: it is user-facing
    required: bool = False
    choices: tuple[Any, ...] = ()
```

`coerce()` casts the TOML value to the declared type (`duration` accepts `"36h"`,
`"90m"`, `"7d"` or bare seconds), rejects a value outside `choices`, and raises
`ConfigError` if a `required` option is missing. The typed accessors on
`PluginConfig` - `get_int`, `get_float`, `get_bool`, `get_str`, `get_duration`,
`get_list` - raise `ConfigError` naming the plugin and the key instead of returning
`None` and letting a plugin guess.

Keys a plugin never declared are not silently ignored: `PluginConfig.unknown_keys` is
reported as *unused config keys* by `fm plugins show` and `fm plugins validate`, which
is how a typo in a user's `config.toml` becomes visible instead of mysterious.

A plugin **declares the datasets it needs** in `datasets`, and the runner uses that
declaration as a gate: before `run()` is called it records `store.coverage(dataset,
since, until)` for each declared dataset and, if none of them has any data in the
window, reports the plugin as `no_data` without running it (§8). "Nothing stored" and
"nothing found" are different answers, and only the declaration makes them
distinguishable without executing code.

### 7. Versioning: one integer, two mismatch behaviours

```python
PLUGIN_API_VERSION: Final[int] = 1       # the contract this host serves
MIN_PLUGIN_API_VERSION: Final[int] = 1   # oldest plugin API this host still accepts
# a plugin declares:  requires_api: int = PLUGIN_API_VERSION
```

`fm plugins --api` prints what the running host serves, and every plugin record
carries `requires_api`.

* `requires_api > PLUGIN_API_VERSION` - the plugin needs a newer host. Refused at
  **discovery** with `PluginRegistrationError` ("needs plugin API 2, this host
  provides 1", hint: upgrade `fitbit-monitor`). It is never partially loaded.
* `requires_api < MIN_PLUGIN_API_VERSION` - the host dropped the API the plugin was
  written against. Also refused at discovery, with its own message.
* Anything in between runs. New hosts run old plugins; never the reverse.

**Bump rule:** additive changes - a new read-only method on `PluginContext`, a new
optional field with a default, a new severity alias - do *not* bump the integer.
Removals, renames, and signature changes do, because those are the changes a plugin
author cannot compile against by accident. `tests/test_contract.py` asserts the
current value, so bumping it is a visible act, and §9's suite is what an author's CI
runs against the new number.

### 8. Failure is expected: isolation, timeouts, and an honest report

Discovery, configuration and execution of each plugin are guarded separately, and
`run_plugins()` never raises for a plugin reason: every failure becomes a named record
on that plugin's outcome and **the run continues**. One bad plugin cannot lose an
import, hang a run, or read another plugin's config.

The named failures (`fitbit_monitor.plugins.errors`, all subclasses of `PluginError`,
all carrying the plugin name and a `stage`):

| class | stage | means |
|---|---|---|
| `PluginImportError` | `import` | the module raised while being imported |
| `PluginRegistrationError` | `register` | not a usable plugin: missing members, bad `name`, unsupported `requires_api`, duplicate options |
| `PluginConfigError` | `configure` | the plugin rejected its own `[plugins.<name>]` section |
| `PluginRuntimeError` | `run` | `run()` raised (carries `cause`) |
| `PluginTimeout` | `timeout` | wall-clock budget exhausted (a `PluginRuntimeError` with its own name, so a runaway is not mistaken for a break) |

`STAGES = ("discover", "import", "register", "configure", "run", "timeout")` - the same
words in the JSON report, the CLI and this file. Each outcome carries one of
`ok`, `no_data`, `skipped`, `config_error`, `error`, `timeout`, plus `error_stage`,
`error_type`, `duration_ms`, `coverage`, and the emitted models;
`config_error`, `error` and `timeout` are the failing statuses.

**Timeouts and runaway work.** Each plugin runs on its own daemon worker thread with a
wall-clock budget: `--timeout` on the CLI, else `[plugins.<name>] timeout_seconds`,
else `[alerts] timeout_seconds`, else `DEFAULT_TIMEOUT_SECONDS = 120`. On expiry the
host **abandons** the plugin: it sets the context's cancel flag (`ctx.cancelled`),
records `PluginTimeout` with the budget in the message, and moves on. Killing a thread
is not possible in Python, so cancellation is cooperative - a long scan is expected to
call `ctx.check_timeout()` (which raises `PluginTimeout` at the next checkpoint). A
plugin that ignores the deadline wastes one daemon thread and nothing else: it cannot
delay the run, cannot hold results back from the report, and cannot keep the process
alive.

**Exit codes** (`fm plugins run`, same numbers everywhere - see `docs/plugins.md`):

| code | meaning |
|---|---|
| `0` | every plugin ran - including "ran, nothing to report", `no_data` and `skipped` |
| `1` | at least one plugin errored, misconfigured itself, or timed out |
| `2` | bad arguments or unreadable config |
| `4` | no local store to read |

`fm plugins list`, `fm plugins show` and `fm plugins validate` report failures with the
plugin's name and stage and propagate none of them. `fm import` (ADR-0005) MUST run
plugins after ingestion through this same guarded entry point - a plugin must never be
able to break an import - which is why the isolation lives in the runner and not in the
CLI.

### 9. The contract test suite a third-party plugin must pass

The suite is published, not described: `fitbit_monitor.plugins.testing` ships the same
conformance tests the built-ins run in CI, so "works on my machine" and "passes the
contract" are the same sentence. A plugin author writes:

```python
from fitbit_monitor.plugins.testing import PluginContractTestCase
from myplugin import MyPlugin

class TestMyPlugin(PluginContractTestCase):
    plugin_cls = MyPlugin
```

`fm plugin new` scaffolds that file, so a new plugin is held to the contract from its
first commit. The suite needs no fixtures of its own: `fixture_store()` writes a
deterministic seeded store (45 days, fixed end date `2026-05-31`, fixed seed),
`fixture_window()` matches it, `empty_window()` is the no-data path, and
`run_fixture_plugin()` runs a plugin exactly the way the CLI does.

What it asserts, and why each one is in the contract:

| assertion | guards |
|---|---|
| `validate_plugin_class()` accepts the class | §2's interface is actually implemented |
| `name` matches the id regex and is lowercase | the config key is stable and typed by a human |
| `version` and `summary` are non-empty | `fm plugins list` is never blank |
| declared `datasets` are non-empty strings | §6's no-data gate can work |
| `requires_api <= PLUGIN_API_VERSION` | §7 before the plugin is ever run |
| options are unique, carry `help`, and `coerce(default)` does not raise | a plugin cannot ship an unconfigurable or unusable default |
| `configure()` succeeds with defaults only | the plugin works with an empty config file |
| a config section cannot leak to another plugin name | §6's scoping |
| it runs `ok` (or `no_data`) on the fixture and produces something | the plugin is not dead on arrival |
| every emitted model is valid: `plugin` matches, keys non-empty, windows tz-aware and ordered, `confidence` within `0..1`, evidence chronological | §4 at runtime, not just at import |
| an empty window is reported, not crashed | "no data yet" is a normal state |
| two runs on the same data give the same metrics | no wall-clock or RNG leakage |
| `ReadOnlyStore` exposes no `put`, `put_many`, `execute`, `executescript`, `commit`, `clear`, `delete_range`, and `store.conn.execute("INSERT ...")` raises `sqlite3.OperationalError` | §3's read-only promise, enforced by SQLite |
| `PluginContext` exposes no public member outside the §3 table | the capability set cannot grow by accident |

### 10. A complete plugin, 20 lines

This is the whole contract, end to end: identity, declared dataset, declared option,
read-only read, one metric, one alert with an evidence timestamp. It is not pseudocode -
`tests/test_docs.py` executes it against the fixture store and asserts it reports a
metric.

```python
from datetime import UTC, datetime, time

from fitbit_monitor.plugins import MonitorPlugin, PluginContext, PluginOption, daily_values

class RestingHrDrift(MonitorPlugin):
    name = "resting_hr_drift"
    summary = "mean resting HR over the window; alerts when it runs high"
    version = "1.0.0"
    datasets = ("daily_summary",)
    options = (PluginOption("high_bpm", "float", 65.0, "alert above this many bpm"),)

    def run(self, ctx: PluginContext) -> None:
        days = daily_values(ctx.store.series("daily_summary", ctx.window), "resting_hr")
        if len(days) < 4: return                          # too few days to average honestly
        mean = sum(days.values()) / len(days)
        when = datetime.combine(max(days), time.min, tzinfo=UTC)
        ctx.metric("resting_hr_mean", round(mean, 1), unit="bpm", confidence=min(1.0, len(days) / 7))
        if mean > ctx.config.get_float("high_bpm", 65.0):
            ctx.alert("high", "Resting HR is running high", f"{mean:.0f} bpm over {len(days)} days",
                      occurred_at=when, severity="warning", evidence=[(when, round(mean, 1))])
```

Drop that file in a configured plugins directory and it is discovered as
`resting_hr_drift` (§5); run it with `fm plugins run resting_hr_drift --since 30d`;
tune it with `[plugins.resting_hr_drift] high_bpm = 70`. To ship it as its own
distribution instead, publish the class into the same group and nothing about the
plugin changes:

```toml
[project.entry-points."fitbit_monitor.plugins"]
resting_hr_drift = "resting_hr_drift.plugin:RestingHrDrift"
```

### 11. Output is data; delivery is the host's job

Plugins emit, the host delivers - that separation is why there is one interface
(§1) instead of a metric/alert class split. Metrics go into the run report
(`--json` for machines, the text summary for humans). Alerts are stored first, then
fanned out to the configured sinks - console, JSONL history, optional webhook - with
dedup and cooldown applied **once, in the dispatcher**, so every sink agrees on what
"the same alert again" means (`effective_dedup_key`, `[alerts] cooldown`, default
36h). A sink that fails is logged and recorded in the report's `deliveries`; a sink
failure never fails a run.

A replay is not a repeat: an alert no newer than what is already stored for its dedup
key is suppressed (`suppressed_by: "cooldown"`) and updates the stored row's
`last_seen_at` and `occurrences` instead. That makes `fm plugins run` idempotent and
safe to put in a timer, which is the only scheduler this project promises.

## Consequences

**Good**

* A plugin is one file importing from one module: §10 is a real plugin, not a sketch.
  The scaffold plus the published contract suite (§9) means a first plugin is correct
  before it is pushed.
* One bad plugin cannot lose an import, hang a run, or read another plugin's config.
* The read-only promise is enforced by SQLite and re-asserted by tests, so a reviewer
  of a third-party plugin can check it by inspection.
* The contract is checkable rather than remembered: this file, the code and the suite
  are mutually pinned, so "the ADR says X" is a statement you can trust.
* Outbound data has exactly one door (the webhook), one document
  ([data-policy.md](../data-policy.md)), and one test.

**Costs**

* Pinning exact field names and signatures in prose is a maintenance tax: adding a
  plugin-facing member means editing this file in the same commit as the code. That is
  the point - the tax *is* the review.
* The host cannot stop a runaway plugin, only stop waiting for it. Mitigated by
  `check_timeout()` in the contract and by the timeout being reported by name.
* `PLUGIN_API_VERSION` is a global knob: one bad removal affects everyone. Mitigated by
  the surface test, the §9 suite, and the additive-change rule.
* `directory` winning over `entry_point` can mask an installed plugin by accident.
  Mitigated by collisions being printed by `fm plugins list` and named in `--json`.
* Two discovery mechanisms is two code paths; the shared contract suite is what keeps
  them identical in behaviour.
* `Severity` is fixed at three levels. Three is enough for a personal monitor and is
  cheap to widen; five would make every sink and every cooldown rule argue about it.

**Explicitly out of scope**

* A daemon, watch mode, or scheduler inside `fm` (run it from cron or a timer).
* Sandboxing beyond read-only data access and a wall-clock budget: a plugin is trusted
  code the author wrote, not hostile code from a stranger. Publishing a plugin registry
  would require revisiting this line.
* Plugin-to-plugin dependencies, ordering, or a metrics store that plugins write to -
  see §1's one-way door.
* A plugin-side network client. Outbound is the webhook's job (§11).
