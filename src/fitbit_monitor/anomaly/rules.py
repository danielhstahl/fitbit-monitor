"""The three rules: what has to be true for a stretch of data to become an episode.

Rules here are *mechanical*: they take one day's in-context readings and a threshold that
somebody else derived, and they return episodes or nothing. They do not score confidence
(that is :mod:`fitbit_monitor.anomaly.engine`, because it needs the baseline's maturity as
well as the episode), they do not talk about what an episode means, and they contain no
threshold constants of their own - every number arrives in an
:class:`~fitbit_monitor.anomaly.config.AnomalyConfig`.

The sustained-rate rule (``sustained_high``, ``sustained_low``)
-------------------------------------------------------------
One direction per call, because the two halves are the same rule read backwards:

1. smooth the raw rates with the library's centred moving mean
   (:func:`fitbit_monitor.hr.windows.smoothed`, ``smoothing_minutes`` wide) - wrist heart
   rate at rest carries a couple of bpm of jitter and a raw threshold would fragment one
   episode into fifteen one-minute ones;
2. take the maximal runs whose *smoothed* rate is at or past the threshold;
3. grow a run across a dip or a dropout of up to ``merge_gap_minutes`` so that one episode
   with a momentary break stays one episode;
4. hand the runs back as episodes measured on the **raw** values inside them - the peak (or
   trough), the mean, the sample count, and a coverage figure - because a reported finding
   must be quotable from the readings themselves, not from a smoothed curve nobody can
   point at.

Nothing here has a duration test: "was it long enough to be a finding" is decided after
merging, by the engine's gate, so that two thirteen-minute stretches separated by a
five-minute dip - which is one thirty-one-minute episode - are not suppressed as two
thirteen-minute ones.

The irregular-rhythm rule (``irregular_rhythm``)
------------------------------------------------
**The stated statistical basis: a scale-free scatter statistic compared to the wearer's own
distribution of it by robust z-score.**

The statistic is the feature library's epoch-derived irregularity index,
:attr:`fitbit_monitor.hr.hrv.HrvFeatures.irregularity_index`:

``index = mean(|Δ interval|) / median(interval)`` with ``interval = 60000 / bpm``

The numerator is the mean absolute successive difference of the beat-interval estimates -
what an ECG person would call a shortened, epoch-sampled neighbour of RMSSD - and dividing
by the median interval makes it scale-free, so a wearer with a higher resting rate is not
automatically "more irregular" and a coarse import is not automatically worse. A steady
rhythm reads near zero; a scatter of intervals reads high. :func:`scatter_index` is the
same formula on the interval estimates of one block, and it is the whole of the "rhythm"
half of the rule.

The threshold is :attr:`~fitbit_monitor.anomaly.baseline.ScatterBaseline.threshold`: the
median of the wearer's own block indices plus ``irregularity_z`` robust standard deviations
of that same distribution (MAD-scaled), floored at ``irregularity_floor``. That is an
Iglewicz-Hoaglin-style robust z test, and it is what makes the rule personal rather than a
borrowed HRV norm: the coarse index above is not comparable with chest-strap values, so the
only comparison that means anything is against the same wearer's own quiet windows. It
needs an *episode*, though, not one noisy half hour: an episode is ``irregularity_min_blocks``
consecutive scored blocks over the threshold, spanning at least ``irregularity_min_minutes``.

One context note that is easy to get wrong: intervals are only formed from samples
``<= 1.5`` epochs apart (the library's own rule), so a block that straddles a dropout simply
has fewer intervals and may fall below ``irregularity_min_intervals`` and go unscored. A
block that was not scored is not a block with a low index, and it never contributes a
finding.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Sequence

from ..hr.hrv import estimate_intervals
from ..hr.util import BPM, mean, median, minutes
from ..hr.windows import smoothing_count, smoothed
from .config import AnomalyConfig
from .context import ContextDay
from .models import FINDING_IRREGULAR, FINDING_SUSTAINED_HIGH, FINDING_SUSTAINED_LOW

#: Direction of the sustained-rate rule: ``+1`` looks upwards, ``-1`` downwards. The
#: finding type follows from it, which is why there is one implementation and not two.
DIRECTION_HIGH = 1
DIRECTION_LOW = -1

#: The rule names a finding quotes. ``relative-sustained-*`` says *relative to what*, which
#: is the whole point: the threshold came from a baseline, not from a table.
RATE_HEURISTIC = "personal-baseline-sustained-rate"
SCATTER_HEURISTIC = "personal-baseline-scatter-index"


@dataclass(frozen=True)
class Episode:
    """One candidate episode, before it has been merged, gated or scored.

    Carries the raw points it was made of so the engine can recompute everything it
    reports from the readings themselves, and the threshold it crossed so a report can
    print "``observed`` against ``threshold``".
    """

    type: str
    context: str
    day: date
    onset: datetime
    offset: datetime
    points: tuple[tuple[datetime, float], ...]
    threshold: float
    heuristic: str
    unit: str
    #: The spacing the episode was measured at, in seconds - what a duration is counted in.
    epoch_seconds: float = 60.0
    #: Seconds actually observed inside the episode. Defaults to ``samples * epoch``; the
    #: scatter rule sets it explicitly because its "points" are blocks, not readings.
    observed_seconds: float | None = None
    #: Raw readings backing the episode (readings for a rate episode, readings summed over
    #: blocks for a scatter one), which is what the "too few readings" gate looks at.
    readings: int | None = None
    #: For ``irregular_rhythm`` these are the per-block scatter indices rather than rates,
    #: and ``block_detail`` says what each one covers.
    detail: dict[str, object] | None = None

    # --- measurements over the raw points -----------------------------------
    @property
    def values(self) -> tuple[float, ...]:
        return tuple(value for _stamp, value in self.points)

    @property
    def samples(self) -> int:
        return len(self.points)

    @property
    def observed(self) -> float:
        """The extreme that carried the finding: peak high, trough low, largest index."""
        values = self.values
        if self.type == FINDING_SUSTAINED_LOW:
            return min(values)
        return max(values)

    @property
    def mean(self) -> float:
        return mean(self.values)

    @property
    def span_seconds(self) -> float:
        return (self.offset - self.onset).total_seconds()

    @property
    def duration_minutes(self) -> float:
        return minutes(self.span_seconds)

    @property
    def reading_count(self) -> int:
        return self.readings if self.readings is not None else self.samples

    @property
    def coverage(self) -> float:
        """Observed seconds over claimed seconds: what a duration is actually worth.

        An hour with a forty-minute hole in it is a twenty-minute episode, and a finding
        that reported the hour would be quoting a duration it did not see.
        """
        span = self.span_seconds
        if span <= 0:
            return 1.0
        observed = (
            self.observed_seconds
            if self.observed_seconds is not None
            else self.samples * self.epoch_seconds
        )
        return max(0.0, min(1.0, observed / span))

    def merged_with(self, other: "Episode") -> "Episode":
        """This episode plus a later one of the same kind, as one episode.

        The threshold and heuristic are the same by construction (same rule, same context,
        same baseline); the points concatenate, so every statistic the engine reports is
        recomputed over the whole merged stretch.
        """
        if other.type != self.type or other.context != self.context:
            raise ValueError("only episodes of the same type and context can be merged")
        detail = dict(self.detail or {})
        detail.update(other.detail or {})
        observed = (
            self.observed_seconds
            if self.observed_seconds is not None
            else self.samples * self.epoch_seconds
        ) + (
            other.observed_seconds
            if other.observed_seconds is not None
            else other.samples * other.epoch_seconds
        )
        readings = (self.reading_count) + (other.reading_count)
        return Episode(
            type=self.type,
            context=self.context,
            day=self.day,
            onset=min(self.onset, other.onset),
            offset=max(self.offset, other.offset),
            points=tuple(sorted(self.points + other.points, key=lambda point: point[0])),
            threshold=self.threshold,
            heuristic=self.heuristic,
            unit=self.unit,
            epoch_seconds=self.epoch_seconds,
            observed_seconds=observed,
            readings=readings,
            detail=detail or None,
        )


def _runs_matching(flags: Sequence[bool]) -> list[tuple[int, int]]:
    """Contiguous ``(first, one-past-last)`` index ranges where ``flags`` is True."""
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for index, flag in enumerate(flags):
        if flag and start is None:
            start = index
        elif not flag and start is not None:
            runs.append((start, index))
            start = None
    if start is not None:
        runs.append((start, len(flags)))
    return runs


def _merge_runs(
    runs: Sequence[tuple[int, int]],
    stamps: Sequence[datetime],
    *,
    epoch_seconds: float,
    merge_gap_seconds: float,
) -> list[tuple[int, int]]:
    """Grow each run forward across a gap of at most ``merge_gap_seconds`` of time."""
    merged: list[tuple[int, int]] = []
    for first, stop in runs:
        if merged:
            previous_first, previous_stop = merged[-1]
            gap = stamps[first].timestamp() - (
                stamps[previous_stop - 1].timestamp() + epoch_seconds
            )
            if gap <= merge_gap_seconds:
                merged[-1] = (previous_first, stop)
                continue
        merged.append((first, stop))
    return merged


def sustained_rate_episodes(
    day: ContextDay,
    config: AnomalyConfig,
    *,
    direction: int,
    threshold: float,
) -> list[Episode]:
    """Every stretch of one day's context that stays at or past ``threshold``.

    See the module docstring for the rule; the returned episodes have had no duration or
    coverage test applied, because the engine gates them after merging.
    """
    points = day.points
    if len(points) < 2:
        return []
    stamps = tuple(stamp for stamp, _value in points)
    values = [value for _stamp, value in points]
    levels = smoothed(values, smoothing_count(day.epoch_seconds, config.smoothing_minutes))
    if direction == DIRECTION_HIGH:
        flags = [level >= threshold for level in levels]
    else:
        flags = [level <= threshold for level in levels]
    finding_type = FINDING_SUSTAINED_HIGH if direction == DIRECTION_HIGH else FINDING_SUSTAINED_LOW
    if direction == DIRECTION_HIGH:
        side = lambda value: value >= threshold  # noqa: E731 - one local, read once
    else:
        side = lambda value: value <= threshold  # noqa: E731
    # The smoother is what makes a real episode findable at all - one skipped reading in a
    # five-minute stretch would otherwise break the run - but it is a *centred* mean, so it
    # starts firing a few minutes before the rate actually moved and stops a few minutes
    # after. The raw readings know where the change was, so every flagged run is trimmed back
    # to the readings that are themselves on the abnormal side of the bar *before* runs are
    # merged: the merge gap is a statement about how far apart two real stretches may be, and
    # measuring it between smoothed envelopes would quietly widen it by the smoothing half
    # width on each side. The smoother decides that something happened; the readings decide when.
    trimmed: list[tuple[int, int]] = []
    for first, stop in _runs_matching(flags):
        while first < stop and not side(values[first]):
            first += 1
        while stop > first and not side(values[stop - 1]):
            stop -= 1
        if stop > first:
            trimmed.append((first, stop))
    runs = _merge_runs(
        trimmed,
        stamps,
        epoch_seconds=day.epoch_seconds,
        merge_gap_seconds=config.merge_gap_minutes * 60.0,
    )
    out: list[Episode] = []
    for first, stop in runs:
        if stop - first < 2:
            continue
        out.append(
            Episode(
                type=finding_type,
                context=day.context,
                day=day.day,
                onset=stamps[first],
                offset=stamps[stop - 1] + timedelta(seconds=day.epoch_seconds),
                points=tuple(points[first:stop]),
                threshold=threshold,
                heuristic=RATE_HEURISTIC,
                unit=BPM,
                epoch_seconds=day.epoch_seconds,
                readings=stop - first,
                detail={
                    "smoothing_minutes": config.smoothing_minutes,
                    "epoch_seconds": day.epoch_seconds,
                },
            )
        )
    return out


def scatter_index(intervals_ms: Sequence[float]) -> float | None:
    """``mean(|Δinterval|) / median(interval)`` - the statistical basis of the rhythm rule.

    ``None`` when there are fewer than two intervals to difference. Identical in definition
    to :attr:`fitbit_monitor.hr.hrv.HrvFeatures.irregularity_index` so a block's value and
    a whole window's value from the feature library are the same quantity.
    """
    if len(intervals_ms) < 2:
        return None
    differences = [abs(b - a) for a, b in zip(intervals_ms, intervals_ms[1:])]
    centre = median(intervals_ms)
    if not centre:
        return None
    return mean(differences) / centre


#: Fewer scored intervals than this in a sliding window - which only happens at the two ends
#: of a day's domain - and the window is not scored at all. Four intervals is three successive
#: differences: the shortest stretch whose mean absolute difference is a measurement rather
#: than an anecdote.
MIN_SCORED_INTERVALS = 4


@dataclass(frozen=True)
class ScatterPoint:
    """One sliding window's scatter index, and the span of intervals it was measured over."""

    onset: datetime
    offset: datetime
    index: float
    intervals: int
    epoch_seconds: float

    @property
    def duration_minutes(self) -> float:
        return minutes((self.offset - self.onset).total_seconds())


