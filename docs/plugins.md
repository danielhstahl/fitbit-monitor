# Writing plugins

A plugin is a small Python class that reads what is already in your local store and
reports two kinds of thing: **metrics** (a value over a window) and **alerts**
(something worth telling you about). fitbit-monitor handles discovery,
configuration, isolation, delivery and history; a plugin handles the question you
care about.

Everything here is local. A plugin gets a read-only view of your database and no
network client; the only outbound path in the whole program is the optional alert
webhook you configure yourself - see [data-policy.md](data-policy.md).

Related reading: [ADR-0004](adr/0004-plugin-contract.md) is the normative contract;
this document is the how-to.

---

## Five minutes to your first plugin

```bash
pip install -e .                         # or: pip install fitbit-monitor

fm dev seed --days 45                    # sample data, if you have none of your own yet
fm plugins list                          # what is installed (and what is broken)
fm plugins run --all --since 30d         # compute metrics, deliver alerts
fm alerts list --since 7d                # what has been flagged
```

Now write one:

```bash
fm plugin new resting_vs_steps --dir ~/fm-plugins
cd ~/fm-plugins/resting_vs_steps
fm plugins list --dir .                  # the project root *is* the drop-in directory
fm plugins run resting_vs_steps --since 30d --dir .
python -m pytest -q                      # the scaffolded contract suite
```

`fm plugin new` writes a project, not a loose file:

```
resting_vs_steps/
├── pyproject.toml                    # packaging + the entry-point declaration
├── README.md                         # how to run, test and ship it
├── resting_vs_steps/
│   ├── __init__.py
│   └── plugin.py                     # <- your code goes here
└── tests/
    ├── __init__.py
    └── test_resting_vs_steps.py      # the shared contract suite
```

It runs immediately as a directory drop-in, because discovery reads `<pkg>/plugin.py`
in every configured directory and `resting_vs_steps/plugin.py` is one - which is why
`--dir` points at the project root and not at its parent. The `pyproject.toml` is
there for the day you want to publish it, and `fm plugin new NAME --dir . --dry-run`
prints the file list without writing anything.

---

## The contract in one page

```python
from fitbit_monitor.plugins import MonitorPlugin, PluginContext, PluginOption


class RestingVsStepsPlugin(MonitorPlugin):
    """Resting HR measured against how much the day actually moved."""

    name = "resting_vs_steps"                       # stable id: the CLI and config key
    summary = "resting HR compared with the day's step count"
    version = "1.0.0"
    datasets = ("daily_summary",)                   # what you read; used for "no data"
    options = (
        PluginOption("min_days", "int", 4, "skip windows with fewer than this many days"),
    )

    def configure(self, config) -> None:            # optional: validate your options
        super().configure(config)
        self.min_days = config.get_int("min_days", 4)

    def run(self, ctx: PluginContext) -> None:
        rows = list(
            ctx.store.query(
                "daily_summary",
                ctx.window.since,
                ctx.window.until,
                fields=["resting_hr", "steps"],
            )
        )
        if len(rows) < self.min_days:
            ctx.log.info("only %d complete days, need %d", len(rows), self.min_days)
            return
        resting = sum(r["resting_hr"] for r in rows) / len(rows)
        steps = sum(r["steps"] for r in rows) / len(rows)
        ctx.metric(
            "resting_hr_per_10k_steps",
            round(resting / max(1.0, steps / 10_000.0), 2),
            unit="bpm per 10k steps",
            confidence=min(1.0, len(rows) / 7.0),
            details={"days": len(rows), "mean_steps": round(steps, 1)},
        )
```

Rules, all of them enforced or tested somewhere:

* `name` is a key. It appears in `--name`, in `[plugins.<name>]`, in alert
  dedup keys and in stored history. Treat changing it as a breaking change.
* `run()` reads through `ctx.store` and writes nothing. The SQLite connection
  handed to a plugin is opened `mode=ro`: an attempted write raises
  `sqlite3.OperationalError`, and a test asserts it.
* `run()` either calls `ctx.metric()` / `ctx.alert()` **or** returns the models it
  built. Not both.
* Every timestamp you pass or receive is timezone-aware and stored in UTC. Naive
  datetimes are rejected at the model boundary with a message naming the field.
* Do not call `datetime.now()` or use a random number in `run()`: use `ctx.now()`
  (the host injects a clock) and derive any variation from the data. The contract
  suite asserts a run is reproducible on the same data.
