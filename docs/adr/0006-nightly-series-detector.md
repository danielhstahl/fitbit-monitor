# ADR-0006: a nightly series gets its own detector

- **Status:** accepted
- **Date:** 2026-11-24
- **Implements:** `fitbit_monitor.anomaly.hrv` (the detector, its baseline and its rules), `fitbit_monitor.plugins.builtin.hrv_anomaly` (`hrv-anomaly`), [docs/detection.md](../detection.md) §"Nightly series: `hrv-anomaly`"
- **Related:** ADR-0004 (plugin contract - this is a second *shape of data* through the same contract), ADR-0003 (the `hrv` dataset and what a nightly row is), [docs/detection.md](../detection.md) (the intraday engine this deliberately does not reuse)

## Context

The request was small and reasonable: *"write another plugin for anomaly on hrv, similar to
heart rate."* The existing anomaly engine is `fitbit_monitor.anomaly`, it detects in
**minutes**, and `hr-anomaly` is built on it. Pointing it at `dataset = "hrv"` looked like a
config change. It was not one, and the reason is worth writing down because it is the kind
of reuse that makes a codebase lie.

Every rule in the intraday engine is stated in observed minutes and every confidence is
built partly out of them:

* `min_sustained_minutes` - how long a rate has to stay unusual. A nightly value is one
  sample. "Sustained for 1 440 minutes" is a rule with no referent, and a minutes floor
  cannot filter a single measurement the way it filters one noisy minute in an hour of
  readings.
* contexts (`rest` / `sleep` / `exercise`) and the exercise mask - which require the
  intraday series and the sleep-session overlay. A night is not a context, and the
  question "was she exercising at 03:00?" is not answerable from `hrv.rmssd`.
* the scatter index - the coefficient of variation of beat-to-beat intervals, derived from
  intraday heart rate. **RMSSD already *is* a scatter statistic**; an index built on it
  would be noise squared, dressed as corroboration.
* episode coverage - "what fraction of the claimed duration was actually recorded". One
  value per day has no duration to be incomplete inside.

Then there is a second problem, which would have survived any amount of reconfiguration:
**a nightly dip is measured in the same unit as its own baseline.** An intraday episode
lasts an hour against a baseline of 28 days of day-medians, so it barely moves the bar it
is judged against. A run of low HRV nights is a fortnight against a bar of 28 nights -
the same order of magnitude. A conventional trailing baseline does not just blur that dip,
it adopts it: the median falls, the MAD widens, and the longer and more interesting the dip,
the more ordinary it looks. That is a silent failure in exactly the case that matters, and
no amount of threshold tuning fixes it.

What was on the table:

1. **Point the intraday engine at `hrv`** with a `dataset` override. Rejected: every knob
   above would have to be answered with a zero or a lie, and the option surface would grow
   a product of two shapes of data - `min_sustained_minutes` alongside
   `min_consecutive_nights`, one config, half of it meaningless for either caller.
2. **Upsample the nightly value into a fake intraday series** so the existing engine can
   read it. Rejected hardest: it manufactures evidence the wearer never recorded, and a
   rule that "sustains" across a value copied across a day would report minutes of
   evidence that are one measurement wearing a disguise. The engine's evidence list is the
   part a reader checks; faking it would rot the one thing this project promises.
3. **A second detector that shares the primitives, not the engine.** Accepted.

The inherited non-goals still apply and are why this ADR exists at all: heuristics over your
own data, never a diagnosis, never a population reference range, every claim auditable down
to the numbers that made it (see [docs/data-policy.md](../data-policy.md)).

## Decision

### 1. A second detector module, sharing primitives rather than the engine

`fitbit_monitor.anomaly.hrv` holds the nightly statistics and rules;
`fitbit_monitor.plugins.builtin.hrv_anomaly` holds only the alerting policy and the prose.
What is shared with `fitbit_monitor.anomaly` is what is genuinely shape-independent:
`robust_sigma`, the confidence shape (a product of factors in `0..1`, ceiling below 1.0,
every factor reported), the provisional-baseline cap, the option-generation idiom
(`option_specs()` on the config dataclass, so advertised options and run thresholds cannot
drift), and the prose conventions.

The intraday engine is **not** given a nightly mode. `HrvAnomalyConfig` is a separate
dataclass with nightly names, because a rule that does not exist should not have an option.

### 2. The bar is your own median and MAD of `log(value)`

HRV moves multiplicatively: 60 → 45 ms is the same event as 40 → 30 ms, and a fixed
millisecond drop means more to a 70 ms wearer than to a 35 ms one. Working in logs makes
one sigma a *percentage* of the wearer's own median. Two absolute floors sit under it -
`min_low_delta_ms` and `min_spread_ms`, converted into log space rather than compared
afterwards - so a near-constant recording (coarse import, a device that rounds, five days
of history) cannot have its own rounding error called a finding.

### 3. Two guards against a baseline eating itself

These are the design, and a test exists for each:

* **A night that breaks the bar does not join the pool the bar is built from.** The bar
  means "what have my ordinary nights looked like"; a dip that votes on its own baseline
  widens and lowers it, so a fortnight of low HRV redefines *low* and then reports nothing.
  Flagged nights are held aside, and a run is judged against the bar standing when the run
  began - at the strength of its first night, not an average dragged down by its own length.
* **The window is read with `baseline_nights` of lead-in.** Judging May on May's history
  spends most of May with no bar at all, which is a silent no. Same reasoning as the
  intraday detector's `lead_in_days`.

### 4. Three rules, one of them off by default

| id | rule | default |
| --- | --- | --- |
| `hrv_low` | `min_consecutive_nights` nights at or below `z_low` sigmas under the median, or a single night past `deep_dip_z` | 2 nights, 3.0, 4.5 |
| `hrv_high` | the mirror above the median | `alert_on_high = false` |
| corroboration | resting HR over the same nights, against its own prior nights at `resting_hr_z` | `corroborate = true`, 2.0 |

**`hrv_high` ships off.** Unusually high HRV is not a warning sign, and an alert that fires
on good news trains its reader to ignore the alerts that are not. The rule exists because
some people want it; it is opt-in.

**Corroboration is never a gate and never a cause.** An HRV dip with a rising resting rate
is a stronger *description* of the same fortnight, not a different diagnosis: it adds a
little confidence and a sentence naming the numbers it used. A store with no
`daily_summary` rows is not evidence of health, so its absence is reported as an absence of
evidence - `nothing to corroborate with, which is not a contradiction` - and the HRV finding
stands alone.

### 5. Early history earns a weaker sentence, not silence

Below `min_baseline_nights` a bar is built anyway, flagged provisional, capped at
`provisional_confidence_cap`, and its alerts can never leave informational severity:
"unusual for you" after four nights is a weaker sentence than after four weeks, but it is a
sentence. Below `provisional_min_nights` the rules refuse to run and say so, because below
five nights a single strange night *is* the wearer's median.

Two data-quality rules ride along, both reported rather than applied quietly: a night with
no value is not a low night (it is a watch on the nightstand), and a stored non-positive
value is a broken row, not the most alarming night of the year.

### 6. The field is chosen and the choice is printed

`field = "auto"` walks `rmssd`, `deep_sleep_rmssd`, `sdnn`: sources disagree about what HRV
even is (a Takeout archive carries RMSSD and nothing else, other pipelines publish SDNN),
and a detector that reports nothing because it asked for the column this wearer's device
never wrote is the worst kind of quiet. Whichever one is used is named in the report, along
with the ones that were empty.

### 7. The plugin reuses `hr-anomaly`'s shared wording rather than inventing its own

The thin-window sentence (`thin_window_reason`), the option readers (`_fraction`,
`_duration`) and the held-back record (`HeldBack`) are imported from
`plugins/builtin/hr_anomaly.py`, so two built-ins do not explain the same shortage in two
voices. They are module-private today; a third caller promotes them to a shared module. The
framing sentence is deliberately **not** shared - "what your rate did" is not what an HRV
alert saw, and a disclaimer that does not describe its own signal is decoration.

## Consequences

**What this buys.** Two shipped anomaly plugins, over two shapes of data, through one plugin
contract: `hr-anomaly` remains the copy-me example for intraday, `hrv-anomaly` is the
example for any once-a-day series (nightly weight, daily SpO2, weekly anything) and carries
no intraday vocabulary it cannot honour. Each rule's name says what it checks. The intraday
engine stays honest about needing minutes.

**What it costs.** There are now two places to look when a night goes unreported. The
mitigation is that both modules are small, both report their own suppressions with the knob
that would have changed the answer, and both are documented in one file
([docs/detection.md](../detection.md)).

**What is still open, stated plainly.** The intraday engine is measured against a labelled
fixture corpus with CI floors
(`tests/test_anomaly_accuracy_gate.py`, `tests/fixtures/anomaly/hr_anomaly_labels.json`,
documented in docs/detection.md §"The accuracy gate: measured on labelled data").
**The nightly detector has no such gate yet.** Its defaults were tuned against the seeded
corpus's own distribution (one sigma ≈ 9.4% of the median; most extreme ordinary night
-2.8σ over 80 judgeable nights; zero findings) and against hand-planted scenarios in
`tests/test_builtin_hrv_anomaly.py`, which proves the arithmetic, the policy and the
plumbing - **not** sensitivity, because nobody has labelled a real wearer's nights. Closing
it needs a labelled nightly corpus (real nights marked low/ordinary/ill from an account that
will share them, or a synthetic corpus whose dips are labelled by construction) and then
the same kind of floors. Until that exists, the honest reading of a nightly HRV number here
is descriptive: it says how a night sat against your own others, and nothing about whether
it was good for you.

**What a wrist wearable cannot fix.** Nightly HRV from a consumer band is an estimate, the
three candidate fields are not interchangeable measures, and corroboration by resting HR -
itself an estimate - cannot promote one into the other. The plugin's confidence ceiling is
below 1.0 for reasons that have nothing to do with statistics.