def scatter_window_size(config: AnomalyConfig, epoch_seconds: float) -> int:
    """How many intervals a scatter window spans on data at ``epoch_seconds``.

    ``irregularity_min_intervals`` is the count the index needs to mean something, and
    ``irregularity_block_minutes`` is the *width* it should not be narrower than; on a coarse
    import the two disagree and the interval count wins, because thinning a window below the
    intervals it needs would make the statistic lie. The width actually used is reported with
    every finding rather than being implied by the config.
    """
    from_config = int(round(config.irregularity_block_minutes * 60.0 / max(1.0, epoch_seconds))) - 1
    return max(config.irregularity_min_intervals, from_config, MIN_SCORED_INTERVALS + 1)


def scatter_windows(day: ContextDay, config: AnomalyConfig) -> list[ScatterPoint]:
    """The sliding scatter index over one day's detection domain, oldest first.

    Centred, one position per interval, exactly like the moving mean in the rate rule - which
    is what gives this rule an onset finer than its own window and lets a short episode that
    sits in the middle of a long quiet stretch still be found (a partition into fixed blocks
    would either be too wide to notice it or too thin to measure it). Each position is scored
    against the same number of intervals wherever possible, so the values a day produces are
    comparable with the values every other day produced - which is what the personal baseline
    in :mod:`fitbit_monitor.anomaly.baseline` pools.
    """
    epoch = day.epoch_seconds
    samples = day.detection_samples
    if len(samples) < MIN_SCORED_INTERVALS + 1:
        return []
    intervals, _skipped = estimate_intervals(samples, epoch_seconds=epoch)
    if len(intervals) < MIN_SCORED_INTERVALS:
        return []
    window = scatter_window_size(config, epoch)
    half = window // 2
    values = [interval.ms for interval in intervals]
    out: list[ScatterPoint] = []
    for centre in range(len(intervals)):
        low = max(0, centre - half)
        stop = min(len(intervals), centre + half + 1)
        chunk = values[low:stop]
        if len(chunk) < MIN_SCORED_INTERVALS:
            continue
        index = scatter_index(chunk)
        if index is None:
            continue
        out.append(
            ScatterPoint(
                onset=intervals[low].start,
                offset=intervals[stop - 1].end,
                index=index,
                intervals=len(chunk),
                epoch_seconds=epoch,
            )
        )
    return out


