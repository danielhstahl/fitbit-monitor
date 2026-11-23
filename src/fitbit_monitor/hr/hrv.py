"""Windowed HRV features - SDNN, RMSSD, pNN50 and an irregularity index - with the
sample count a value requires stated in the open.

Read this before using a number from here
-----------------------------------------
A Fitbit intraday heart-rate series does **not** contain R-R intervals. It contains one
average heart rate per epoch - 60 seconds for ``heart_rate`` in this catalog, 300
seconds in the synthetic corpus - and an RR interval is ~1000 ms. So every interval in
this module is an **estimate**: ``60000 / bpm`` for one reading, and the difference
between two consecutive same-epoch estimates for a successive difference.

The consequence is stated rather than hidden, because it decides what each feature can
be used for:

``RMSSD`` and the ``irregularity_index``
    Both are built from *successive* epoch-to-epoch differences, which is a 60-second
    difference, not a beat-to-beat one. Vagal respiratory sinus arrhythmia largely
    averages out over a minute, so these read far **lower** than chest-strap values for
    the same person, and they are only comparable with other values from this function.
    Use them for *relative* movement in one wearer - night 12 vs the median of the last
    30 nights - and never against a published RMSSD norm.
``SDNN``
    The standard deviation of the epoch-derived intervals in the window. It is the least
    affected of the three by the coarse sampling, and it is the one most often quoted in
    the literature as a *long-term* (24 h) measure anyway.
``pnn50``
    Fraction of successive differences past a threshold. The threshold is proportional to
    the window's own mean interval (see
    :func:`fitbit_monitor.hr.util.pnn50_threshold_ms`): 5% of it, which is exactly the
    classic 50 ms at a 60 bpm resting rate and stays equivalent at other rates, where the
    same variability would otherwise read as a smaller absolute difference.

Units: **milliseconds** for every interval feature (:data:`~fitbit_monitor.hr.util.UNIT_MS`),
a dimensionless ratio for ``pnn50`` (0..1) and ``irregularity_index``, ``bpm`` for
``mean_bpm``.

What is counted as a sample
---------------------------
Only **measured** readings, and only **adjacent** ones: an interval is emitted for two
samples exactly one epoch apart, so an interval never spans a gap, a refused value or an
imputed fill. A window with holes therefore loses intervals, and
:class:`HrvFeatures.reason` says so. Below ``min_intervals`` usable intervals - the
default :data:`MIN_INTERVALS`, eight - nothing is emitted: SDNN and RMSSD on a handful
of intervals are noise with a decimal point, and an anomaly rule is worse than useless
if it can fire on one. The refusal is reported, not silent.

Best window: the detected sleep window (see :mod:`fitbit_monitor.hr.windows`), which is
where the literature says to take HRV and where the wearer is not moving.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Sequence

from .epochs import EpochSeries, Sample
from .util import (
    BPM,
    HRV_HEURISTIC,
    MS,
    UNIT_MS,
    iso,
    mean,
    median,
    pnn50_threshold_ms,
    population_stdev,
)

#: Fewer successive intervals than this and no feature is emitted at all.
#
# Eight is a floor, not a recommendation: it is the point below which SDNN is dominated
# by the two or three intervals that happen to differ, and it matches the shortest
# window (about 9 one-minute readings) a nightly baseline can reasonably be built from.
# A caller with a stricter rule - the anomaly engine, for instance - raises it.
MIN_INTERVALS = 8


@dataclass(frozen=True)
class Interval:
    """One beat-interval estimate, and the two readings it came from.

    ``ms`` is :data:`~fitbit_monitor.hr.util.UNIT_MS`; ``span_seconds`` is the actual
    spacing of the two readings, which is how a caller can see whether the interval is
    at the series' grain or straddles something.
    """

    start: datetime
    end: datetime
    ms: float
    span_seconds: float

    @property
    def unit(self) -> str:
        return UNIT_MS

    def as_dict(self) -> dict[str, object]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "ms": round(self.ms, 1),
            "span_seconds": self.span_seconds,
            "unit": UNIT_MS,
        }


@dataclass(frozen=True)
class HrvFeatures:
    """One window's HRV features, or the stated reason there are none.

    Every feature is ``None`` unless :attr:`emitted`, and ``None`` means "not computed",
    never "zero". ``as_dict()`` is the shape a metric or a report should carry.
    """

    since: datetime | None = None
    until: datetime | None = None
    heuristic: str = HRV_HEURISTIC
    min_intervals: int = MIN_INTERVALS
    epoch_seconds: float = 60.0
    intervals: tuple[Interval, ...] = ()
    #: Pairs dropped because the two readings were not one epoch apart (a gap, a refused
    #: reading, an imputed fill): the caller learns the window was broken, not that it
    #: was politely rounded down.
    skipped_pairs: int = 0
    sample_count: int = 0
    reason: str | None = None
    detail: dict[str, object] = field(default_factory=dict)

    # --- sample counts ------------------------------------------------------
    @property
    def interval_count(self) -> int:
        return len(self.intervals)

    @property
    def emitted(self) -> bool:
        """Were the features computed? False means every value below is None."""
        return self.interval_count >= self.min_intervals

    @property
    def not_emitted_reason(self) -> str | None:
        if self.emitted:
            return None
        return self.reason or (
            f"{self.interval_count} successive intervals, need {self.min_intervals}"
        )

    # --- features -----------------------------------------------------------
    @property
    def values_ms(self) -> tuple[float, ...]:
        return tuple(interval.ms for interval in self.intervals)

    @property
    def _deltas(self) -> tuple[float, ...]:
        values = self.values_ms
        return tuple(b - a for a, b in zip(values, values[1:]))

    @property
    def mean_interval_ms(self) -> float | None:
        if not self.emitted:
            return None
        return mean(self.values_ms)

    @property
    def median_interval_ms(self) -> float | None:
        if not self.emitted:
            return None
        return median(self.values_ms)

    @property
    def mean_bpm(self) -> float | None:
        """Rate corresponding to the mean interval - a check on the window, not a rule."""
        interval = self.mean_interval_ms
        return None if interval is None else 60_000.0 / interval

    @property
    def sdnn_ms(self) -> float | None:
        """Standard deviation of the interval estimates in this window (population, n)."""
        if not self.emitted:
            return None
        return population_stdev(self.values_ms)

    @property
    def rmssd_ms(self) -> float | None:
        """Root mean square of successive interval differences, in ms. See the module doc."""
        deltas = self._deltas
        if not self.emitted or not deltas:
            return None
        return math.sqrt(sum(delta * delta for delta in deltas) / len(deltas))

    @property
    def pnn50(self) -> float | None:
        """Fraction of successive differences past the threshold (0..1), threshold in ms."""
        deltas = self._deltas
        if not self.emitted or not deltas:
            return None
        threshold = pnn50_threshold_ms(float(self.mean_interval_ms or 0.0))
        return sum(1 for delta in deltas if abs(delta) > threshold) / len(deltas)

    @property
    def pnn50_threshold_ms(self) -> float | None:
        """The threshold ``pnn50`` actually used, so a report can print it next to the ratio."""
        interval = self.mean_interval_ms
        return None if interval is None else pnn50_threshold_ms(interval)

    @property
    def irregularity_index(self) -> float | None:
        """A simple, scale-free irregularity measure: mean |Δinterval| / median interval.

        ``RMSSD`` divided by the window's own median interval, in effect: a steady rhythm
        reads near 0, a scattered one climbs, and dividing by the median means a night at
        a higher resting rate is not automatically called irregular. Over epoch-derived
        intervals this is an *epoch-to-epoch* scatter measure - see the module docstring -
        which is what makes it usable as a per-night trend rather than a diagnosis.
        """
        deltas = self._deltas
        centre = self.median_interval_ms
        if not self.emitted or not deltas or not centre:
            return None
        return (sum(abs(delta) for delta in deltas) / len(deltas)) / centre

    # --- output -------------------------------------------------------------
    def as_dict(self) -> dict[str, object]:
        def round_or(value: float | None, places: int = 2) -> float | None:
            return None if value is None else round(value, places)

        return {
            "heuristic": self.heuristic,
            "emitted": self.emitted,
            "reason": self.not_emitted_reason,
            "since": iso(self.since),
            "until": iso(self.until),
            "samples": self.sample_count,
            "intervals": self.interval_count,
            "min_intervals": self.min_intervals,
            "skipped_pairs": self.skipped_pairs,
            "epoch_seconds": self.epoch_seconds,
            "unit": UNIT_MS,
            "sdnn_ms": round_or(self.sdnn_ms),
            "rmssd_ms": round_or(self.rmssd_ms),
            "pnn50": round_or(self.pnn50, 4),
            "pnn50_threshold_ms": round_or(self.pnn50_threshold_ms),
            "irregularity_index": round_or(self.irregularity_index, 4),
            "mean_interval_ms": round_or(self.mean_interval_ms),
            "median_interval_ms": round_or(self.median_interval_ms),
            "mean_bpm": round_or(self.mean_bpm),
            **({"detail": self.detail} if self.detail else {}),
        }


def estimate_intervals(
    samples: Sequence[Sample], *, epoch_seconds: float, tolerance: float = 1.5
) -> tuple[tuple[Interval, ...], int]:
    """Interval estimates from consecutive samples, and how many pairs were skipped.

    A pair produces an interval only when both samples carry a **measured** value and
    they are ``<= tolerance`` epochs apart (default 1.5: one epoch, plus half an epoch so
    a series arriving on a slightly ragged 60 s cadence still counts). Everything else -
    a gap, a refused reading, an imputed value - is counted in ``skipped`` and shows up in
    :attr:`HrvFeatures.skipped_pairs`.

    Skipped pairs are counted instead of dropped quietly because the difference between
    "a quiet night with 6 readings" and "a broken night with 6 readings" is exactly what
    a confidence score needs.
    """
    usable = [sample for sample in samples if sample.measured and sample.bpm is not None]
    intervals: list[Interval] = []
    skipped = 0
    limit = epoch_seconds * max(1.0, tolerance)
    for before, after in zip(usable, usable[1:]):
        span = (after.ts - before.ts).total_seconds()
        if span <= 0 or span > limit:
            skipped += 1
            continue
        intervals.append(
            Interval(
                start=before.ts,
                end=after.ts,
                ms=60_000.0 / float(before.bpm),  # type: ignore[arg-type]
                span_seconds=span,
            )
        )
    # The last reading contributes an interval estimate of its own for the mean, but no
    # successive difference; the pair loop above is what feeds SDNN/RMSSD.
    return tuple(intervals), skipped


def hrv_features(
    source: EpochSeries | Sequence[Sample],
    *,
    epoch_seconds: float | None = None,
    tolerance: float = 1.5,
    min_intervals: int = MIN_INTERVALS,
    label: str = "",
) -> HrvFeatures:
    """SDNN, RMSSD, pNN50 and the irregularity index over one window.

    ``source`` is an :class:`~fitbit_monitor.hr.epochs.EpochSeries` (its own epoch length
    is used) or a plain sequence of samples - so a caller can pass
    ``mask.hrv_samples(series)`` or the readings of a detected window. Pass the readings
    of the window you want the features for; this function does not choose one for you.

    Nothing is emitted below ``min_intervals`` intervals; :attr:`HrvFeatures.reason`
    then says how many there were, and :attr:`HrvFeatures.skipped_pairs` says how many
    pairs were lost to gaps.
    """
    if isinstance(source, EpochSeries):
        samples: Sequence[Sample] = source.measured_samples
        grain = epoch_seconds or source.effective_epoch_seconds
        since: datetime | None = source.since
        until: datetime | None = source.until
    else:
        samples = tuple(source)
        grain = epoch_seconds or (samples[0].epoch_seconds if samples else 60.0)
        since = samples[0].ts if samples else None
        until = samples[-1].ts if samples else None
    intervals, skipped = estimate_intervals(samples, epoch_seconds=float(grain), tolerance=tolerance)
    features = HrvFeatures(
        since=since,
        until=until,
        epoch_seconds=float(grain),
        intervals=intervals,
        skipped_pairs=skipped,
        sample_count=sum(1 for sample in samples if sample.measured),
        min_intervals=min_intervals,
        detail={"label": label} if label else {},
    )
    if features.emitted:
        return features
    if features.reason is None:
        object.__setattr__(
            features, "reason",
            f"only {features.interval_count} successive interval"
            f"{'' if features.interval_count == 1 else 's'} at a {grain:g}s grain "
            f"(need {min_intervals}; {skipped} pair(s) lost to gaps)",
        )
    return features


__all__ = ["BPM", "MIN_INTERVALS", "MS", "HrvFeatures", "Interval", "estimate_intervals", "hrv_features"]
