# Anomaly detection

`fitbit_monitor.anomaly` answers one question about your own heart-rate history: **which
stretches of it are far from your own ordinary values, for long enough to be worth a look?**
It is a library, not a diagnosis. It does not know what any rhythm *means*, it has no
threshold in bpm that claims to be normal for a human being, and it never produces a sentence
about your health - that is a plugin's job, and the plugin that owns the user relationship is
the right place for it. What this library produces is an auditable measurement: an onset, an
offset, a duration, the personal level it was compared against, the spread that bar was built
from, the evidence series, and a confidence that says how much the engine is allowed to
believe itself.

* The feature layer it reads: `fitbit_monitor.hr` - epoch series, resting and sleep windows,
  the exercise mask, and interval-derived scatter. `fitbit_monitor.hr.windows` and
  `fitbit_monitor.hr.daily` are the two this engine leans on most, and their thresholds are
  stated in their own docstrings.
* Alerts, dedup and cooldowns: [docs/plugins.md](plugins.md).
* The synthetic corpus the numbers below were measured on: `fm dev seed`
  ([docs/data-import.md](data-import.md)).

```
  ReadOnlyStore
        |
        v
  epoch_series ......... one uniform series, gaps and imputation flags made explicit
        |
        v
  day_features ......... per local day: sleep window, resting window, exercise spans
        |
        v
  contexts ............. rest (day - nights - exercise)  |  sleep (the night's window)
        |               each split into a baseline set and a wider detection domain
        v
  baselines ............ median level, MAD spread, thresholds - or a refusal, with a reason
        |
        v
  rules ................ sustained high / sustained low / irregular scatter
        |
        v
  merge, gate, score ... one episode, its suppression, or its confidence
        |
        v
  Finding | SuppressedEpisode | RuleNote  ->  a plugin, an alert sink, a report
```

## What a baseline is, and what it refuses to be

Every bar in this library is a distance from the wearer's own distribution, so the baseline
carries three statistics per context, not one:

| statistic | what it measures | why it is that one |
| --- | --- | --- |
| level | the median of the per-day means of the context window | a mean is dragged by the episodes this engine looks for; the median is not |
| quiet spread | day-to-day spread and the *within-window* spread, in quadrature | the yardstick for "lower than this person ever goes" |
| rule spread | the same, but with the **detection domain's** spread instead of the quiet window's | the yardstick for "higher than usual": an awake day swings tens of bpm, and a bar measured on a quiet window would call a brisk walk abnormal |

The two spreads are deliberately different. Being active raises a heart rate and never lowers
one, so measuring "too low" against the swing of an awake day would push the low threshold
below anything short of an emergency and make the bradycardia half of the rule decorative.
Each is also floored (`min_spread_bpm`, `min_spread_index`): a spread measured as zero would
turn `level + z * spread` into `level`, and then every reading above the wearer's own mean
would be an episode.

A baseline can also refuse. Below `provisional_min_days` usable days there is no level, and
the engine emits a `RuleNote` saying how many days it had and how many it needed. Between that
and `min_baseline_days` the baseline is *provisional*: findings are allowed, but capped at
`provisional_confidence_cap`, because "unusual for you" is a different claim on five days of
data than on thirty. `days` and `days_scanned` are both reported - "3 of 30 days had a usable
resting window" and "3 of 3" are different statements about the same finding.

## Two contexts, two domains

The window a baseline is built from is the *quietest* sustained stretch the feature layer
found. The set a rule scans is wider, because the episode a rule is looking for is by
definition not the quiet part:

* **`rest`** - baseline: the day's resting window, exercise subtracted. Detection: the whole
  local day minus both nights minus every exercise span. The bar it has to clear is derived
  from that wider set's own spread, which is what stops the wider net crying wolf.
* **`sleep`** - baseline and detection: the night's window, with **no** exercise subtraction.
  A wearer who is asleep is not exercising, and a rate that would read as a workout at noon is
  exactly the finding worth making at 03:00.