def scatter_episodes(
    day: ContextDay,
    points: Sequence[ScatterPoint],
    *,
    threshold: float,
    config: AnomalyConfig,
) -> list[Episode]:
    """Every run of consecutive over-threshold scatter windows in one day, as episodes.

    Runs are merged across a break of up to ``merge_gap_minutes`` - the same discipline as the
    rate rule, so one episode with a two-minute dropout in it is one episode - and each run
    has to contain at least ``irregularity_min_blocks`` scored positions. Whether the result
    is long or dense enough to report is the engine's gate, not this function's.
    """
    if not points:
        return []
    stamps = [point.onset for point in points]
    flags = [point.index >= threshold for point in points]
    runs = _merge_runs(
        _runs_matching(flags),
        stamps,
        epoch_seconds=day.epoch_seconds,
        merge_gap_seconds=config.merge_gap_minutes * 60.0,
    )
    out: list[Episode] = []
    for first, stop in runs:
        run = list(points[first:stop])
        if len(run) < config.irregularity_min_blocks:
            continue
        onset, offset = run[0].onset, run[-1].offset
        readings = sum(1 for sample in day.detection_samples if onset <= sample.ts < offset)
        out.append(
            Episode(
                type=FINDING_IRREGULAR,
                context=day.context,
                day=day.day,
                onset=onset,
                offset=offset + timedelta(seconds=day.epoch_seconds),
                points=tuple((point.onset, point.index) for point in run),
                threshold=threshold,
                heuristic=SCATTER_HEURISTIC,
                unit="irregularity index (dimensionless)",
                epoch_seconds=day.epoch_seconds,
                observed_seconds=readings * day.epoch_seconds,
                readings=readings,
                detail={
                    "scatter_points": len(run),
                    "scatter_window_intervals": run[0].intervals,
                    "scatter_window_minutes": round(
                        (run[0].offset - run[0].onset).total_seconds() / 60.0, 1
                    ),
                    "epoch_seconds": day.epoch_seconds,
                },
            )
        )
    return out


__all__ = [
    "DIRECTION_HIGH",
    "DIRECTION_LOW",
    "MIN_SCORED_INTERVALS",
    "RATE_HEURISTIC",
    "SCATTER_HEURISTIC",
    "Episode",
    "ScatterPoint",
    "scatter_episodes",
    "scatter_index",
    "scatter_window_size",
    "scatter_windows",
    "sustained_rate_episodes",
]
