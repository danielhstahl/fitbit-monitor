"""The anomaly detection engine: personal baselines, sustained-rate rules, irregular rhythm.

This is the library a heart-rate alert lives inside, kept apart from the plugin that wraps it
so that the part worth testing - the part that decides whether a stretch of your data is far
from your own ordinary values - can be tested against data, thresholds and hand-crafted
series without a plugin, a sink or a config file in the way. It sits on top of
:mod:`fitbit_monitor.hr` (which says what the data *is*: per-epoch series, resting and sleep
windows, exercise exclusion, HRV statistics) and underneath the hr-anomaly plugin (which
says what a finding *means*, in words, to a person). Read it in this order::

    fitbit_monitor.anomaly.config    every threshold, with its default and its reasoning
    fitbit_monitor.anomaly.baseline  median/MAD personal baselines, and when one refuses
    fitbit_monitor.anomaly.context   what "at rest" means for a rule, stated once
    fitbit_monitor.anomaly.rules     the three rules, and the statistics they stand on
    fitbit_monitor.anomaly.engine    the pipeline: read, baseline, rules, merge, gate, score
    fitbit_monitor.anomaly.models    findings, suppressed candidates, run summary
    fitbit_monitor.anomaly.errors    the two ways this package says "no"

Typical use, in one block::

    from datetime import datetime, UTC
    from fitbit_monitor.anomaly import AnomalyConfig, detect
    from fitbit_monitor.store.reader import ReadOnlyStore

    store = ReadOnlyStore(path)
    config = AnomalyConfig(time_zone="Europe/Berlin")   # or AnomalyConfig.from_mapping(toml)
    result = detect(store, since, until, config=config)
    for finding in result.findings:
        finding.type              # 'sustained_high' | 'sustained_low' | 'irregular_rhythm'
        finding.onset, finding.offset, finding.duration_minutes
        finding.confidence        # 0..1, never 1.0, from stated factors
        finding.evidence          # the readings that produced it, as Evidence objects
    for note in result.notes:         # rules that could not run, and why
        ...

The four promises this package exists to keep
--------------------------------------------
**The baseline is personal, and it says how sure it is.** A textbook constant is wrong for
every real wearer in both directions, so every threshold is the wearer's own median resting
level in that context plus or minus a multiple of their own spread
(:mod:`fitbit_monitor.anomaly.baseline`). A baseline needs
:attr:`~AnomalyConfig.min_baseline_days` usable resting days to be *established*; before
that it is provisional and no finding derived from it may exceed
:attr:`~AnomalyConfig.provisional_confidence_cap`. Below
:attr:`~AnomalyConfig.provisional_min_days` there is no baseline and no rule runs - the run
says so in a :class:`~fitbit_monitor.anomaly.models.RuleNote` instead of inventing a number.

**Sustained, not spiky.** Every rate rule has a minimum duration
(:attr:`~AnomalyConfig.min_sustained_minutes`) enforced *after* merging, so one spike cannot
fire and one episode broken apart by a dropout is not discarded for being short. Each finding
carries the duration it observed and the fraction of it that was actually measured.

**Irregularity has a stated statistical basis.** ``mean(|Δ interval|) / median interval``
over epoch-derived intervals, compared with the wearer's own distribution of that same
statistic by robust (MAD-scaled) z-score, floored absolutely so a very steady wearer's
sensor jitter cannot become an episode. See
:mod:`fitbit_monitor.anomaly.rules` for the whole argument, including what the coarse
epoch sampling does to it and why that is survivable here and nowhere else.

**Findings, not diagnoses.** A :class:`~fitbit_monitor.anomaly.models.Finding` names a rule,
a span, a confidence and an evidence series. It contains no claim about a condition, no
severity, no title and no message, because those are words about a person's health and the
plugin that talks to the person owns them: :meth:`~fitbit_monitor.anomaly.models.Finding.alert_args`
hands over every technical field an :class:`~fitbit_monitor.plugins.models.Alert` needs and
none of the prose. This library is a heuristic over a wrist-worn optical sensor and is not
a medical device; nothing in it is a diagnosis, and the type names are descriptions of a
signal shape, not of a condition.

Not in here, on purpose
-----------------------
No store writes, no clock reads, no alert delivery and no notification state: findings carry
a stable ``dedup_key`` and an ``occurred_at`` and let the Epic 5 sinks
(:mod:`fitbit_monitor.plugins.sinks`, :mod:`fitbit_monitor.store.alerts`) do the dedup and
cooldown they already do properly. No plugin class, no CLI and no user-facing wording; the
hr-anomaly plugin (this epic's next ticket) is the thin layer that adds exactly those, and
the labelled accuracy gate after it is where the defaults in
:mod:`fitbit_monitor.anomaly.config` get justified with a confusion matrix instead of an
argument.
"""

from __future__ import annotations

from .baseline import (
    MAD_TO_SIGMA,
    RateBaseline,
    ScatterBaseline,
    build_rate_baseline,
    build_scatter_baseline,
    median_absolute_deviation,
    robust_sigma,
)
from .config import CONTEXTS, AnomalyConfig, AnomalyConfigError, declared_option_names
from .context import (
    ContextDay,
    baseline_samples,
    context_days,
    context_window,
    detection_samples,
)
from .engine import AnomalyEngine, Reference, confidence_for, detect, merge_episodes
from .errors import AnomalyError, AnomalyValueError
from .models import (
    FINDING_IRREGULAR,
    FINDING_SUSTAINED_HIGH,
    FINDING_SUSTAINED_LOW,
    FINDING_TYPES,
    FINDING_UNITS,
    RATE_FINDING_TYPES,
    DetectionResult,
    Finding,
    RuleNote,
    SuppressedEpisode,
    sample_points,
)
from .rules import (
    DIRECTION_HIGH,
    DIRECTION_LOW,
    MIN_SCORED_INTERVALS,
    RATE_HEURISTIC,
    SCATTER_HEURISTIC,
    Episode,
    ScatterPoint,
    scatter_episodes,
    scatter_index,
    scatter_window_size,
    scatter_windows,
    sustained_rate_episodes,
)

__all__ = [
    "CONTEXTS",
    "DIRECTION_HIGH",
    "DIRECTION_LOW",
    "FINDING_IRREGULAR",
    "FINDING_SUSTAINED_HIGH",
    "FINDING_SUSTAINED_LOW",
    "FINDING_TYPES",
    "FINDING_UNITS",
    "MAD_TO_SIGMA",
    "MIN_SCORED_INTERVALS",
    "RATE_FINDING_TYPES",
    "RATE_HEURISTIC",
    "SCATTER_HEURISTIC",
    "AnomalyConfig",
    "AnomalyConfigError",
    "AnomalyEngine",
    "AnomalyError",
    "AnomalyValueError",
    "ContextDay",
    "DetectionResult",
    "Episode",
    "Finding",
    "RateBaseline",
    "Reference",
    "RuleNote",
    "ScatterBaseline",
    "ScatterPoint",
    "SuppressedEpisode",
    "baseline_samples",
    "build_rate_baseline",
    "build_scatter_baseline",
    "confidence_for",
    "context_days",
    "context_window",
    "declared_option_names",
    "detect",
    "detection_samples",
    "median_absolute_deviation",
    "merge_episodes",
    "robust_sigma",
    "sample_points",
    "scatter_episodes",
    "scatter_index",
    "scatter_window_size",
    "scatter_windows",
    "sustained_rate_episodes",
]