* Long scans should call `ctx.check_timeout()` in the loop - see
  [Timeouts](#timeouts).

### What a plugin receives

| | |
|---|---|
| `ctx.store` | `ReadOnlyStore`: `query`, `series`, `coverage`, `count`, `latest`, `first_ts`, `last_ts`, `datasets`, `days_with_data`, `has_any_data` |
| `ctx.config` | `PluginConfig`: your `[plugins.<name>]` section, typed accessors, nothing else from the file |
| `ctx.window` | `DateRange` - the half-open `[since, until)` UTC window for this run |
| `ctx.now()` | the host's clock, injectable for tests |
| `ctx.log` | a logger named `fitbit_monitor.plugin.<name>` |
| `ctx.metric(...)`, `ctx.alert(...)` | the only two ways out |
| `ctx.deadline`, `ctx.time_remaining()`, `ctx.check_timeout()` | the wall-clock budget |
| `ctx.api_version` | the plugin API this host serves |

`ctx.store` really is all of it: `test_context_exposes_nothing_writable` fails if
`PluginContext` grows a public member that is not on that list, so the surface
cannot widen by accident.

### The read API

```python
for point in ctx.store.query("heart_rate", ctx.window.since, ctx.window.until, fields=["hr"]):
    ...                       # point.ts is aware UTC; point["hr"], point.get("hr", 0)
```

| call | returns |
|---|---|
| `query(dataset, since, until, *, fields, order, limit)` | `SeriesPoint` iterator over `[since, until)`; `fields` drops points missing any named field |
| `series(dataset, window, **kw)` | the same, with the window already applied |
| `coverage(dataset, since, until)` | points, days expected/present, `missing_days`, `fraction` |
| `count(dataset, since, until)`, `days_with_data()` | sizes, for honest completeness reporting |
| `latest(dataset)`, `first_ts(dataset)`, `last_ts(dataset)` | edges of what is stored |
| `has_dataset(dataset)`, `datasets()` | what exists |

Two helpers turn intraday noise into a series you can aggregate:

```python
from fitbit_monitor.plugins import daily_values, downsample

by_day = daily_values(ctx.store.query("hrv", start, stop), "rmssd")     # {date: mean}
weekly = list(downsample(points, "week", how="mean", fields=["hr"]))    # Mondays, means
```

`coverage` exists so a plugin can say *four of seven days present* instead of
quietly averaging three. The built-in example plugin does exactly that, and puts
the fraction in `confidence` so a chart can show it.

### Metrics and alerts

`ctx.metric(key, value, *, unit, confidence, window_start, window_end, labels, details, summary)`

| field | notes |
|---|---|
| `key` | stable id, e.g. `resting_hr_weekly` |
| `value` | finite number or non-empty string; a `nan`/`inf` is refused at the call |
| `unit` | `bpm`, `ms`, `min` - put it in, charts depend on it |
| `confidence` | `0.0 .. 1.0` or omitted; completeness is an honest use of it |
| `window_start` / `window_end` | default to the run window; aware UTC, end not before start |
| `labels` | short, low-cardinality strings (`iso_week`, `hrv_measure`) |
| `details` | anything else worth keeping; may be empty, never holds secrets |

`ctx.alert(key, title, message, *, occurred_at, severity, confidence, evidence, dedup_key, window_start, window_end, labels, details)`

`severity` is `Severity.INFO` / `WARNING` / `CRITICAL` (strings are parsed,
`warn` and `error` are accepted as aliases). `title` is one line, because every sink
prints it inline. `evidence` is a chronological
sequence of `(timestamp, value)` pairs, `Evidence(...)` objects, or `{"at", "value"}`
mappings - out-of-order evidence is rejected, because an alert that cannot say
*when* in the right order is not evidence.

`dedup_key` decides what counts as "the same alert again" for cooldown. Leave it
out and you get `<plugin>/<key>/<severity>`; set it when a group of keys describes
one episode (say `night-2026-05-12` across three related findings).

### What an alert should say

An alert is the only place in this program where a number turns into a sentence, and a
sentence about somebody's body is the easiest thing in the world to over-claim. The rule
this codebase holds itself to, and the one `hr-anomaly` demonstrates:

* **say what it is** - a heuristic over your own stored data, not a diagnosis and not
  medical advice, and name the kind of sensor it came from. A wrist-worn consumer device
  can see a rate; it cannot say what caused it or what it means.
* **name the evidence** - the window, how many readings covered it and how much of the
  span was actually recorded, the value that crossed the bar, the *personal* level and
  threshold it was compared against, and the confidence with an explicit note about what
  that number is not (it is not a probability that anything is wrong).
* **point at the raw data** - a command that runs, so a reader can check the alert
  instead of trusting it, plus how to see what has already been flagged.
* **explain a quiet run** - every episode held back is reported, with who decided and
  why. "Nothing happened" and "something happened, here is why it stayed quiet" are
  different answers.

That is a wording budget, not a shape the contract enforces: `Alert` requires a title, a
message and a timestamp, and nothing stops a plugin writing three words. What the
contract *does* enforce is that the words are chosen by the plugin and not inherited: a
`Finding` from the detection engine arrives with no claim in it at all, so nothing can
reach your user that you did not write. The reference alerting plugin is
[`hr-anomaly`](#reference-hr-anomaly-the-built-in-alerting-example), and its own test suite
asserts this framing is present in every message it emits.

---

## Configuration

Everything a plugin needs comes from `config.toml`
(`$FM_CONFIG` or `~/.config/fitbit-monitor/config.toml`):

```toml
[store]
path = "~/.local/share/fitbit-monitor/store.sqlite3"

[plugins]
dirs = ["~/fm-plugins"]          # drop-in directories, scanned in order

[plugins.resting_trend]
enabled = true                   # false = skipped, with the reason printed
lookback_weeks = 12              # must match a declared PluginOption
hrv_field = "sdnn"

[plugins.hr-anomaly]             # the built-in alert plugin: every engine threshold,
enabled = true                   # plus its own alerting policy - see the reference below
min_sustained_minutes = 20
min_confidence = 0.3
cooldown = "36h"

[plugins.my_plugin]
threshold = 62.0

[alerts]
cooldown = "36h"                 # repeat-suppression window
jsonl = true                     # append to <data-dir>/alerts.jsonl
console = true
timeout_seconds = 120            # default wall-clock budget per plugin
[alerts.webhook]
url = "https://hooks.example.invalid/…"
enabled = false                  # the only outbound path; off unless you turn it on
```

* A plugin sees **only its own section**. `fm plugins show NAME` prints the effective
  values (only the ones that differ from the plugin's declared default), and warns
  about keys the plugin never declared (almost always a typo). `fm plugins run` names
  the same keys as `unused config keys` under the plugin that ignored them and still
  exits 0 - the plugin ran, on its defaults; `fm plugins validate` checks this without
  running anything.
* `enabled` and `timeout_seconds` are the host's own switches, valid in every
  `[plugins.<name>]` section and never reported as unused.
* Values are coerced to the declared option type before `configure()` is called; a
  bad value is reported as `MISCONFIGURED`, not as a crash, and the other plugins
  still run.
* Per-run override without editing the file:

  ```bash
  fm plugins run resting_trend --since 8w --set resting_trend.hrv_field=sdnn
  ```
* `[plugins.defaults]` applies to every plugin, which is the place to set a shared
  `timeout_seconds`.

---

## Discovery, and who wins a name clash

Two sources, in this precedence order:

| source | where it comes from | wins over |
|---|---|---|
| `directory` | every `*.py` and every `<pkg>/plugin.py` in each configured `plugins.dirs` (and `--dir`, and `$FM_PLUGIN_DIRS`) | everything |
| `entry_point` | installed distributions publishing the `fitbit_monitor.plugins` group | `builtin` |
| `builtin` | what ships with fitbit-monitor (`resting_trend`) | nothing |

Within one source, the first candidate found wins; directories are scanned in the
order they are configured. A shadowed plugin is not dropped silently - `fm plugins
list` prints the collision and which copy won. The point of letting a directory beat
an installed package is that you can develop against the name of a plugin you have
installed, without uninstalling anything.

```toml
[plugins]
dirs = ["~/fm-plugins"]     # my override wins over the installed one
```

```console
$ fm plugins list
NAME           VERSION  SOURCE     DATASETS                        ERROR
bad_idea       -        directory  -                               error(import): plugin file bad_idea.py raised RuntimeError while importing: boom
resting_trend  0.1.0    directory  daily_summary, hrv, heart_rate
  hint  bad_idea: run `python -c "import ..."` on the file to see the traceback
note: resting_trend declared twice; using /home/you/fm-plugins/resting_trend.py (shadowed fitbit_monitor.plugins.builtin.RestingTrendPlugin: directory outranks builtin)

1 plugin(s) failed to load - run `fm plugins validate` for detail
```

A plugin that failed to load still gets a row - name, source, and why - because
"which one is broken" is the only question asked here. `fm plugins list --json`
says the same thing in `plugins[].error`, with `stage`, `type`, `message` and
`hint`, and lists the directories it searched in `plugin_dirs`.

---

## Errors, isolation and exit codes

A plugin is untrusted code that happens to be written by you. The host assumes it
can fail at any point, and none of those failures is allowed to be fatal:

| failure | when | what you see |
|---|---|---|
| import error | module raises while loading | `fm plugins list` shows the plugin's own row with `error(import): …`; everything else still loads |
| registration error | not a `MonitorPlugin`, abstract, bad `name`/`version`, needs a newer API | `error(register): plugin 'x' does not implement the required interface: run` |
| config error | `configure()` rejects the section, or a value will not coerce | run reports `MISCONFIGURED`, exit 1 |
| run error | anything raised inside `run()` | `ERROR (run) - RuntimeError: boom`; the other plugins still ran |
| `sys.exit()` in a plugin | `run()` or `configure()` raises a `BaseException` | reported as that plugin's `error` like any other - a plugin cannot end the host |
| Ctrl-C | the user interrupts | re-raised, never filed as a plugin's error: an interrupt belongs to the host |
| timeout | budget exceeded | `TIMEOUT - exceeded 0.2s wall-clock budget` |

A plugin is executed on its own worker thread with a wall-clock budget
(`--timeout`, or `timeout_seconds` in the plugin's config). A plugin that never
returns is **abandoned**, not waited for: the host moves on and the thread is left
to finish as a daemon, so a hung plugin cannot hang a run. If your plugin can be
slow, cooperate:

```python
for week in weeks:
    ctx.check_timeout()        # raises PluginTimeout at the next budget breach
    ...
```

Exit codes, the same for every command:

| code | meaning |
|---|---|
| 0 | success - including "ran and found nothing" and "no data for this range" |
| 1 | a plugin errored, misconfigured itself, or timed out |
| 2 | bad arguments or unreadable config |
| 4 | no local store to read - create one with `fm dev seed` |

"No data" and "no findings" are different answers and printed differently:

```console
resting_trend: no data - nothing stored for daily_summary, hrv, heart_rate in range
silent_plugin: ok - ran, nothing to report for this range
```

A key in `[plugins.<name>]` that no option declares is named rather than swallowed,
and does not fail the run:

```console
resting_trend: ok (2 metrics)
      note: unused config keys: hrv_feild - this plugin declares no option by that name, so they were ignored
```

---

## Alerts: where they go

An alert emitted by any plugin is stored first and delivered second:

1. **history** - a row in the store's `alerts` table, queryable with `fm alerts list`;
2. **console** - printed, unless `alerts.console = false`;
3. **JSONL** - one line per alert in `<data-dir>/alerts.jsonl`, `tail -f`-able and
   easy to feed to anything else you already run;
4. **webhook** - optional, `https` only, see below.

Before any of that, the dispatcher applies **dedup and cooldown**. An alert whose
`dedup_key` was already seen inside `alerts.cooldown` (default 36h) updates the
stored row - `occurrences + 1`, `last_seen_at` moved, and never a *lower*
severity than the key has already reached - and is **not** delivered
again. One overnight episode is one notification, and the history still shows that
it recurred. Re-running the same range twice is also a no-op: an alert not *newer*
than what is stored changes nothing, so `fm plugins run` is safe to re-run, retry,
or put in cron. If the store cannot take the write at all - full disk, read-only
mount, locked by another process - `fm` logs a warning and delivers the alert
without dedup: a broken history costs repeats, not the alert and not the run.

```console
$ fm alerts list --since 7d
SEEN              SEVERITY  PLUGIN/KEY                    TITLE
2026-05-12 03:00  critical  hr-anomaly/sustained_low x3   Heart rate held below your own usual level for 55 min
2026-05-10 09:20  warning   hr-anomaly/irregular_rhythm   Beat-to-beat intervals were unusually uneven for 160 min
```

`--json`, `--severity`, `--plugin` and `--limit` are there for scripts.

### The webhook, and why it is HTTPS-only

`alerts.webhook.url` is the only place fitbit-monitor sends data off the machine.
It is off unless configured, it refuses any scheme that is not `https://`
(`alerts.webhook.allow_insecure = true`, or `--allow-insecure-webhook` for one run,
is the override and exists for a loopback test endpoint; it logs a warning when
used), it sends a body containing *only* the alert - no store paths,
no config, nothing from other plugins - and a failed POST is logged, never raised:
the alert stays in local history and the run still succeeds.

[docs/data-policy.md](data-policy.md) is the one-page statement of what leaves the
machine and what does not.

---

## Not a daemon

There is no `--watch` or `--interval` flag on `fm plugins run`, deliberately. A
daemon would own your scheduler, your logging and your restart policy; you already
have those. Run it from cron, a `systemd.timer` or a launchd agent and you get the
same dedup and cooldown, plus your existing monitoring:

```cron
15 6 * * *  fm plugins run --all --since 26h >> $HOME/.local/share/fitbit-monitor/cron.log 2>&1
```

---

## Testing a plugin

The suite the built-in plugins run in CI is published for you:

```python
from fitbit_monitor.plugins.testing import PluginContractTestCase

from resting_vs_steps.plugin import RestingVsStepsPlugin


class TestRestingVsSteps(PluginContractTestCase):
    plugin_cls = RestingVsStepsPlugin
    config_values = {"min_days": 4}
```

Inheriting it buys you, against a deterministic seeded store:

* metadata is valid (`name` shape, `version`, `summary`, option help text and types);
* the plugin configures from defaults alone and its scope is limited to its own section;
* it runs cleanly on fixture data, emits valid models, and stays inside 0..1 on confidence;
* an empty range reports "no data" instead of crashing;
* two runs on the same data give the same metrics (no clock or RNG leakage);
* the store it is handed cannot write, and `PluginContext` exposes nothing new.

Helpers for your own tests: `fixture_store()`, `fixture_window()`, `empty_window()`,
`make_config()`, `run_fixture_plugin()`. The store is the one `fm dev seed` writes -
same generator, fixed length and end date - so a failure in your CI reproduces with a
command, not a fixture file checked into a repo. `python -m pytest` and
`python -m unittest` both work; from a scaffolded project root, run either one and
both your file and the inherited suite are collected.

---

## Shipping it

Directory drop-in is fine for personal use. To publish, declare the entry point
(`fm plugin new` already wrote it, in the layout setuptools expects):

```toml
[project]
dependencies = ["fitbit-monitor"]

[project.entry-points."fitbit_monitor.plugins"]
resting_vs_steps = "resting_vs_steps.plugin:RestingVsStepsPlugin"

[tool.setuptools]
packages = ["resting_vs_steps"]
```

The entry-point value is `<package>.<module>:<Class>`, and `[tool.setuptools]
packages` has to name the directory that holds it - a build fails with
`package directory 'resting_vs_steps' does not exist` when an editor or a refactor
moves the package out from under it. `pip install -e .` (or a wheel from
`python -m build`) and `fm plugins list` finds it with no configuration and no
`--dir` - which is also how a plugin gets to live in its own repository:

```console
$ fm plugins list
NAME           VERSION  SOURCE       DATASETS
my_metric      0.1.0    entry_point  daily_summary
resting_trend  0.1.0    builtin      daily_summary, hrv, heart_rate
```

One caveat on `[project] dependencies`: fitbit-monitor is not on PyPI yet, so the
scaffold ships that list empty and tells you to install the host from the checkout
(`pip install /path/to/fitbit-monitor`) until it is.

Versioning: `PLUGIN_API_VERSION` is bumped when the contract changes incompatibly.
A plugin declares what it needs with `requires_api`; a host older than that refuses
the plugin at discovery with a named error instead of half-running it, and a host
newer than the plugin's version runs it unchanged. Within one API version, the host
may add to the context (a test guards unintended additions) but will not remove or
re-sign anything.

---

## Reference: `resting_trend`, the built-in metric example

The plugin that ships with fitbit-monitor, and the one the docs and CI treat as the
reference for **metrics**: weekly resting heart rate and HRV, with week-over-week and
4-week deltas, and explicit completeness reporting.

```console
$ fm plugins show resting_trend
resting_trend 0.1.0
  weekly resting heart rate and HRV trend, with WoW and 4-week deltas
  source:     builtin (fitbit_monitor.plugins.builtin.RestingTrendPlugin)
  datasets:   daily_summary, hrv, heart_rate
  plugin api: 1
  enabled:    yes
  options:
    lookback_weeks       int      number of ISO weeks to report [default: 8]
    min_days_per_week    int      weeks with fewer days present than this are reported as incomplete [default: 4]
    hrv_field            str      which HRV measure to report [default: rmssd]
    resting_source       str      where resting HR comes from: a stored daily value, or the quiet overnight window [default: auto]
  config:     every option at its declared default

$ fm plugins run resting_trend --since 5w --json | python -m json.tool | head -30
```

It is short enough to read in one sitting:
[`src/fitbit_monitor/plugins/builtin/resting_trend.py`](../src/fitbit_monitor/plugins/builtin/resting_trend.py).

---

## Reference: `hr-anomaly`, the built-in alerting example

The other half of the contract: a plugin that decides *something is worth telling you
about*. It reads your stored heart rate, hands it to the detection engine
([docs/detection.md](detection.md), which owns every threshold and every statistic), and
turns the findings into alerts the host delivers through the sinks above. The engine
produces a measurement with no claim in it; this plugin is where the claim gets made,
which is exactly why it lives in a plugin.

```console
$ fm plugins show hr-anomaly
hr-anomaly 0.1.0
  alerts when your heart rate strays from your own usual level, with the readings attached
  source:     builtin (fitbit_monitor.plugins.builtin.HrAnomalyPlugin)
  datasets:   heart_rate
  plugin api: 1
  enabled:    yes
  options:
    min_baseline_days    int      resting days needed before a finding may claim full confidence [default: 7]
    z_high               float    robust z-score above the personal resting level that counts as sustained-high [default: 4.0]
    min_sustained_minutes float    how long a rate must stay past its threshold to be a finding [default: 20.0]
    ...                            (30 more: every field of AnomalyConfig, at its engine default)
    min_confidence       float    findings below this confidence are reported as suppressed instead of alerted on [default: 0.3]
    cooldown             duration two episodes of the same rule and context this close together are one alert in one run [default: 36h]
    max_alerts           int      most alerts one run emits; the rest are reported as suppressed, least significant first [default: 10]
    min_window_coverage  float    fraction of the run's days that must have heart-rate data, below it nothing is alerted on [default: 0.5]
  config:     every option at its declared default

$ fm plugins run --name hr-anomaly --since 20d --no-deliver
ran 1 plugin(s) over 2026-11-03T16:40:34+00:00 .. 2026-11-23T16:40:34+00:00
  hr-anomaly: ok (6 metrics, 1 alert)
      hr_anomaly_data_coverage = 1 fraction of days
      hr_anomaly_baseline = 60.4 bpm
      hr_anomaly_baseline = 59.2 bpm
      hr_anomaly_findings = 1 findings
      hr_anomaly_alerts = 1 alerts
      hr_anomaly_suppressed = 11 episodes
      [CRITICAL] hr-anomaly: Beat-to-beat intervals were unusually uneven for 170 min (confidence 0.92) @ 2026-11-16 04:45 UTC /     Your beat-to-beat interval was far more uneven than your own ordinary unevenness for 2026-11-16 04:45 to 07:35 UTC while you were at rest (170 min, 21 readings, 97% of that span actually recorded): the scatter index reached 0.81 against your usual 0.06, past a bar of 0.15 (31.2 times your usual spread). Confidence 0.92 says how far those readings sat from your own ordinary spread and how complete the recording was (the personal baseline behind it is 21 days of data) - it is not a probability that anything is wrong, and it is not comparable between people. This is a heuristic pattern in your own recorded data, not a diagnosis and not medical advice: a wrist-worn consumer sensor can see what your rate did, it cannot tell you what caused it or what it means. To check it yourself: fm store export --dataset heart_rate --since 2026-11-16T04:35:00+00:00 --until 2026-11-16T07:45:00+00:00 --format csv prints the readings behind this window, and `fm alerts list --plugin hr-anomaly` lists every episode already flagged.
summary: 1 ok; exit 0
```

(`--no-deliver` computes and prints without touching the sinks; without it the same alert
is stored in history, printed by the console sink and appended to the JSONL file.)

Read that alert message as the checklist in [What an alert should
say](#what-an-alert-should-say) turned into prose: the window, how many readings covered
it, the value that crossed the bar, the wearer's own level and threshold, a confidence
with an explicit statement of what it is *not*, the framing sentence about the sensor and
the heuristic, and a command that prints the readings. Note what it does **not** say: no
condition, no severity beyond the sink's own `critical`/`warning`/`info`, no advice.

The four metrics are the other half of the honesty. A run that raised one alert says so,
and a run that raised none says which of four things happened:

| metric | the question it answers |
|---|---|
| `hr_anomaly_data_coverage` | how much of this window was actually recorded (`too_thin_to_alert` when it declined to look) |
| `hr_anomaly_baseline` | what every threshold was built from, per context, with `state` = `established` / `provisional` / `refused` |
| `hr_anomaly_findings` | what crossed a rule, and `alerted: true/false` for what the plugin did about each |
| `hr_anomaly_suppressed` | what did not become an alert, `by_code`, with the reason and who decided (`engine` or `hr-anomaly`) |

`--json` carries all of it, including the rules that never ran:

```console
$ fm plugins run --name hr-anomaly --since 20d --json | python -m json.tool
... (outcomes -> metrics, the hr_anomaly_suppressed entry)
        {
          "key": "hr_anomaly_suppressed",
          "value": 11.0,
          "summary": "11 candidate episode(s) did not become alerts",
          "details": {
            "by_code": {"engine_gate": 11},
            "by_source": {"engine": 11},
            "listed": 11,
            "episodes": [
              {
                "decided_by": "engine",
                "code": "engine_gate",
                "type": "sustained_high",
                "context": "rest",
                "onset": "2026-11-04T01:20:00+00:00",
                "offset": "2026-11-04T01:55:00+00:00",
                "duration_minutes": 35.0,
                "observed": 148.2,
                "reason": "148 bpm sustained for 35 min inside an exercise span: attributed to activity rather than reported"
              }
            ],
            "rules_that_did_not_run": [
              {"rule": "sustained_high", "context": "rest",
               "reason": "11 sustained stretch(es) above the high threshold (45 min longest) fell inside an exercise span and were attributed to activity, not reported"}
            ],
            "policy": {"min_confidence": 0.3, "cooldown": "36h", "max_alerts": 10,
                       "min_window_coverage": 0.5, ...}
          }
        }
```

So a run that sent nothing can still be read: `by_code` counts the reasons, `by_source`
says who decided (the engine, or this plugin's own policy), `episodes` lists every one
of them in chronological order with the numbers that decided it, and
`rules_that_did_not_run` names the rules that had no baseline to be measured against -
which is the answer to "did it look, or did it not look?".

And a window too thin to mean anything alerts on nothing - while still saying so, in the
text report as well as in `--json`:

```console
$ fm plugins run hr-anomaly --since 45d   # with [plugins.hr-anomaly] min_window_coverage = 0.99
ran 1 plugin(s) over 2026-10-09T16:40:56+00:00 .. 2026-11-23T16:40:56+00:00
  hr-anomaly: ok (5 metrics)
      hr_anomaly_declined = only 43 of 45 days in [2026-10-09T16:40:56+00:00, 2026-11-23T16:40:56+00:00) had heart_rate readings (96%), below this plugin's min_window_coverage of 0.99, so nothing was analysed and nothing was alerted on. A watch left on a nightstand is not a quiet heart: widen --since or store more days.
      hr_anomaly_data_coverage = 0.96 fraction of days
      hr_anomaly_suppressed = 1 episodes
      hr_anomaly_findings = 0 findings
      hr_anomaly_alerts = 0 alerts
summary: 1 ok; exit 0
```

Its configuration is the engine's configuration, plus a small alerting policy of its own
(a full annotated list of the thresholds is in [docs/detection.md](detection.md)):

```toml
[plugins.hr-anomaly]
enabled = true
time_zone = "Europe/Berlin"       # day boundaries and episode anchors, so a night is one day
min_baseline_days = 7             # before this, findings are provisional and capped
min_sustained_minutes = 20        # the "a single spike must not fire" knob
z_high = 4.0                      # robust z above the wearer's own level
min_window_coverage = 0.5         # below this fraction of days recorded, decline and say so
min_confidence = 0.3              # weaker findings are reported as suppressed, not alerted
cooldown = "36h"                  # two episodes of one rule this close are one alert in one run
max_alerts = 10                   # a 90-day scan should not send 40 notifications
```

`cooldown` here is not `alerts.cooldown`: this plugin's collapses repeats *inside one
scan*, and the sink's `alerts.cooldown` decides repeats *across runs*. Neither is a claim
that an episode stopped happening.

The source is the long version of everything above, with the reasoning next to each
decision:
[`src/fitbit_monitor/plugins/builtin/hr_anomaly.py`](../src/fitbit_monitor/plugins/builtin/hr_anomaly.py).

---

## API reference

Generated from the docstrings by `scripts/docs/api_reference.py`; CI regenerates and
compares it, so this section cannot drift.

<!-- BEGIN GENERATED API REFERENCE -->

Constants: `PLUGIN_API_VERSION = 1`, minimum accepted `1`, entry-point group `fitbit_monitor.plugins`.

### `MonitorPlugin` - subclass this
`class MonitorPlugin()`

Base class for every plugin, built-in or third-party

Members:
  - `configure(self, config: PluginConfig)` - Validate and cache options. May raise PluginConfigError
  - `run(self, ctx: PluginContext) -> Iterable[Any] | None` - Compute metrics and alerts over `ctx.window`

Class attributes a plugin sets: `name`, `summary`, `version`, `datasets`, `requires_api`, `options`.

### `PluginContext` - what run() receives
`class PluginContext(*, plugin_name: str, store: ReadOnlyStore, config: PluginConfig | None = None, window: DateRange, clock: Clock | None = None, log: logging.LoggerAdapter | None = None, api_version: int = 1, deadline: datetime | None = None, cancel: threading.Event | None = None)`

The read-only world a plugin runs inside. Built by the host, never by a plugin

Members:
  - `now(self) -> datetime` - The host's clock. Never call `datetime.now()` in a plugin: tests inject this
  - `request_cancel(self)` - Host-side only: ask a running plugin to stop at its next checkpoint
  - `check_timeout(self)` - Cooperative checkpoint for long scans. Raises PluginTimeout
  - `metric(self, key: str, value: float | int | str, *, unit: str | None = None, confidence: float | None = None, window_start: datetime | None = None, window_end: datetime | None = None, labels: Mapping[str, str] | None = None, details: Mapping[str, Any] | None = None, summary: str | None = None) -> MetricResult` - Record a computed value. The run window and plugin name are filled in
  - `alert(self, key: str, title: str, message: str, *, occurred_at: datetime, severity: Severity | str = <Severity.WARNING: warning>, confidence: float | None = None, evidence: Iterable[Evidence] | None = None, window_start: datetime | None = None, window_end: datetime | None = None, dedup_key: str | None = None, labels: Mapping[str, str] | None = None, details: Mapping[str, Any] | None = None) -> Alert` - Raise an alert. Delivered to the configured sinks by the host
  - `adopt(self, results: Iterable[Any])` - Take ownership of models a `run()` returned instead of emitting

### `PluginOption` - declare a config option
`class PluginOption(name: str, type: str = float, default: Any = None, help: str = , required: bool = False, choices: tuple[Any, ...] = ())`

A declared option: the plugin's config schema, visible to `fm plugins show`

Members:
  - `coerce(self, value: Any) -> Any` - Cast a TOML value to the declared type; raise on nonsense

Declared types: `int`, `float`, `bool`, `str`, `duration`, `list`.

### `MetricResult` - a value a plugin computed
`class MetricResult(plugin: str, key: str, value: float | int | str, window_start: datetime, window_end: datetime, unit: str | None = None, confidence: float | None = None, labels: Mapping[str, str] = <factory>, details: Mapping[str, Any] = <factory>, produced_at: datetime | None = None, summary: str | None = None)`

A value a plugin computed over a window: the "metrics" half of the contract

### `Alert` - something the user should hear about
`class Alert(plugin: str, key: str, title: str, message: str, occurred_at: datetime, severity: Severity = <Severity.WARNING: warning>, confidence: float | None = None, evidence: tuple[Evidence, ...] = (), window_start: datetime | None = None, window_end: datetime | None = None, dedup_key: str | None = None, labels: Mapping[str, str] = <factory>, details: Mapping[str, Any] = <factory>, alert_id: int | None = None, first_emitted_at: datetime | None = None, last_seen_at: datetime | None = None, occurrences: int = 1)`

Something a plugin wants the user to know about: the "alerts" half

Members:
  - `effective_dedup_key` - What the sinks treat as "the same alert" for cooldown purposes
  - `rendered(self) -> str` - The one-line human form printed by the console sink
  - `as_dict(self, *, full: bool = True) -> dict[str, Any]` - `full=False` is the webhook body: the alert itself, nothing else

### `Evidence` - one data point quoted by an alert
`class Evidence(at: datetime, value: float, dataset: str | None = None, detail: str | None = None)`

One data point quoted by an alert: *when*, *what value*, *from what*

### `Severity` - alert severity
`class Severity(*values)`

Alert severity. Three levels, parsed from the usual aliases

Values: `'info'`, `'warning'`, `'critical'`.

### `PluginError` - base class for plugin failures
`PluginError(message: str, *, plugin: str | None = None, hint: str | None = None) *(error)*`

Base class for plugin failures. Carries the plugin name and the stage

### `PluginRegistrationError` - object is not a usable plugin
`PluginRegistrationError(message: str, *, plugin: str | None = None, hint: str | None = None) *(error)*`

The object is not a usable plugin: missing members, bad name, unknown API

### `PluginImportError` - the module raised on import
`PluginImportError(message: str, *, plugin: str | None = None, hint: str | None = None) *(error)*`

The module raised while being imported

### `PluginConfigError` - the plugin rejected its options
`PluginConfigError(message: str, *, plugin: str | None = None, hint: str | None = None) *(error)*`

The plugin rejected its own `[plugins.<name>]` section

### `PluginRuntimeError` - run() raised
`PluginRuntimeError(message, *, plugin=None, hint=None, cause: BaseException | None = None) *(error)*`

The plugin's `run()` raised. Other plugins still get their turn

### `PluginTimeout` - wall-clock budget exhausted
`PluginTimeout(message, *, plugin=None, hint=None, cause: BaseException | None = None) *(error)*`

The plugin exceeded its wall-clock budget and was abandoned

### `ReadOnlyStore` - what `ctx.store` is: the whole read API
`class ReadOnlyStore(path: Path, connection: sqlite3.Connection | None = None)`

Read-only view of the local store. The plugin's only window onto data

Members:
  - `query(self, dataset: str, since: datetime | None = None, until: datetime | None = None, *, fields: Sequence[str] | None = None, order: str = asc, limit: int | None = None) -> Iterator[SeriesPoint]` - Iterate points of `dataset` in `[since, until)`
  - `series(self, dataset: str, window: DateRange, **kw) -> Iterator[SeriesPoint]` - `query` with the run window already applied
  - `coverage(self, dataset: str, since: datetime, until: datetime) -> Coverage` - Point count plus which calendar days are present in `[since, until)`
  - `days_with_data(self, dataset: str | None = None) -> list[date]` - Distinct calendar days that have at least one stored point
  - `has_any_data(self, datasets: Sequence[str], window: DateRange) -> bool` - False when *nothing* is stored for the window - a run says so out loud
  - `day(self, dataset: str, day: date | datetime | str) -> list[SeriesPoint]` - Every point of `dataset` on one calendar day (UTC), oldest first
  - `describe(self, dataset: str) -> catalog.DatasetSpec` - Kind, grain, fields and units of one dataset (see :mod:`.datasets`)
  - `units(self, dataset: str) -> dict[str, str]` - `{field: unit}` for a dataset - the units a report must print
  - `grain_seconds(self, dataset: str) -> int | None` - Native spacing of a dataset, or `None` when fm does not know it
  - `gaps(self, dataset: str, since: datetime, until: datetime, *, max_gap_seconds: float | None = None, tolerance: float = 3) -> tuple[Gap, ...]` - Breaks in a series inside `[since, until)`, longest break first
  - `completeness(self, dataset: str, since: datetime, until: datetime, *, max_gap_seconds: float | None = None) -> Completeness` - Coverage and gaps together - the answer before computing a metric on a window
  - `stored_datasets(self) -> tuple[DatasetInfo, ...]` - Every registered dataset with its extent and its cost on disk
  - `iter_raw(self, **filters) -> Iterator[RawPayload]` - Stored payloads, oldest fetch first. See fitbit_monitor.store.raw.iter_payloads
  - `raw_payload(self, content_hash: str, *, with_body: bool = True) -> RawPayload | None` - One stored payload by content hash, bytes included
  - `raw_for(self, dataset: str, ts: datetime) -> RawPayload | None` - The most recent payload fetched for a dataset at or before `ts`
  - `raw_totals(self, dataset: str | None = None) -> dict[str, dict[str, float]]` - Payload counts and bytes per dataset (`"all"` included)
  - `assert_read_only(self)` - Raise if this handle could write. Cheap, and used by the contract test suite

### `SeriesPoint` - one stored sample, with units
`class SeriesPoint(ts: datetime, values: Mapping[str, float], dataset: str = )`

One stored sample: an instant plus the numeric fields recorded at it

Members:
  - `unit(self, name: str) -> str | None` - The unit of one field, or `None` when the catalog does not name it
  - `items_with_units(self) -> tuple[tuple[str, float, str | None], ...]` - `(field, value, unit)` triples - what a report prints without guessing

### `Coverage` - how complete a window is
`class Coverage(dataset: str, since: datetime, until: datetime, points: int, days_expected: int, days_present: int, missing_days: tuple[date, ...] = (), first_ts: datetime | None = None, last_ts: datetime | None = None)`

How complete a dataset is over a window - plugins report this honestly

### `Completeness` - coverage plus the gaps behind it
`class Completeness(coverage: Coverage, gaps: tuple[Gap, ...] = (), max_gap_seconds: float | None = None)`

Coverage plus the gaps behind it: "is this window safe to compute on?"

### `DatasetInfo` - one dataset as stored
`class DatasetInfo(name: str, kind: str = daily, granularity_seconds: int | None = None, points: int = 0, first_ts: datetime | None = None, last_ts: datetime | None = None, days_present: int = 0, payload_count: int = 0, payload_bytes: int = 0, stored_bytes: int = 0)`

One dataset as stored: shape, extent, and what it costs on disk

### `Gap` - a break in a series
`class Gap(start: datetime, end: datetime, seconds: float)`

A break in a series longer than the dataset's own grain allows

### `run_plugins()` - run plugins over a window

`run_plugins(registry: PluginRegistry, store: ReadOnlyStore, window: DateRange, *, config: AppConfig | None = None, names: Sequence[str] | None = None, timeout: float = 120.0, dispatcher: AlertDispatcher | None = None, clock: Clock | None = None) -> RunReport`

Run `names` (or every plugin) over `window`. Never raises for plugin reasons

### `validate_plugin_class()` - check a candidate class

`validate_plugin_class(candidate: Any, *, source: str = ) -> type[MonitorPlugin]`

Raise PluginRegistrationError unless `candidate` is a usable plugin class

### `downsample()` - bucket points by hour/day/week

`downsample(points: Iterable[SeriesPoint], period: str = day, *, how: str = mean, fields: Sequence[str] | None = None) -> Iterator[SeriesPoint]`

Bucket points by `period` and aggregate each numeric field with `how`

### `daily_values()` - {date: value} for one field

`daily_values(points: Iterable[SeriesPoint], field: str, *, how: str = mean) -> dict[date, float]`

Convenience: `{date: aggregated field}` for one field. Days with no value are absent

### `aggregate()` - collapse a list of numbers

`aggregate(values: Sequence[float], how: str = mean) -> float | None`

Collapse `values` with `how`. Returns `None` for an empty sequence

### `PLUGIN_API_VERSION` - the contract version this host serves

`1`

### `MIN_PLUGIN_API_VERSION` - oldest plugin API still accepted

`1`

### `ENTRY_POINT_GROUP` - the entry-point group plugins publish into

`'fitbit_monitor.plugins'`

<!-- END GENERATED API REFERENCE -->

---

## Checklist before you ship a plugin

- [ ] `name` is stable, lowercase, and unique among your plugins
- [ ] `summary` is one line a human will read in `fm plugins list`
- [ ] `datasets` lists every dataset you read - it is how "no data for this range" is answered without running you
- [ ] every option is declared, typed and documented, and `configure()` rejects nonsense
- [ ] timestamps are aware UTC; evidence is chronological
- [ ] `confidence` means something (completeness is a legitimate meaning) and never exceeds 1.0
- [ ] no writes, no network, no `datetime.now()`, no RNG that is not seeded from the data
- [ ] `ctx.check_timeout()` inside any loop that can be long
- [ ] `dedup_key` chosen so a recurring episode collapses to one notification
- [ ] `PluginContractTestCase` subclass in your repo, and it passes
