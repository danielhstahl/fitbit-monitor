"""The exercise exclusion mask: which readings were movement, not rest.

Why this exists as its own layer
--------------------------------
Nearly every "abnormally high rate" threshold in this domain is a *resting* threshold,
and the way a resting threshold fails is exercise: 152 bpm during a run is not
tachycardia, and a rule that sees it either cries wolf until the user turns it off or is
dulled until it misses the real thing. So "was this person exercising" is computed once,
here, as a mask that a detector *subtracts* before applying a resting rule - and the
mask is returned with its spans and its threshold so a report can show what was excluded
instead of a number that quietly dropped the run.

The rule, named ``relative-sustained-elevation``
-----------------------------------------------
A reading is a candidate when::

    bpm >= max(absolute_floor_bpm, ceil(reference_bpm * resting_multiple))

with ``reference_bpm`` the wearer's own resting rate for the range (usually
:meth:`~fitbit_monitor.hr.windows.TimeWindow.mean_bpm` of the resting window). A run of
candidates becomes an excluded span when it lasts at least ``sustained_minutes``, and the
span then absorbs ``cooldown_minutes`` after its last reading, because heart rate is
still coming down when the walking does.

Every one of those constants is a stated assumption:

* ``resting_multiple = 1.35`` - a relative bar, because "elevated" means elevated *for
  this person*: 35% over a 55 bpm resting rate is 74 bpm, over a 95 bpm resting rate it
  is 129. An absolute-only rule would either never exclude the first person or never
  exclude the second.
* ``absolute_floor_bpm = 110`` - the bar can never be lowered below this, so a wearer with
  an unusually low resting rate does not have their ordinary walk up the stairs excluded
  as exercise, and - more importantly - a *personal* threshold is never so low that a
  genuinely elevated rate gets excluded as "exercise" and never reported.
* ``sustained_minutes = 8`` - shorter than this and a two-minute burst of stairs or a
  single noisy reading starts excluding the hour around it.
* ``cooldown_minutes = 10`` - recovery, per the physiology above.

A range with no resting window at all (no baseline yet) still gets a mask: the absolute
floor alone. That is the conservative direction, and the mask says which reference it
used in :attr:`ExerciseMask.reference_bpm`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable, Sequence

from .epochs import EpochSeries, Sample
from .util import (
    BPM,
    EXERCISE_HEURISTIC,
    UNIT_BPM,
    mean,
    minutes,
)

#: Nothing is excluded below this rate, however low the personal threshold is.
DEFAULT_ABSOLUTE_FLOOR_BPM = 110.0
#: A candidate must be this far over the wearer's resting rate.
DEFAULT_RESTING_MULTIPLE = 1.35
#: How long a run of candidates must last before it counts as exercise.
EXERCISE_SUSTAINED_MINUTES = 8.0
#: Minutes of recovery absorbed into the end of a span.
DEFAULT_COOLDOWN_MINUTES = 10.0


def threshold_bpm(
    resting_bpm: float | None,
    *,
    resting_multiple: float = DEFAULT_RESTING_MULTIPLE,
    absolute_floor_bpm: float = DEFAULT_ABSOLUTE_FLOOR_BPM,
) -> float:
    """The rate at and above which a reading is treated as exercise, in bpm.

    ``max(floor, resting * multiple)`` - see the module docstring for why both halves are
    needed. Returns a whole number of bpm: a threshold reported as 128.37 implies a
    precision the resting estimate behind it does not have.
    """
    if resting_bpm is None or resting_bpm <= 0:
        return float(absolute_floor_bpm)
    personal = resting_bpm * resting_multiple
    return float(max(absolute_floor_bpm, int(personal) + (1 if personal % 1 else 0)))


@dataclass(frozen=True)
class Span:
    """One excluded stretch of time, with the readings that produced it."""

    start: datetime
    end: datetime
    samples: int
    mean_bpm: float
    peak_bpm: float
    #: How far above the threshold the span reached - what a report quotes as the severity.
    excess_bpm: float

    @property
    def minutes(self) -> float:
        return minutes((self.end - self.start).total_seconds())

    def covers(self, instant: datetime) -> bool:
        return self.start <= instant < self.end

    def as_dict(self) -> dict[str, object]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "minutes": self.minutes,
            "samples": self.samples,
            "mean_bpm": round(self.mean_bpm, 1),
            "peak_bpm": round(self.peak_bpm, 1),
            "excess_bpm": round(self.excess_bpm, 1),
            "unit": UNIT_BPM,
        }


@dataclass(frozen=True)
class ExerciseMask:
    """The spans to subtract before applying a resting rule, and what they cost.

    ``covers`` answers the question a detector asks minute by minute;
    :meth:`hrv_samples` answers the one an HRV window asks. The fractions say how much of
    the range was excluded, which is how a caller knows that a day which is 60% exercise
    has far less resting evidence than the raw sample count suggests.
    """

    since: datetime
    until: datetime
    threshold: float
    epoch_seconds: float
    heuristic: str = EXERCISE_HEURISTIC
    spans: tuple[Span, ...] = ()
    reference_bpm: float | None = None
    resting_multiple: float = DEFAULT_RESTING_MULTIPLE
    absolute_floor_bpm: float = DEFAULT_ABSOLUTE_FLOOR_BPM
    sustained_minutes: float = EXERCISE_SUSTAINED_MINUTES
    cooldown_minutes: float = DEFAULT_COOLDOWN_MINUTES
    measured_samples: int = 0
    excluded_samples: int = 0
    detail: dict[str, object] = field(default_factory=dict)

    # --- membership ---------------------------------------------------------
    def covers(self, instant: datetime) -> bool:
        """Was this instant exercise? (True only for a detected span.)"""
        return any(span.covers(instant) for span in self.spans)

    #: Read-as-English alias: ``mask.excludes(ts)`` reads better in a detector rule.
    def excludes(self, instant: datetime) -> bool:
        return self.covers(instant)

    def keeps(self, sample: Sample) -> bool:
        """Keep this sample for resting use?"""
        return not self.covers(sample.ts)

    @property
    def detected(self) -> bool:
        return bool(self.spans)

    # --- size ---------------------------------------------------------------
    @property
    def minutes_excluded(self) -> float:
        return round(sum(span.minutes for span in self.spans), 1)

    @property
    def fraction_excluded(self) -> float:
        """Fraction of the observed readings that fell inside a span, 0..1."""
        if not self.measured_samples:
            return 0.0
        return min(1.0, self.excluded_samples / self.measured_samples)

    @property
    def peak_bpm(self) -> float | None:
        return max((span.peak_bpm for span in self.spans), default=None)

    def excluded(self, samples: Iterable[Sample]) -> tuple[Sample, ...]:
        return tuple(sample for sample in samples if self.covers(sample.ts))

    def kept(self, samples: Iterable[Sample]) -> tuple[Sample, ...]:
        return tuple(sample for sample in samples if not self.covers(sample.ts))

    def hrv_samples(self, series: EpochSeries) -> tuple[Sample, ...]:
        """Measured readings of ``series`` with exercise removed - what HRV should use."""
        return self.kept(series.measured_samples)

    def values(self, series: EpochSeries) -> tuple[float, ...]:
        """The excluded rates - the evidence that the exclusion was justified."""
        return tuple(
            float(sample.bpm)
            for sample in self.excluded(series.measured_samples)
            if sample.bpm is not None
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "heuristic": self.heuristic,
            "since": self.since.isoformat(),
            "until": self.until.isoformat(),
            "unit": UNIT_BPM,
            "threshold_bpm": round(self.threshold, 1),
            "reference_bpm": None if self.reference_bpm is None else round(self.reference_bpm, 1),
            "resting_multiple": self.resting_multiple,
            "absolute_floor_bpm": self.absolute_floor_bpm,
            "sustained_minutes": self.sustained_minutes,
            "cooldown_minutes": self.cooldown_minutes,
            "spans": [span.as_dict() for span in self.spans],
            "minutes_excluded": self.minutes_excluded,
            "samples_excluded": self.excluded_samples,
            "measured_samples": self.measured_samples,
            "fraction_excluded": round(self.fraction_excluded, 4),
            "peak_bpm": None if self.peak_bpm is None else round(self.peak_bpm, 1),
            **({"detail": self.detail} if self.detail else {}),
        }


def build_exercise_mask(
    series: EpochSeries,
    *,
    resting_bpm: float | None = None,
    resting_multiple: float = DEFAULT_RESTING_MULTIPLE,
    absolute_floor_bpm: float = DEFAULT_ABSOLUTE_FLOOR_BPM,
    sustained_minutes: float = EXERCISE_SUSTAINED_MINUTES,
    cooldown_minutes: float = DEFAULT_COOLDOWN_MINUTES,
) -> ExerciseMask:
    """Mask out the parts of ``series`` that read as exercise.

    Only **measured** readings are considered: an imputed value between a resting minute
    and a resting minute is not evidence of a run. The spacing used for "sustained" is the
    series' effective epoch (the coarser of the catalog grain and the spacing actually
    present), so a 5-minute corpus is not judged as though it had sixty readings a minute.

    Pass ``resting_bpm`` from the resting window of the same range - see
    :func:`fitbit_monitor.hr.windows.detect_resting_window` - or leave it ``None`` to get
    the absolute-floor-only mask, which is the honest default for a wearer with no
    baseline yet.
    """
    epoch = series.effective_epoch_seconds
    limit = epoch * max(1.0, series.tolerance)
    threshold = threshold_bpm(
        resting_bpm, resting_multiple=resting_multiple, absolute_floor_bpm=absolute_floor_bpm
    )
    measured = series.measured_samples
    runs: list[list[Sample]] = []
    for sample in measured:
        value = sample.bpm
        candidate = value is not None and value >= threshold
        if not candidate:
            continue
        if runs:
            previous = runs[-1][-1]
            if (sample.ts - previous.ts).total_seconds() <= limit:
                runs[-1].append(sample)
                continue
        runs.append([sample])

    span_seconds = sustained_minutes * 60.0
    cooldown = timedelta(minutes=cooldown_minutes)
    spans: list[Span] = []
    for run in runs:
        first, last = run[0], run[-1]
        duration = (last.ts - first.ts).total_seconds() + epoch
        if duration < span_seconds:
            continue
        values = tuple(float(s.bpm) for s in run if s.bpm is not None)
        spans.append(
            Span(
                start=first.ts,
                end=last.ts + timedelta(seconds=epoch) + cooldown,
                samples=len(run),
                mean_bpm=mean(values),
                peak_bpm=max(values),
                excess_bpm=max(values) - threshold,
            )
        )
    spans = _merge(spans)
    excluded = sum(1 for sample in measured if any(span.covers(sample.ts) for span in spans))
    return ExerciseMask(
        since=series.since,
        until=series.until,
        threshold=threshold,
        epoch_seconds=epoch,
        spans=tuple(spans),
        reference_bpm=resting_bpm,
        resting_multiple=resting_multiple,
        absolute_floor_bpm=absolute_floor_bpm,
        sustained_minutes=sustained_minutes,
        cooldown_minutes=cooldown_minutes,
        measured_samples=len(measured),
        excluded_samples=excluded,
        detail={"sustained_epoch_seconds": epoch},
    )


def _merge(spans: Sequence[Span]) -> list[Span]:
    """Join spans that overlap once cooldown is applied, oldest first."""
    merged: list[Span] = []
    for span in sorted(spans, key=lambda item: item.start):
        if merged and span.start <= merged[-1].end:
            previous = merged[-1]
            end = max(previous.end, span.end)
            samples = previous.samples + span.samples
            merged[-1] = Span(
                start=previous.start,
                end=end,
                samples=samples,
                mean_bpm=(previous.mean_bpm * previous.samples + span.mean_bpm * span.samples) / samples,
                peak_bpm=max(previous.peak_bpm, span.peak_bpm),
                excess_bpm=max(previous.excess_bpm, span.excess_bpm),
            )
            continue
        merged.append(span)
    return merged


__all__ = [
    "BPM",
    "DEFAULT_ABSOLUTE_FLOOR_BPM",
    "DEFAULT_COOLDOWN_MINUTES",
    "DEFAULT_RESTING_MULTIPLE",
    "EXERCISE_SUSTAINED_MINUTES",
    "ExerciseMask",
    "Span",
    "build_exercise_mask",
    "threshold_bpm",
]