Both domains end at a *detected* window, and a detected window is a function of the data. Two
consequences are documented in `anomaly/context.py` rather than hidden, because they are the
sharp edges of this design:

1. A sustained stretch above the night's level is an excursion the window's growth gives up
   on, so the episode lands outside the night - where the resting rules see it. The same
   episode is therefore often reported in the resting context, and `dedup_key` carries the
   context so that is visible rather than confusing.
2. A stretch *below* the night is the quietest thing there, so it becomes the window and
   pushes the rest of that night out into the resting domain - where an ordinary 55 bpm
   against a 75 bpm resting level reads as a seven-hour bradycardia that never happened. The
   engine refuses that one: a low stretch that overlaps the region the wearer's night was
   searched in *and* sits at or below their own sleeping level plus `nocturnal_low_margin_bpm`
   is attributed to the night and reported as suppressed, with the reason.

A scan also reads `context_warmup_hours` past both ends of the requested window. Without it a
run that starts at midnight cannot see the night that ran into that morning, treats it as
resting time, and invents a bradycardia that exists only because of where the window began.
The reported window is still the one that was asked for.

## The rules

All three threshold on a **smoothed** series (a centred moving mean, `smoothing_minutes`),
because a single dropped reading in a five-minute stretch would otherwise break a run. The
smoother is a detector, not a clock: it starts firing before the rate actually moved, so each
flagged run is trimmed back to the raw readings that are themselves past the bar before runs
are merged. The smoother decides *that* something happened; the readings decide *when*.

| finding | fires when | unit |
| --- | --- | --- |
| `sustained_high` | smoothed rate stays at or above `level + max(z_high * spread, min_high_delta_bpm)` | bpm |
| `sustained_low` | smoothed rate stays at or below `level - max(z_low * quiet_spread, min_low_delta_bpm)` | bpm |
| `irregular_rhythm` | the interval-to-interval **scatter index** stays above `median_index + max(irregularity_z * scatter_spread, irregularity_floor)` | dimensionless |

The irregular rule is the one that needed inventing rather than borrowing. A published RMSSD
norm is not comparable with a scatter index derived from one reading per minute, so the
comparison is against the wearer's own ordinary scatter, pooled from sliding windows over the
same detection domain the episode was found in. The index itself
(`mean(|Δinterval|) / median(interval)`) is the same statistic as
`fitbit_monitor.hr.hrv.HrvFeatures.irregularity_index`, so a block's value and a whole
window's value from the feature library are the same quantity.

Then every candidate - found or held back - passes the same gates: at least
`min_sustained_minutes` (and `irregularity_min_minutes` for a scatter episode), at least
`min_finding_samples` readings, and at least `min_coverage` of its claimed duration actually
observed. Anything held back is returned as a `SuppressedEpisode` with the bar it missed.
Stretching a run across a hole is a config decision (`merge_gap_minutes`), and so is the hole
itself: a dip in the rate is *observed* data, and a 60-minute episode that dips back to normal
for 20 minutes is one episode with a lower mean, not two episodes and a gap.

## Confidence, and why it is never 1.0

```
confidence = ceiling * 0.45 * margin_factor + ceiling * 0.30 * coverage
           + baseline_factor * 0.25 * (1 - provisional_cap_penalty)
```

Not a probability, and not a grade ("high/medium/low" hides which of its inputs moved). It is a
monotone score over three stated factors - how far past the bar the episode went *relative to
the wearer's own spread*, how much of it was actually observed, and how mature the baseline
behind it is - each of which is returned in `Finding.detail["confidence"]` next to the ceiling
and the provisional cap that were applied. `confidence_ceiling` is below 1.0 by validation: an
engine that can report certainty is an engine someone will eventually misread.

## What it costs: measured on the seeded corpus

Twelve synthetic wearers, 60 days each, five-minute intraday data, defaults as shipped.
Labels are the injected episodes in `dev_annotation`; a finding counts against a label when
it overlaps it (45 minutes of tolerance), and a finding that overlaps nothing is a false
positive.

