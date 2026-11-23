"""Sleep and resting windows, with the heuristic that made each one named.

Two different questions get two different answers here:

* **when was the wearer asleep** (:func:`detect_sleep_window`), which is where a night's
  HRV lives and where a sustained low rate is least surprising; and
* **when were they at rest while awake** (:func:`detect_resting_window`), which is where
  a sustained *high* rate is the alarming one, and which is the window a personal
  resting-HR baseline should be built from.

Both are derived from heart rate alone, because that is what this store holds intraday:
the ``sleep`` dataset is one row per night with totals and no onset, so it cannot say
when sleep began. The heuristics are therefore load heuristics - heart rate falls when
the body goes quiet - and each one is *named* (``lowest-load-overnight-block``,
``quiet-hour-sustained-minimum``, see :mod:`fitbit_monitor.hr.util`) so a finding can
quote the rule that produced it.

The shape of both rules is the same, and deliberately simple:

1. restrict to a plausible region (the night, for sleep; everything outside the night,
   for rest);
2. smooth the rates over ``smoothing_minutes`` (15 by default) and find the contiguous
   block of at least ``block_minutes`` with the **lowest smoothed mean** - the minimum is
   taken over sustained blocks, so a single quiet minute in the afternoon is not mistaken
   for rest;
3. grow that block outwards while the *smoothed* rate stays within ``extend_slack_bpm``
   of the block's mean - or within two standard deviations of the block's own noise, if
   the block is noisier than the stated slack (see :data:`NOISE_FACTOR`) - the readings
   stay continuous, and no more than ``max_extension_minutes`` has been added in each
   direction.

Step 3 is why these are windows rather than samples: a person asleep from 23:10 to 06:20
should get a window that says so, and one whose data stopped at 02:00 should get a window
that stops at 02:00. The smoothing in step 2-3 is not decoration - wrist heart rate at
rest has a standard deviation of a couple of bpm, so an unsmoothed "stop at the first
reading more than 3 bpm above the floor" dies at the first ordinary sample and hands back
a one-hour window in the middle of a six-hour night. The window's :attr:`~TimeWindow.values`
are the **raw** readings inside the edges it settled on; only the decisions were smoothed.

Every function here returns a :class:`TimeWindow`, detected or not. When it could not
decide - a night with four readings, a day that is all exercise - it says so in ``reason``
rather than inventing a window, because a threshold compared against an invented window is
a false alarm waiting to happen. Detected windows are built from **measured** readings
only: values interpolated across a gap (:data:`~fitbit_monitor.hr.util.FLAG_IMPUTED`)
never move a window edge.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Iterable, Sequence

from .epochs import EpochSeries, Sample
from .util import (
    BPM,
    REST_HEURISTIC,
    SLEEP_HEURISTIC,
    UNIT_BPM,
    iso,
    local_clock,
    mean,
    median,
    minutes,
    population_stdev,
)

#: Narrowest block that counts as "sustained" for the daytime rest rule.
#:
#: Deliberately *not* the same constant as the exercise rule's sustained floor, which is
#: eight minutes: "a stretch quiet enough to be a resting baseline" and "a stretch long
#: enough to have been exercise" are different questions and were silently sharing one
#: name once, which made every exercise span in a day require half an hour of effort.
REST_SUSTAINED_MINUTES = 30.0
#: Below this many eligible minutes a window is not emitted at all.
DEFAULT_MIN_WINDOW_MINUTES = 15.0
#: How far a block is allowed to grow while looking for the real edge of a window.
#:
#: This is a runaway guard, not the rule that decides where a window ends - the rate rise
#: does that - so it has to sit above any window the rule is meant to be able to find.
#: Six hundred minutes each way, on top of a one-hour seed block, covers a twelve-hour
#: night; the overnight anchor (18:00 to noon) is the real outer bound. A value small
#: enough to bite (it was 240 once) silently truncates every long sleep at the same
#: distance from the block, which looks like a short night rather than a cap.
DEFAULT_MAX_EXTENSION_MINUTES = 600.0
#: Rate tolerance, in bpm, for "still the same quiet state".
DEFAULT_EXTEND_SLACK_BPM = 3.0
#: Smoothing width, in minutes, for the means the decisions are made on.
DEFAULT_SMOOTHING_MINUTES = 15.0
#: How long a window may stay above its tolerance before the excursion counts as waking.
#: (In the middle of the night a two-minute artefact or a trip to the bathroom is not a
#: waking episode; a half-hour of elevated rate is.)
DEFAULT_PATIENCE_MINUTES = 30.0
#: How many standard deviations of the block's own noise the growth tolerance is never
#: tighter than.
#
#: A block's mean sits below the level around it by about the block's own noise, so a
#: growth tolerance measured only in bpm cuts the window off at the first ordinary
#: reading. Taking "the larger of the stated slack and two standard deviations of the
#: block" makes the edge mean something about the body rather than about the sensor's
#: jitter - and it adapts to a wearer whose nights are noisier than someone else's.
NOISE_FACTOR = 2.0


@dataclass(frozen=True)
class TimeWindow:
    """A stretch of time a heuristic claimed, with the numbers that made it.

    ``start``/``end`` are half-open aware instants (``end`` exclusive), and both are
    ``None`` when nothing was detected - which is a normal, reported result, not an
    error. Read :attr:`detected` before using an edge.
    """

    kind: str
    heuristic: str
    start: datetime | None = None
    end: datetime | None = None
    values: tuple[float, ...] = ()
    epoch_seconds: float = 60.0
    reason: str | None = None
    detail: dict[str, object] = field(default_factory=dict)

    # --- is it a window at all ---------------------------------------------
    @property
    def detected(self) -> bool:
        return self.start is not None and self.end is not None and self.end > self.start

    def __bool__(self) -> bool:
        return self.detected

    @property
    def not_detected_reason(self) -> str | None:
        return None if self.detected else (self.reason or "no window detected")

    # --- what was in it -----------------------------------------------------
    @property
    def samples(self) -> int:
        return len(self.values)

    @property
    def span_seconds(self) -> float:
        """Seconds the window covers.

        ``end`` already sits one epoch past the last reading (the half-open convention), so
        the difference *is* the duration - adding an epoch here would make a 420-minute
        night report 421.
        """
        if not self.detected:
            return 0.0
        return (self.end - self.start).total_seconds()  # type: ignore[operator]

    @property
    def minutes(self) -> float:
        """Minutes the window covers, to 0.1 - the reporting unit for a duration."""
        return minutes(self.span_seconds)

    @property
    def mean_bpm(self) -> float | None:
        return mean(self.values) if self.values else None

    @property
    def median_bpm(self) -> float | None:
        return median(self.values) if self.values else None

    @property
    def min_bpm(self) -> float | None:
        return min(self.values) if self.values else None

    @property
    def max_bpm(self) -> float | None:
        return max(self.values) if self.values else None

    @property
    def unit(self) -> str:
        return UNIT_BPM

    def covers(self, instant: datetime) -> bool:
        """Is ``instant`` inside this window (half-open)?"""
        if not self.detected:
            return False
        return self.start <= instant < self.end  # type: ignore[operator]

    def overlaps(self, start: datetime | None, end: datetime | None) -> bool:
        """Does any part of this window fall inside ``[start, end)``?

        An undetected neighbour (``start`` or ``end`` is ``None``) overlaps nothing: a
        caller comparing today's rest against a sleep window that was never found has to
        get ``False``, not a ``TypeError``.
        """
        if not self.detected or start is None or end is None:
            return False
        return self.start < end and start < self.end  # type: ignore[operator]

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "heuristic": self.heuristic,
            "detected": self.detected,
            "start": iso(self.start),
            "end": iso(self.end),
            "minutes": self.minutes,
            "samples": self.samples,
            "unit": UNIT_BPM,
            "mean_bpm": None if self.mean_bpm is None else round(self.mean_bpm, 1),
            "median_bpm": None if self.median_bpm is None else round(self.median_bpm, 1),
            "min_bpm": None if self.min_bpm is None else round(self.min_bpm, 1),
            "max_bpm": None if self.max_bpm is None else round(self.max_bpm, 1),
            "reason": self.not_detected_reason,
            **({"detail": self.detail} if self.detail else {}),
        }


def not_detected(kind: str, heuristic: str, reason: str, **detail: object) -> TimeWindow:
    """The explicit "this heuristic could not decide" window."""
    return TimeWindow(kind=kind, heuristic=heuristic, reason=reason, detail=dict(detail))


def smoothing_count(epoch_seconds: float, smoothing_minutes: float) -> int:
    """How many readings a smoothing window covers (at least one)."""
    if smoothing_minutes <= 0:
        return 1
    return max(1, int(round(smoothing_minutes * 60.0 / max(1.0, epoch_seconds))))


def smoothed(values: Sequence[float], count: int) -> list[float]:
    """Centred moving mean of ``values`` over ``count`` samples (edges shrink to fit).

    Centred rather than trailing so a window edge is not pushed half a smoothing width
    late, and computed over *samples* rather than clock time so a series that skipped ten
    minutes does not silently average across the skip.
    """
    if count <= 1 or len(values) <= 1:
        return [float(value) for value in values]
    half = count // 2
    out: list[float] = []
    for index in range(len(values)):
        low = max(0, index - half)
        stop = min(len(values), index + half + 1)
        out.append(sum(values[low:stop]) / (stop - low))
    return out


def growth_slack(block_values: Sequence[float], slack_bpm: float) -> float:
    """The tolerance a block grows by: the stated slack, or the block's own noise.

    See :data:`NOISE_FACTOR`. Reported in the window's ``detail`` so a reader can see how
    wide the tolerance actually was on the night they are looking at.
    """
    if len(block_values) < 2:
        return slack_bpm
    return max(slack_bpm, NOISE_FACTOR * population_stdev(block_values))


def _pool(series: EpochSeries, *, exclude: Iterable[TimeWindow] = ()) -> tuple[Sample, ...]:
    """Measured, usable samples - the only evidence a window edge may move on."""
    holes = [window for window in exclude if window.detected]
    return tuple(
        sample
        for sample in series.measured_samples
        if not any(window.covers(sample.ts) for window in holes)
    )


def _series_of(samples: Sequence[Sample]) -> tuple[list[float], list[float]]:
    stamps = [sample.ts.timestamp() for sample in samples]
    values = [float(sample.bpm) for sample in samples if sample.bpm is not None]
    return stamps, values


def _best_block(
    stamps: Sequence[float],
    levels: Sequence[float],
    *,
    epoch_seconds: float,
    block_seconds: float,
    min_span_seconds: float,
) -> tuple[int, int, float] | None:
    """Contiguous run with the lowest mean rate; the earliest one wins an exact tie.

    A run is a set of consecutive samples whose span (first instant to last instant plus
    one epoch) fits inside ``block_seconds`` and covers at least ``min_span_seconds`` - so
    "lowest mean" always means "lowest mean over a sustained stretch", never over one
    lonely reading. Returns ``(first_index, one_past_last, mean_of_levels)``.
    """
    if len(levels) < 2:
        return None
    prefix = [0.0]
    for value in levels:
        prefix.append(prefix[-1] + value)
    best: tuple[int, int, float] | None = None
    for index, stamp in enumerate(stamps):
        limit = stamp + block_seconds - epoch_seconds + 1e-9
        stop = bisect.bisect_right(stamps, limit)
        count = stop - index
        if count < 2:
            continue
        span = stamps[stop - 1] - stamp + epoch_seconds
        if span < min_span_seconds:
            continue
        average = (prefix[stop] - prefix[index]) / count
        if best is None or average < best[2] - 1e-9:
            best = (index, stop, average)
    return best


def _grow(
    samples: Sequence[Sample],
    levels: Sequence[float],
    first: int,
    stop: int,
    *,
    centre_bpm: float,
    slack_bpm: float,
    epoch_seconds: float,
    tolerance: float,
    max_extension_seconds: float,
    patience_seconds: float = 0.0,
    blocked: Callable[[Sample], bool] = lambda _sample: False,
) -> tuple[int, int]:
    """Extend ``[first, stop)`` while the (smoothed) rate stays near ``centre_bpm``.

    An excursion above the tolerance is tolerated for up to ``patience_seconds`` - a short
    bump inside a night is not a waking episode - and the edges are trimmed afterwards so
    a tolerated bump at the very end does not lengthen the window. Stops at a break in the
    data longer than ``tolerance`` epochs, at a sample inside an excluded window, and at
    ``max_extension_seconds`` of added time in each direction - so a genuinely flat 24
    hours does not turn "the quietest half hour" into "the day".
    """
    reach = max(0.0, max_extension_seconds)
    step_limit = epoch_seconds * max(1.0, tolerance)
    ceiling = centre_bpm + slack_bpm

    def over(index: int) -> bool:
        return levels[index] > ceiling

    added = 0.0
    spent = 0.0
    while first > 0:
        candidate, previous = samples[first - 1], samples[first]
        gap = (previous.ts - candidate.ts).total_seconds()
        if gap > step_limit or blocked(candidate) or added + gap > reach:
            break
        if over(first - 1):
            spent += gap
            if spent > patience_seconds:
                break
        added += gap
        first -= 1
    added = 0.0
    spent = 0.0
    while stop < len(samples):
        candidate, previous = samples[stop], samples[stop - 1]
        gap = (candidate.ts - previous.ts).total_seconds()
        if gap > step_limit or blocked(candidate) or added + gap > reach:
            break
        if over(stop):
            spent += gap
            if spent > patience_seconds:
                break
        added += gap
        stop += 1
    # Trim back over any tolerated excursion the growth walked over at the edges.
    while first < stop - 1 and over(first):
        first += 1
    while stop - 1 > first and over(stop - 1):
        stop -= 1
    return first, stop


def _window_from(
    kind: str,
    heuristic: str,
    pool: Sequence[Sample],
    first: int,
    stop: int,
    *,
    epoch_seconds: float,
    detail: dict[str, object],
    centre_bpm: float,
) -> TimeWindow:
    chosen = pool[first:stop]
    values = tuple(float(sample.bpm) for sample in chosen if sample.bpm is not None)
    return TimeWindow(
        kind=kind,
        heuristic=heuristic,
        start=chosen[0].ts,
        end=chosen[-1].ts + timedelta(seconds=epoch_seconds),
        values=values,
        epoch_seconds=epoch_seconds,
        detail={**detail, "block_mean_bpm": round(centre_bpm, 1)},
    )


def detect_sleep_window(
    series: EpochSeries,
    *,
    tz=None,
    evening_hour: int = 18,
    morning_hour: int = 12,
    block_minutes: float = 60.0,
    min_block_minutes: float = 45.0,
    extend_slack_bpm: float = DEFAULT_EXTEND_SLACK_BPM,
    max_extension_minutes: float = DEFAULT_MAX_EXTENSION_MINUTES,
    patience_minutes: float = DEFAULT_PATIENCE_MINUTES,
    smoothing_minutes: float = DEFAULT_SMOOTHING_MINUTES,
    min_samples: int = 8,
) -> TimeWindow:
    """The night's sleep window, heuristic ``lowest-load-overnight-block``.

    The search is anchored to the wearer's clock, not to UTC: the region is local
    ``evening_hour``:00 on the local day ``series.since`` falls in through
    ``morning_hour``:00 the next day (18:00 → 12:00 by default), because "the night whose
    HRV belongs to 12 May" is a wall-clock idea and a UTC-anchored search hands a
    night-shift wearer the wrong twelve hours. The anchor is clipped to the range actually
    read, so a range that stops at 06:00 is honest about only having looked at that.

    Inside the region the lowest-mean ``block_minutes`` block (must span at least
    ``min_block_minutes`` of readings) is found, then grown while the smoothed rate stays
    within ``extend_slack_bpm`` of that block's mean.

    Why *lowest mean rate* rather than "below a fixed bpm": a threshold of 55 bpm calls a
    sedentary weekend day sleep and misses a feverish night at 78 bpm entirely. The lowest
    sustained block of the night is the night's own floor whatever the wearer's absolute
    level turns out to be - which is also what makes it usable as a personal baseline.

    Why the block grows instead of the whole night being "everything below a threshold":
    the fall and rise around sleep are gradual, and any fixed cut through them is an
    arbitrary hour. Growing from the floor outward by a fixed tolerance is the same
    judgement a human makes looking at the trace - "she was quiet from about eleven".
    """
    detail: dict[str, object] = {
        "anchored_local": f"{evening_hour:02d}:00-{morning_hour:02d}:00",
        "block_minutes": block_minutes,
        "min_block_minutes": min_block_minutes,
        "extend_slack_bpm": extend_slack_bpm,
        "smoothing_minutes": smoothing_minutes,
    }
    epoch = series.effective_epoch_seconds
    anchor_day = series.since.astimezone(tz).date() if tz is not None else series.since.date()
    start = max(series.since, local_clock(anchor_day, tz, evening_hour))
    end = min(series.until, local_clock(anchor_day + timedelta(days=1), tz, morning_hour))
    # Reported with the verdict: "the night I looked at" is part of what the window means,
    # and a window found in a range clipped to 06:00 should not read like a full night.
    detail["anchored"] = [iso(start), iso(end)]
    if end <= start:
        return not_detected(
            "sleep", SLEEP_HEURISTIC,
            f"the range {iso(series.since)} .. {iso(series.until)} contains no part of the night",
            **detail,
        )
    pool = _pool(series.slice(start, end))
    if len(pool) < min_samples:
        # Two different failures, and a caller reading the reason needs to tell them
        # apart: a night with nothing in it is a device left on the bedside table, a
        # night with three readings is a band that stopped reporting.
        observed = "no readings overnight" if not pool else f"only {len(pool)} measured readings overnight"
        return not_detected(
            "sleep", SLEEP_HEURISTIC,
            f"{observed} (need {min_samples})",
            samples=len(pool), **detail,
        )
    stamps, raw = _series_of(pool)
    levels = smoothed(raw, smoothing_count(epoch, smoothing_minutes))
    block = _best_block(
        stamps, levels,
        epoch_seconds=epoch,
        block_seconds=block_minutes * 60.0,
        min_span_seconds=min_block_minutes * 60.0,
    )
    if block is None:
        return not_detected(
            "sleep", SLEEP_HEURISTIC,
            f"no {min_block_minutes:g}-minute stretch of readings overnight to average",
            samples=len(pool), **detail,
        )
    first, stop, centre = block
    slack = growth_slack(raw[first:stop], extend_slack_bpm)
    first, stop = _grow(
        pool, levels, first, stop,
        centre_bpm=centre,
        slack_bpm=slack,
        epoch_seconds=epoch,
        tolerance=series.tolerance,
        max_extension_seconds=max_extension_minutes * 60.0,
        patience_seconds=patience_minutes * 60.0,
    )
    return _window_from(
        "sleep", SLEEP_HEURISTIC, pool, first, stop,
        epoch_seconds=epoch, detail={**detail, "growth_slack_bpm": round(slack, 2)}, centre_bpm=centre,
    )


def detect_resting_window(
    series: EpochSeries,
    *,
    exclude: Iterable[TimeWindow] = (),
    sustained_minutes: float = REST_SUSTAINED_MINUTES,
    min_minutes: float = DEFAULT_MIN_WINDOW_MINUTES,
    extend_slack_bpm: float = 2.0,
    max_extension_minutes: float = DEFAULT_MAX_EXTENSION_MINUTES,
    patience_minutes: float = DEFAULT_PATIENCE_MINUTES,
    smoothing_minutes: float = DEFAULT_SMOOTHING_MINUTES,
    min_samples: int = 4,
) -> TimeWindow:
    """The awake-resting window, heuristic ``quiet-hour-sustained-minimum``.

    The sustained minimum *outside* the excluded windows (pass the sleep window): the
    quietest ``sustained_minutes`` of the wearer's waking hours, grown out while the
    smoothed rate stays within ``extend_slack_bpm`` of that block. If the range is shorter
    than ``sustained_minutes`` the block shrinks to ``min_minutes``; below ``min_minutes``
    of eligible readings nothing is emitted, and ``reason`` says how few there were.

    Excluding sleep is not a nicety: without it the resting window is always the middle of
    the night, the resting rate is the sleeping rate, and every "elevated rate at rest"
    rule downstream either never fires or fires at breakfast.

    Why the *sustained minimum* rather than, say, the day's 5th percentile: a percentile
    has no edges, so it cannot be handed to a rule that needs a start and an end, and it
    quietly mixes "at a desk" with "asleep on the sofa" into one number. A window has a
    duration, and a duration is what lets the exercise mask and the sustained-rate rules
    agree about what "at rest" meant at 14:20.
    """
    holes = [window for window in exclude if window.detected]
    pool = _pool(series, exclude=holes)
    epoch = series.effective_epoch_seconds
    detail: dict[str, object] = {
        "sustained_minutes": sustained_minutes,
        "min_minutes": min_minutes,
        "extend_slack_bpm": extend_slack_bpm,
        "smoothing_minutes": smoothing_minutes,
        "excluded_windows": [window.kind for window in holes],
    }
    if len(pool) < min_samples:
        return not_detected(
            "rest", REST_HEURISTIC,
            f"only {len(pool)} eligible readings outside the sleep window (need {min_samples})",
            **detail,
        )
    stamps, raw = _series_of(pool)
    levels = smoothed(raw, smoothing_count(epoch, smoothing_minutes))
    eligible_seconds = stamps[-1] - stamps[0] + epoch
    blocked = lambda sample: any(window.covers(sample.ts) for window in holes)  # noqa: E731
    for block_minutes in (sustained_minutes, min_minutes):
        if block_minutes * 60.0 > eligible_seconds and block_minutes != min_minutes:
            continue
        block = _best_block(
            stamps, levels,
            epoch_seconds=epoch,
            block_seconds=block_minutes * 60.0,
            min_span_seconds=min_minutes * 60.0,
        )
        if block is None:
            continue
        first, stop, centre = block
        slack = growth_slack(raw[first:stop], extend_slack_bpm)
        first, stop = _grow(
            pool, levels, first, stop,
            centre_bpm=centre,
            slack_bpm=slack,
            epoch_seconds=epoch,
            tolerance=series.tolerance,
            max_extension_seconds=max_extension_minutes * 60.0,
            patience_seconds=patience_minutes * 60.0,
            blocked=blocked,
        )
        return _window_from(
            "rest", REST_HEURISTIC, pool, first, stop,
            epoch_seconds=epoch,
            detail={**detail, "block_minutes_used": block_minutes, "growth_slack_bpm": round(slack, 2)},
            centre_bpm=centre,
        )
    return not_detected(
        "rest", REST_HEURISTIC,
        f"only {minutes(eligible_seconds)} eligible minutes outside the sleep window "
        f"(need {min_minutes:g})",
        **detail,
    )


__all__ = [
    "BPM",
    "DEFAULT_MIN_WINDOW_MINUTES",
    "DEFAULT_PATIENCE_MINUTES",
    "REST_SUSTAINED_MINUTES",
    "TimeWindow",
    "detect_resting_window",
    "detect_sleep_window",
    "growth_slack",
    "not_detected",
    "smoothing_count",
    "smoothed",
]
