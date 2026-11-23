"""Which readings a context actually contains - the join between windows and the series.

The feature library hands over *windows* (:class:`~fitbit_monitor.hr.windows.TimeWindow`)
and a *mask* (:class:`~fitbit_monitor.hr.exercise.ExerciseMask`), and a rule needs
timestamps: an episode has an onset and an offset, and a window's ``values`` tuple has
lost the instants its readings came from. This module is the explicit join that says, once,
what "at rest" means - and it separates two questions that are easy to collapse into one
and wrong when you do:

**Where the baseline comes from.** Each day's quietest sustained stretch: the awake resting
window (:data:`~fitbit_monitor.hr.util.REST_HEURISTIC`) for the ``rest`` context, the
overnight window (:data:`~fitbit_monitor.hr.util.SLEEP_HEURISTIC`) for ``sleep``. This is
the wearer's *level*, and it has to come from the quietest part of the day or the level
drifts upward with the wearer's activity budget.

**Where the rules look.** A wider set: for ``rest``, every measured reading of the local day
that is neither asleep nor exercising; for ``sleep``, the night's window. Confining the rules
to the quietest window would be self-defeating, because a resting window is grown *outwards
from* its quietest block and stops at a rise - so the elevated stretch a rule exists to find
is exactly the stretch the window excludes. Detection therefore runs on the wider set and the
bar it has to clear is derived from that same set's own spread
(:attr:`~fitbit_monitor.anomaly.baseline.RateBaseline.sigma_bpm`), which is what keeps the
wider net from crying wolf: the ordinary swing of an awake day is tens of bpm wide, and the
rule asks for a departure from the resting level of several of those widths, sustained.

The two contexts, in full:

``rest``
    baseline: the day's awake resting window, exercise subtracted. detection: the local day
    minus the night that ran into this morning, minus this evening's night, minus every
    exercise span. Exercise subtraction is what keeps "152 bpm on a run" out of a
    resting-rate rule - see :mod:`fitbit_monitor.hr.exercise`.
``sleep``
    baseline and detection: the night's detected window - the quietest sustained stretch,
    grown outwards - with **no** exercise subtraction. A wearer who is asleep is not
    exercising, and a rate that would read as a workout at noon is precisely the finding worth
    making at 03:00: subtracting the exercise mask overnight would delete the sustained high
    rates this rule exists to find. Both decisions are in
    :data:`~fitbit_monitor.anomaly.config.EXERCISE_EXCLUDED_CONTEXTS`, and the exclusions
    that were applied are reported with every day rather than being invisible.

Both domains end at a *detected window*, and a detected window is a function of the data: a
sustained abnormality long enough to matter is also what truncates it. The consequence is
named where it bites (see :func:`_split_domain`) rather than hidden - an overnight episode
tends to be reported once per context it touches, which is why the dedup key carries the
context rather than only the day and the type.

A day whose context window was never detected contributes nothing, and is still counted as a
day that was offered: "3 of 30 days usable" and "3 of 3" are different statements about the
same set of findings, and a baseline that cannot say which it is cannot be audited.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Sequence

from ..hr.daily import DayFeatures
from ..hr.epochs import EpochSeries, Sample
from ..hr.util import minutes
from ..hr.windows import TimeWindow
from .config import CONTEXTS, AnomalyConfig, AnomalyConfigError


def context_window(day: DayFeatures, context: str) -> TimeWindow:
    """The window ``context`` names for one day. Undetected windows come back as such."""
    if context == "rest":
        return day.rest
    if context == "sleep":
        return day.sleep
    raise AnomalyConfigError(
        f"unknown context {context!r}",
        hint=f"a context is one of: {', '.join(CONTEXTS)}",
    )


@dataclass(frozen=True)
class ContextDay:
    """One day, one context: what the baseline may use and what the rules may use."""

    day: date
    context: str
    window: TimeWindow
    #: Readings the personal level is built from - inside the context window.
    baseline_samples: tuple[Sample, ...] = ()
    #: Readings the rules run over - the context's detection domain (see this module).
    detection_samples: tuple[Sample, ...] = ()
    epoch_seconds: float = 60.0
    #: Readings dropped from the baseline set by the exercise mask (0 where the context does
    #: not subtract exercise at all), so every exclusion is visible in the output.
    excluded_samples: int = 0
    #: Readings the exercise mask took out of the detection domain. The engine runs the high
    #: rule over these too - not to report them, but to say out loud that it saw them and
    #: attributed them to activity. See the ``activity`` note in the engine.
    exercise_overlap: tuple[Sample, ...] = ()
    #: The region each of this day's nights was searched in, as the feature library recorded
    #: it (``[local evening, next local morning]``, clipped to the data). Not a claim that the
    #: wearer was asleep in it - the library's own search region - which is exactly why it is
    #: the right thing to ask about when deciding whether a low stretch is a night's or a
    #: day's. See :attr:`fitbit_monitor.anomaly.config.AnomalyConfig.nocturnal_low_margin_bpm`.
    night_bounds: tuple[tuple[datetime, datetime], ...] = ()

    # --- the baseline's view ------------------------------------------------
    @property
    def usable(self) -> bool:
        """Two readings is the minimum a day can contribute: one is a point, not a level."""
        return len(self.baseline_samples) >= 2 and self.window.detected

    @property
    def values(self) -> tuple[float, ...]:
        return tuple(
            float(sample.bpm) for sample in self.baseline_samples if sample.bpm is not None
        )

    # --- the rules' view ----------------------------------------------------
    @property
    def points(self) -> tuple[tuple[datetime, float], ...]:
        """``(instant, rate)`` pairs over the detection domain, oldest first."""
        return tuple(
            (sample.ts, float(sample.bpm))
            for sample in self.detection_samples
            if sample.bpm is not None
        )

    @property
    def detection_values(self) -> tuple[float, ...]:
        return tuple(value for _stamp, value in self.points)

    @property
    def has_domain(self) -> bool:
        return len(self.detection_samples) >= 2

    @property
    def minutes_observed(self) -> float:
        return minutes(len(self.detection_samples) * self.epoch_seconds)

    @property
    def window_minutes(self) -> float:
        return self.window.minutes

    def as_dict(self) -> dict[str, object]:
        return {
            "day": self.day.isoformat(),
            "context": self.context,
            "usable": self.usable,
            "baseline_samples": len(self.baseline_samples),
            "detection_samples": len(self.detection_samples),
            "excluded_samples": self.excluded_samples,
            "observed_minutes": self.minutes_observed,
            "window": self.window.as_dict(),
        }


def baseline_samples(
    day: DayFeatures,
    series: EpochSeries,
    context: str,
    *,
    exclude_exercise: bool = True,
) -> tuple[tuple[Sample, ...], int]:
    """The measured in-window samples of one day, and how many exercise dropped."""
    window = context_window(day, context)
    if not window.detected:
        return (), 0
    kept: list[Sample] = []
    dropped = 0
    mask = day.exercise
    for sample in _in_window(series, day, context):
        if not window.covers(sample.ts):
            continue
        if exclude_exercise and mask.covers(sample.ts):
            dropped += 1
            continue
        kept.append(sample)
    return tuple(kept), dropped


def detection_samples(
    day: DayFeatures,
    series: EpochSeries,
    context: str,
    *,
    exclude_exercise: bool = True,
) -> tuple[Sample, ...]:
    """The context's detection domain for one day - see this module's docstring."""
    return _split_domain(day, series, context, exclude_exercise=exclude_exercise)[0]


def _split_domain(
    day: DayFeatures,
    series: EpochSeries,
    context: str,
    *,
    exclude_exercise: bool = True,
) -> tuple[tuple[Sample, ...], tuple[Sample, ...]]:
    """The detection domain, and the exercise spans that were taken out of it.

    For ``sleep`` that is the night's window, read out of ``series`` rather than out of
    ``day.series``: a night belongs to the day it *began* on, so most of its readings fall in
    the next calendar day's slice and would be missing from it. For ``rest`` it is the local
    day minus both nights and minus exercise, which is by construction inside ``day.series``.

    A detected window is a function of the data, so an abnormality big enough to be worth
    finding also moves the boundary - in both directions. A sustained stretch above the
    night's level is an excursion the window's growth gives up on, so it lands outside the
    window and the night's rules do not see it (the resting rules do, which is why an
    overnight episode is often reported in the resting context). A stretch *below* the night
    is the quietest thing there and becomes the window itself, pushing the rest of the night
    out into the resting domain, where a perfectly ordinary 55 bpm against a 75 bpm resting
    level reads as a seven-hour bradycardia. This engine does not silently redefine the
    feature library's answer to "when was this wearer asleep"; what it does is refuse to let
    that second leak become a finding - a low stretch at the wearer's own sleeping level
    inside the region their night was searched in is attributed to the night and reported
    once, by the context that can measure it. See
    :attr:`~fitbit_monitor.anomaly.config.AnomalyConfig.nocturnal_low_margin_bpm`.
    """
    if context == "sleep":
        window = context_window(day, context)
        if not window.detected:
            return (), ()
        return tuple(sample for sample in series.measured_samples if window.covers(sample.ts)), ()
    kept: list[Sample] = []
    overlap: list[Sample] = []
    mask = day.exercise
    for sample in day.series.measured_samples:
        if day.sleep.covers(sample.ts) or day.previous_sleep.covers(sample.ts):
            continue
        if exclude_exercise and mask.covers(sample.ts):
            overlap.append(sample)
            continue
        kept.append(sample)
    return tuple(kept), tuple(overlap)


def _in_window(series: EpochSeries, day: DayFeatures, context: str) -> tuple[Sample, ...]:
    """Samples a day's context window may be matched against (see :func:`detection_samples`)."""
    if context == "sleep":
        return series.measured_samples
    return day.series.measured_samples