| configuration | `sustained_low` | `irregular_rhythm` | false positives |
| --- | --- | --- | --- |
| **defaults as shipped** | 12/12 | 11/12 | 4 (scatter), 1 (high) |
| `z_high = 5.0` | 12/12 | 11/12 | 4 (scatter) |
| `z_high = 3.0` | 12/12 | 11/12 | 107 (high) + 4 |
| `irregularity_floor = 0.20` | 12/12 | 11/12 | 1 |
| `irregularity_z = 3.0` | 12/12 | 11/12 | 6 (scatter) + 1 |

What the numbers say, in order of how much they matter:

* **`z_high = 4.0` is a cliff, not a dial.** At 3.0 the same corpus produces 107 sustained-high
  findings, all of them ordinary afternoon life; at 4.0 it produces one. The reason is the
  *detection domain*: the resting context scans an awake day, whose spread is large but whose
  tail is long, and a z-score of 3 on a wearer whose own spread has been floored at 2 bpm is a
  bar of 6 bpm. The wide-bar design only holds if the multiplier is generous.
* **The seeded sustained-high episodes are mostly the exercise mask's, on purpose.** 16 of the
  24 injected high episodes (133-155 bpm, 40-90 min, mid-morning) are reported as *attributed
  to activity*: heart rate alone cannot tell a workout from a sustained elevation, so the
  engine subtracts the exercise mask in the awake context and records each stretch it set
  aside, with its rate and duration, as a suppressed episode. With no workout records in the
  store the mask is inferred from the rate itself, which means the mask and the rule are
  competing for the same signal - a decision about which one should win is a tuning decision,
  and it belongs to the labelled-corpus gate rather than being settled here by pushing
  `z_high` down into the noise.
* **The scatter floor is doing real work.** Raising `irregularity_floor` from 0.15 to 0.20
  removes three of the four scatter false positives and costs nothing in recall on this
  corpus; lowering it to 0.12 adds more. The shipped 0.15 is the permissive end of that: it
  keeps all eleven recalled episodes and leaves the last four to be argued with on labelled
  data, where the ordinary index (which on this corpus sits between 0.02 and 0.13 depending on
  the wearer) is a synthetic generator's spread of a spread, not a human's.

Reproduce it with the sweep in this repository's ticket history, or in three lines:

```python
from fitbit_monitor.anomaly import AnomalyConfig, detect
result = detect(store, since, until, config=AnomalyConfig())
print(result.counts(), [note.reason for note in result.notes])
```

What a scan costs is a tracked benchmark case, `anomaly_detect`: **~66 days/second** on the
90-day corpus, warm-up read and evidence series included (`benchmarks/baseline.json`). That is
the case that catches an accidental per-day re-query or a quadratic in the rules, and it is why
the engine reads each dataset once and slices it in memory.

## Configuring it

Every threshold is a field on `AnomalyConfig`, every field is a declared plugin option
(`AnomalyConfig.option_specs()` is what a plugin advertises, so the two cannot drift), and
invalid combinations are refused at construction - a `z` below 1.0, a provisional minimum
above the established one, a confidence ceiling of 1.0, a baseline window shorter than the
baseline it is supposed to hold. See `fitbit_monitor/anomaly/config.py` for the annotated
defaults and [docs/plugins.md](plugins.md) for setting them per plugin.

## What this library will not do

* No clinical vocabulary. Findings carry types (`sustained_high`, `sustained_low`,
  `irregular_rhythm`), measurements and evidence. Words like arrhythmia, bradycardia,
  diagnosis or "see a doctor" never appear in a finding, a suppression or a note; the test
  suite asserts that over the serialised output, because a claim inside the library is a claim
  every consumer inherits.
* No severity, no titles, no messages: `Finding.alert_args()` hands a plugin every technical
  field an `Alert` needs and none of the prose.
* No alerting decisions. The engine merges its own overlapping episodes and stops.
  `Finding.dedup_key` is `(type, context, local day)` - stable when a scan window moves - and
  what happens after that is the sink's cooldown.
* No clinical validity claim. The corpus the numbers above come from is synthetic.
