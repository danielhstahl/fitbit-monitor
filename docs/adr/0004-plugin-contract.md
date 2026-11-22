# ADR-0004: the plugin contract - discovery, interfaces, configuration, versioning

- **Status:** accepted
- **Date:** 2026-11-22
- **Implements:** `fitbit_monitor.plugins` (contract in code), `docs/plugins.md` (author guide)
- **Related:** ADR-0003 (storage model), ADR-0002 (evidence, where that work lands)

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

## Decision

### 1. One interface, one context object

A plugin subclasses `MonitorPlugin`, sets `name`, `summary`, `version`, optionally
`datasets` and `options`, and implements `run(ctx)`. `run(ctx)` receives a
`PluginContext` and nothing else; the context is the entire capability set:

| member | gives | may |
|---|---|---|
| `store` | `ReadOnlyStore` over the local store | read only |
| `config` | that plugin's own config section, typed | read only |
| `window` | the half-open `[since, until)` UTC window | read only |
| `now()`, `clock` | the host's clock | read only |
| `log` | `fitbit_monitor.plugins.<name>` logger | write to logs |
| `metric()`, `alert()` | emission of `MetricResult` / `Alert` | append to the run's results |
| `deadline`, `time_remaining()`, `check_timeout()` | the wall-clock budget | read only |

A plugin MUST NOT be given: a writable connection, a filesystem handle, a network
client, the host object, or another plugin's configuration. `PluginContext` is
`__slots__`-bounded and a test asserts its public surface, so growth is a decision
rather than an accident.

`ReadOnlyStore` opens SQLite `mode=ro` and `PRAGMA query_only=ON`. Read-only is
therefore enforced by the database engine, not by convention, and the contract test
suite asserts a write through `ctx.store.conn` raises `sqlite3.OperationalError`.

### 2. Values are validated at the boundary

`MetricResult` and `Alert` are frozen dataclasses whose `__post_init__` rejects a
missing required field, a severity outside `{info, warning, critical}`, a confidence
outside `0..1`, a naive timestamp, an inverted window, and evidence that is not in
chronological order. A plugin learns about a bad emit at the `ctx.metric()` call
that caused it, with a message naming the field.

Every timestamp crossing the boundary is timezone-aware and normalised to UTC. This
is not cosmetic: a health series crossing a DST change is precisely where naive
arithmetic produces a confidently wrong answer.

`Alert.dedup_key` (default `<plugin>/<key>/<severity>`) is the identity used for
cooldown. Plugins SHOULD set it when several keys describe one episode.

### 3. Two discovery sources, with a stated precedence

Discovery is by `importlib.metadata` entry points in the group
`fitbit_monitor.plugins`, plus scanning each configured plugins directory for
`*.py` files and `<pkg>/plugin.py`.

Precedence on a name collision, highest first: `directory` > `entry_point` >
`builtin`; within a source the first candidate found wins. The loser is reported,
never dropped silently. A local directory beating an installed distribution is
deliberate: it is how you develop a replacement for an installed plugin without
uninstalling it.

Entry points exist so a plugin can live in its own repository and be installed with
`pip`; the directory source exists so a plugin can be one file edited in place. Both
paths MUST keep working, and the scaffolded plugin supports both.

### 4. Failure is expected and isolated

Discovery, configuration and execution of each plugin are individually guarded. A
failure is recorded as a named `PluginError` (`import`, `register`, `configure`,
`run`, `timeout`) attached to that plugin's record, and the host continues. `fm
plugins list`, `fm plugins validate` and `fm plugins run` all report the failure
with the plugin's name; none of them propagates it.

Each plugin runs on its own daemon worker thread with a wall-clock budget
(default 120s, per-plugin override via `timeout_seconds`, CLI override via
`--timeout`). On expiry the host **abandons** the plugin: it marks a
`PluginTimeout`, releases the main thread and continues. Killing a thread is not
possible in Python, so the contract is cooperative: the context carries a deadline
and `check_timeout()`; a plugin that ignores it wastes a thread and nothing else -
it cannot delay the run, and the daemon thread cannot prevent the process exiting.

The import command (owned by the import epic) MUST run plugins after ingestion
through the same guarded entry point, which is why the isolation lives in the runner
and not in the CLI.

### 5. Configuration is scoped and declared

`[plugins.<name>]` in `config.toml` is a plugin's whole visible configuration. The
host parses the file once; a plugin receives a `PluginConfig` carrying its own
section plus the defaults it declared - it cannot read another plugin's section,
the webhook URL, or the store path. Options are declared with `PluginOption(name,
type, default, help, choices, required)`; values are coerced to the declared type
before `configure()`, and keys a plugin never declared are surfaced as *unused
config keys* by `fm plugins show` and `fm plugins validate` rather than silently
ignored. `[plugins.<name>].enabled = false` skips the plugin with the reason
printed: turning one off is a supported state, not an error.

### 6. Versioning is a single integer

`PLUGIN_API_VERSION` describes the contract, `requires_api` declares what a plugin
needs, and `MIN_PLUGIN_API_VERSION` is the floor this host still accepts. A plugin
needing a newer host fails at discovery with a named error. Additive changes
(a new read method, a new optional field) do not bump it; removals, renames and
signature changes do. The API version is asserted in tests, so bumping it without
updating the assertions is a visible act.

### 7. Output is data, and delivery is the host's job

Plugins emit; the host delivers. Metrics go into the run report (`--json` for
machines). Alerts are stored first, then fanned out to sinks - console, JSONL
history, optional webhook - with dedup and cooldown applied once, in the
dispatcher, so every sink agrees on what "the same alert again" means. A sink that
fails is logged and recorded in the report; a sink failure never fails a run.

A replay is not a repeat: an alert no newer than what is already stored for its
dedup key changes nothing, which makes `fm plugins run` idempotent and safe to put
in a timer.

## Consequences

**Good**

* A plugin is one file, importing three names from one module. The scaffold plus the
  published contract suite means a first plugin is correct before it is pushed.
* One bad plugin cannot lose an import, hang a run, or read another plugin's config.
* The read-only promise is enforced by SQLite, and re-asserted by tests, so a
  reviewer of a third-party plugin can check it by inspection.
* Outbound data has exactly one door (the webhook), one document
  ([data-policy.md](../data-policy.md)), and one test.

**Costs**

* The host cannot stop a runaway plugin, only stop waiting for it. Mitigated by
  `check_timeout()` in the contract and by the timeout being reported by name.
* `PLUGIN_API_VERSION` is a global knob: one bad removal affects everyone. Mitigated
  by the surface test and by the additive-change rule.
* `directory` winning over `entry_point` can mask an installed plugin by accident.
  Mitigated by collisions being printed by `fm plugins list` and named in the
  `--json` output.
* Two discovery mechanisms is two code paths; the shared contract test suite is what
  keeps them identical in behaviour.

**Explicitly out of scope**

* A daemon, watch mode, or scheduler inside `fm` (run it from cron or a timer).
* Sandboxing beyond read-only data access and a wall-clock budget: a plugin is
  trusted code the author wrote, not hostile code from a stranger. Publishing a
  plugin registry would require revisiting this line.
* Plugin-to-plugin dependencies, ordering, or a metric store plugins write to.
