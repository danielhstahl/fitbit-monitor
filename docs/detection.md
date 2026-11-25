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

The plugin that turns those measurements into alerts - with the plain-language framing, the
alerting policy and the per-plugin config - is `hr-anomaly`, and it is documented in
[docs/plugins.md](plugins.md#reference-hr-anomaly-the-built-in-alerting-example). Everything
below is about the intraday engine; [nightly series](#nightly-series-hrv-anomaly) has its
own detector, for reasons that turn out to be about the shape of the data rather than the
number of samples.

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

## Nightly series: `hrv-anomaly`

Heart-rate variability arrives as **one number per night**. That is not "heart rate, fewer
samples": it is a different shape, and the intraday engine's machinery has nothing to bite
on. "Sustained for `min_sustained_minutes`" has no referent in a nightly value, a minutes
floor cannot filter a single measurement, and there is no burst to sustain through a gap.
Bending the intraday config to cover it would have meant rules whose names lie about what
they check, so nightly HRV has its own detector:
[`src/fitbit_monitor/anomaly/hrv.py`](../src/fitbit_monitor/anomaly/hrv.py), surfaced by the
`hrv-anomaly` plugin
([`plugins/builtin/hrv_anomaly.py`](../src/fitbit_monitor/plugins/builtin/hrv_anomaly.py)).
The full reasoning - including the option of upsampling nightly values into a fake intraday
series, and why manufacturing evidence was rejected outright - is
[ADR-0006](adr/0006-nightly-series-detector.md).

The bar is your own median and MAD of `log(value)` over up to `baseline_nights` nights.
Log space because HRV is multiplicative - 10 ms off a 30 ms night is not the same event as
10 ms off a 90 ms night - and MAD because a wearer who was ill for a week should not have
that week pulled their own "normal" towards it. Both thresholds are floors as well as
sigmas (`min_low_delta_ms`, `min_spread_ms`), so a recording with no scatter in it cannot
make its own rounding noise look like a finding.

Three rules:

| id | in words | default |
| --- | --- | --- |
| `hrv_low` | `min_consecutive_nights` nights at or below `z_low` sigmas under your median, or one night past `deep_dip_z` | 2 nights, 3.0σ, 4.5σ for a lone night |
| `hrv_high` | the mirror above the median - **off**, because high HRV is not a warning sign and an alert on good news trains you to ignore the ones that are not | `alert_on_high = false` |
| corroboration | resting HR over the same nights, judged against its own prior nights; recorded, never required | `corroborate = true` at 2.0σ |

Two details are the whole design, and both are about a baseline eating itself:

* **A night that breaks the bar does not join the pool the bar is built from.** The bar means
  "what have my ordinary nights looked like", and a dip that votes on its own baseline would
  widen and lower it - a fortnight of low HRV would redefine *low* and then report nothing.
  So flagged nights are held aside, and a run is judged against the bar standing when the run
  began, not against one averaged across its own length.
* **The window is read with `baseline_nights` of lead-in.** Judging May using only May's
  history spends most of May with no bar at all, which is a silent no; the series is fetched
  from before the window and only the nights inside it are judged.

Early history is answered with a weaker sentence rather than no sentence: below
`min_baseline_nights` the bar is built anyway, flagged provisional and capped at
`provisional_confidence_cap` (and such an alert can never leave informational severity),
while below `provisional_min_nights` the rules do not run at all and the run says so. A night
with no value is not a low night and a stored `0 ms` is a broken row, not the strongest
finding in the month; both are excluded and reported.

The same three confidence factors as the intraday engine - how far past the bar, how mature
the baseline, how complete the record - with the same ceiling below 1.0. What an alert says,
and how to run it, is in
[docs/plugins.md](plugins.md#reference-hrv-anomaly-the-built-in-nightly-series-example).

One caveat the project keeps on the record: the intraday rules are measured against a
labelled corpus with CI floors below, and **the nightly rules are not yet**. Their defaults
were tuned against the seeded corpus and hand-planted scenarios, which shows the arithmetic
and the plumbing work, not the sensitivity. See ADR-0006's consequences.

## Confidence, and why it is never 1.0

```
confidence = ceiling * coverage * margin_factor * baseline_factor
             (and never more than provisional_confidence_cap while the baseline is provisional)
```

Four factors multiplied, not weights added: a finding with half its episode unobserved is worth
half as much whatever else it has going for it, and a product says that where a weighted sum
hides it. Not a probability, and not a grade ("high/medium/low" hides which of its inputs
moved). It is a monotone score over three stated factors - how far past the bar the episode
went *relative to the wearer's own spread*, how much of it was actually observed, and how
mature the baseline behind it is - each of which is returned in `Finding.detail["confidence"]`
next to the ceiling and the provisional cap that were applied. `confidence_ceiling` is below
1.0 by validation: an engine that can report certainty is an engine someone will eventually
misread. What the ordering is *for* is checked, not asserted: the accuracy gate scores how well
confidence separates a real episode from a false finding
([the accuracy gate](#the-accuracy-gate-measured-on-labelled-data)).

## The accuracy gate: measured on labelled data

Every threshold named above was chosen against labelled data and is re-measured on every CI
run, because "the defaults look sensible" is how a plugin ends up alerting on a third of your
weekends. The fixture is `tests/fixtures/anomaly/hr_anomaly_labels.json` - five synthetic
wearers of `fm dev seed` data, 90 days each, generated by
`scripts/dev/record_accuracy_fixtures.py` - and it holds **30 labelled episodes** and
**327 clean all-night windows**. Each label carries a type, a window and an expected verdict:

| verdict | what it demands | how many |
| --- | --- | --- |
| `alert` | a finding of this type must be reported overlapping this window | 20 episodes |
| `held` | nothing may be reported here, but the engine must *account* for it: a suppression naming the window, not silence | 10 episodes |
| `clear` | no finding of the sustained-rate rules may appear inside this window | 327 nights |

`held` is the interesting one, and it exists because of what the generator injects. Its
abnormal elevations are 133-155 bpm for 40-90 minutes, mid-morning - which is the same shape
as its workouts, and the shipped awake context subtracts the inferred exercise mask
(`exclude_exercise_in`). So a labelled daytime elevation at or above the exercise mask's
absolute floor is *supposed* to be attributed to activity and reported as suppressed.
Labelling those windows `alert` would demand a detector that alerts on workouts; labelling
them `clear` would demand that the engine not see them at all, and this engine promises it
never stays silent about something it saw. `held` is the verdict that says "not reportable,
and say why", and the gate checks it: found-and-explained passes, silently dropped fails.

Two failures the gate found on the way, both fixed here rather than papered over:

* the seed generator compared a workout *hour* against a *5-minute slot index*, so every
  synthetic workout landed at 00:30-01:35 - inside the nights the fixture was about to
  label "clean". A gate cannot grade a clean night that has a 140 bpm workout in it;
* the engine's exercise attribution ran the rule over a whole day's masked readings
  concatenated, so the centred smoother averaged the end of one exercise span into the start
  of the next, the "episode" that came out spanned most of the day, failed its own coverage
  gate, and was dropped - a real 75-minute elevation producing neither a finding nor a
  suppression. That is the silence `held` exists to catch. The attribution now probes one
  contiguous span at a time; the regression is pinned at the engine level by
  `TestExerciseAttributionAcrossTwoSpans`, which is written at the corpus's 5-minute grain
  because that is the grain the bridging needs - with the generator fixed, the shipped
  fixture's wearers no longer line their spans up badly enough to trip it on their own.

### Running it

```console
$ fm dev accuracy --wearers 1     # the matrix, and an exit code that means something
$ ./scripts/dev/test.sh           # the same gate over all five wearers, on every CI run
$ python scripts/dev/record_accuracy_fixtures.py --check   # is the fixture still what the generator makes?
```

`fm dev accuracy` seeds the fixture's corpora into a temporary store (never yours), prints
the confusion matrix and every floor, and exits non-zero if a floor is missed. The suite runs
the same scoring, so the numbers below are printed by `scripts/dev/test.sh` on every run.

### Measured, with the shipped defaults

Tolerance 45 minutes; five wearers; 30 labelled episodes; 327 clean nights.

| metric | measured | floor | what a miss would mean |
| --- | --- | --- | --- |
| recall, `irregular_rhythm` labels | 1.000 | 0.90 | the scatter rule stopped finding scatter |
| recall, `sustained_low` labels | 1.000 | 0.90 | the bradycardia half of the rate rule regressed |
| recall, `sustained_high` labels | n/a (all 10 are `held`) | 0.90 | would fire if a high label were re-labelled `alert` and missed |
| recall, every `alert` label | 1.000 | 0.90 | something the detector claims to find went unfound |
| precision | 1.000 | 0.90 | ordinary life started being reported |
| false positives inside clean nights | 0 | 0 | a rate finding on an unremarkable all-night window - the one number that is not a trade-off |
| `held` labels with no explanation | 0 | 0 | the engine went silent about an episode it had to account for |
| confidence ranks true above false | 1.000 | 0.75 | the ordering a notification threshold depends on inverted |

The floors sit at 0.90 while the measurement is 1.000 on purpose: a floor pinned to last
week's measurement fails on noise and gets disabled, and a floor that fails when a rule stops
working is a floor worth keeping. Two changes of behaviour would trip them, and both are
listed here because they are the two a well-meaning change would cause.

### What the gate changed, and what it refused

* **`irregularity_floor` 0.15 → 0.20, and that is now the shipped default.** At 0.15 the
  corpus produced scatter findings that matched no labelled episode - every one of them the
  onset of an inferred workout, every one of them under 0.55 confidence. At 0.18 they were
  gone and every labelled irregular episode was still found; at 0.25 the first labelled
  episode started being missed. 0.20 sits inside that window with room on both sides. The
  caveat is stated rather than skipped: the corpus's ordinary scatter is a synthetic
  generator's spread-of-a-spread, so 0.20 is calibrated to *this* data and a real wearer's
  quiet-window index is not necessarily the same number.
* **`exclude_exercise_in` stayed `("rest",)`.** The tempting "fix" for a sustained high that
  went unreported is to subtract nothing in the awake context, so that was measured rather
  than argued about. Over the first two fixture wearers the wide net turns 8 findings into
  128: precision 1.000 → 0.062, confidence ranking 1.000 → 0.338 - *worse than a coin flip*,
  because the workout onsets it adds are the loudest things in the run - and it reports all
  four `held` labels it is supposed to explain instead. The recall it buys is zero: the rate
  rule already found every alertable episode. That is the trade this default is making, and
  `ExerciseAttributionTradeoffTest` fails if someone rediscovers the wide net and calls it
  free.

### What a scan costs, and what it is allowed to cost

Two tracked benchmark cases cover a scan, both over the same 90-day corpus in
`benchmarks/baseline.json`:

* **`anomaly_detect`** - the engine alone, warm-up read and evidence series included, in
  days/second. This is the case that catches an accidental per-day re-query or a quadratic
  in the rules, and it is why the engine reads each dataset once and slices it in memory.
* **`hr_anomaly_scan`** - the whole `hr-anomaly` plugin run over the same corpus: engine,
  coverage gate, alert policy, the prose on every alert and the metrics the run reports.
  This is what `fm plugins run hr-anomaly --since 90d` actually costs you.

Each has a documented budget of **9 days/second, i.e. a full 90-day scan under 10 s** on
the reference runner ([docs/ci.md](ci.md#the-budget-each-case-has-to-fit-inside) holds the
whole table), and both are currently running about seven times inside it. The gate that can
fail a build is the 15% regression against the recorded measurement rather than the budget,
so the number to watch in a review is the delta in `bench-summary.md`, not the headroom.

Peak resident set is reported per case where the platform can measure it. A scan streams:
it holds a day's epochs and the baselines, not the quarter, and the recorded per-case delta
in `benchmarks/baseline.json` is the number that would move if that ever stopped being true.

## Configuring it

Every threshold is a field on `AnomalyConfig`, every field is a declared plugin option
(`AnomalyConfig.option_specs()` is what a plugin advertises, so the two cannot drift), and
invalid combinations are refused at construction - a `z` below 1.0, a provisional minimum
above the established one, a confidence ceiling of 1.0, a baseline window shorter than the
baseline it is supposed to hold. See `fitbit_monitor/anomaly/config.py` for the annotated
defaults.

Set them per plugin in `[plugins.hr-anomaly]`, which declares every one of them plus its
own alerting policy (`min_confidence`, `cooldown`, `max_alerts`, `min_window_coverage`):

```toml
[plugins.hr-anomaly]
min_baseline_days = 10            # more history before a finding may claim full confidence
min_sustained_minutes = 30        # a longer stretch past the bar before anything is reported
z_high = 4.5                      # harder to call an elevation abnormal for this wearer
```

```console
$ fm plugins show hr-anomaly            # every option, its default, and what the file changed
$ fm plugins run hr-anomaly --since 30d --set hr-anomaly.z_high=4.5   # one run, no edit
```

[docs/plugins.md](plugins.md) is the guide to running and configuring it; this document is
about what the numbers mean.

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