def anchored_region(window: TimeWindow) -> tuple[datetime, datetime] | None:
    """The region a night was searched in, read back out of the window the library built.

    :func:`fitbit_monitor.hr.windows.detect_sleep_window` records it in ``detail["anchored"]``.
    Reusing it rather than recomputing one here means one timezone decision, one pair of anchor
    hours, and no chance of the anomaly layer and the feature layer disagreeing about when the
    night was.
    """
    anchored = window.detail.get("anchored") if window.detail else None
    if not isinstance(anchored, (list, tuple)) or len(anchored) != 2:
        return None
    try:
        return datetime.fromisoformat(str(anchored[0])), datetime.fromisoformat(str(anchored[1]))
    except ValueError:  # pragma: no cover - the library writes ISO strings
        return None


def night_regions(day: DayFeatures) -> tuple[tuple[datetime, datetime], ...]:
    """Both nights that touch one local day: the one that ran into this morning, and this evening's."""
    out: list[tuple[datetime, datetime]] = []
    for window in (day.previous_sleep, day.sleep):
        if window.detected:
            region = anchored_region(window)
            if region is not None:
                out.append(region)
    return tuple(out)


def context_days(
    days: Sequence[DayFeatures],
    series: EpochSeries,
    context: str,
    config: AnomalyConfig | None = None,
) -> list[ContextDay]:
    """Every day's baseline and detection samples for one context, oldest first."""
    cfg = config or AnomalyConfig()
    exclude_exercise = context in cfg.exclude_exercise_in
    epoch = series.effective_epoch_seconds
    out: list[ContextDay] = []
    for day in days:
        kept, dropped = baseline_samples(day, series, context, exclude_exercise=exclude_exercise)
        domain, overlap = _split_domain(day, series, context, exclude_exercise=exclude_exercise)
        out.append(
            ContextDay(
                day=day.day,
                context=context,
                window=context_window(day, context),
                baseline_samples=kept,
                detection_samples=domain,
                epoch_seconds=epoch,
                excluded_samples=dropped,
                exercise_overlap=overlap,
                night_bounds=night_regions(day),
            )
        )
    return out


__all__ = [
    "CONTEXTS",
    "ContextDay",
    "baseline_samples",
    "context_days",
    "anchored_region",
    "context_window",
    "detection_samples",
    "night_regions",
]
