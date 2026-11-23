"""Heart-rate features: the input layer a heart-rate detector is built on.

Everything here turns what the store holds - intraday heart rate, one value per epoch -
into the views a detection rule needs, and nothing else: no thresholds for "abnormal", no
alerts, no clinical claim. Those belong to the anomaly engine and its plugin (the
hr-anomaly epic); this package's job is to hand them inputs whose units, gaps, windows and
sample counts are already stated.

Read it in this order::

    fitbit_monitor.hr.epochs     per-epoch series: gaps and impossible values identified
    fitbit_monitor.hr.windows    sleep and resting windows, each heuristic named
    fitbit_monitor.hr.exercise   the exercise exclusion mask
    fitbit_monitor.hr.hrv        SDNN, RMSSD, pNN50 and an irregularity index
    fitbit_monitor.hr.daily      all of it joined up, one row per local day
    fitbit_monitor.hr.util       the shared assumptions: units, bounds, named heuristics

Typical use, in one block::

    from fitbit_monitor.hr import FeatureOptions, daily_features, epoch_series
    from zoneinfo import ZoneInfo

    opts = FeatureOptions(tz=ZoneInfo("Europe/Berlin"))
    series = epoch_series(store, since, until)          # one day's data, gaps flagged
    for day in daily_features(store, since, until, options=opts):
        day.rest.mean_bpm                               # the day's resting rate
        day.hrv.rmssd_ms                                # None if too few intervals
        day.exercise.covers(a_timestamp)                # was that minute exercise?

The four promises this package exists to keep
--------------------------------------------
**Units are never a guess.** Rates are beats per minute
(:data:`~fitbit_monitor.hr.util.UNIT_BPM`), intervals - SDNN, RMSSD, every RR estimate -
are milliseconds (:data:`~fitbit_monitor.hr.util.UNIT_MS`), durations are reported in
minutes, and every instant is timezone-aware UTC, because that is what the store holds and
:func:`~fitbit_monitor.store.timeutil.ensure_utc` refuses anything else. Local time is a
*parameter* (``tz``), never an assumption.

**Nothing is silently dropped.** An impossible value, a null, a sensor dropout and a
two-hour hole each produce a record - a flagged
:class:`~fitbit_monitor.hr.epochs.Sample` or a
:class:`~fitbit_monitor.store.reader.Gap` - and the counts that a threshold divides by
come from the same object. See :mod:`~fitbit_monitor.hr.epochs`.

**A window names the rule that made it.** ``lowest-load-overnight-block``,
``quiet-hour-sustained-minimum``, ``relative-sustained-elevation``,
``successive-epoch-interval-estimate``: a finding can quote the heuristic it fired on, and a
threshold can be argued about instead of trusted. See :mod:`~fitbit_monitor.hr.util`.

**A feature that cannot be computed says so.** Every HRV value is ``None`` below
:data:`~fitbit_monitor.hr.hrv.MIN_INTERVALS` usable intervals, with
:attr:`~fitbit_monitor.hr.hrv.HrvFeatures.reason` explaining the count; every window that
could not be detected is a :class:`~fitbit_monitor.hr.windows.TimeWindow` with
``detected == False`` and a reason. A day with no data and a day with four readings both
produce an answer, and neither produces a number that looks like evidence.

Units and the assumptions a threshold depends on
-----------------------------------------------
===============================  ==================================  ==========================
quantity                         unit                                assumption
===============================  ==================================  ==========================
heart rate                       beats per minute (``bpm``)         plausible band [25, 220] inclusive
beat-interval estimate           milliseconds (``ms``)              ``60000 / bpm`` of one reading
SDNN                             milliseconds                       population SD (``n``) of the window
RMSSD                            milliseconds                       successive *epoch* differences, not beat-to-beat
pNN50                            fraction 0..1                      threshold proportional to the window's own mean interval
irregularity index               dimensionless ratio                mean |Δinterval| / median interval
window duration                  minutes                            half-open ``[start, end)``, local-day anchored
exercise threshold               beats per minute                   ``max(110, resting x 1.35)`` rounded up
minimum HRV sample               intervals                          8 successive same-epoch intervals
===============================  ==================================  ==========================

Each row is explained where it is implemented; this table is the index, not the argument.
"""

from __future__ import annotations

from ..timeutil import resolve_timezone
from .daily import (
    HRV_WINDOWS,
    DayFeatures,
    FeatureOptions,
    anchor_window,
    daily_features,
    day_features,
    day_window,
)
from .epochs import (
    DEFAULT_EPOCH_SECONDS,
    DEFAULT_GAP_TOLERANCE,
    EpochSeries,
    Sample,
    build_samples,
    classify,
    epoch_series,
    find_gaps,
    grain_seconds,
)
from .exercise import ExerciseMask, Span, build_exercise_mask, threshold_bpm
from .hrv import MIN_INTERVALS, HrvFeatures, Interval, estimate_intervals, hrv_features
from .util import (
    DEFAULT_MAX_BPM,
    DEFAULT_MIN_BPM,
    EXERCISE_HEURISTIC,
    HRV_HEURISTIC,
    REST_HEURISTIC,
    SLEEP_HEURISTIC,
    UNIT_BPM,
    UNIT_MS,
    bpm_to_ms,
    ms_to_bpm,
)
from .windows import TimeWindow, detect_resting_window, detect_sleep_window, not_detected

__all__ = [
    "DEFAULT_EPOCH_SECONDS",
    "DEFAULT_GAP_TOLERANCE",
    "DEFAULT_MAX_BPM",
    "DEFAULT_MIN_BPM",
    "EXERCISE_HEURISTIC",
    "HRV_HEURISTIC",
    "HRV_WINDOWS",
    "MIN_INTERVALS",
    "REST_HEURISTIC",
    "SLEEP_HEURISTIC",
    "UNIT_BPM",
    "UNIT_MS",
    "DayFeatures",
    "EpochSeries",
    "ExerciseMask",
    "FeatureOptions",
    "HrvFeatures",
    "Interval",
    "Sample",
    "Span",
    "TimeWindow",
    "anchor_window",
    "bpm_to_ms",
    "build_exercise_mask",
    "build_samples",
    "classify",
    "daily_features",
    "day_features",
    "day_window",
    "detect_resting_window",
    "detect_sleep_window",
    "epoch_series",
    "estimate_intervals",
    "find_gaps",
    "grain_seconds",
    "hrv_features",
    "ms_to_bpm",
    "not_detected",
    "resolve_timezone",
    "threshold_bpm",
]
