"""One row per local day: the series, the windows, the exclusion and the HRV together.

The pieces in :mod:`fitbit_monitor.hr.epochs`, :mod:`~fitbit_monitor.hr.windows`,
:mod:`~fitbit_monitor.hr.exercise` and :mod:`~fitbit_monitor.hr.hrv` are individually
useful, but a detector and a report both want the same joined-up question answered per
day: *what did this day's resting window look like, how much of it was exercise, and what
HRV can be claimed from it*. That join is where the day-boundary and timezone decisions
get made, so it lives in one place rather than being re-done at each call site.

Days are **local** days (the ``tz`` passed in, UTC when none is given), anchored at
wall-clock midnight. A UTC day would split one night of sleep across two rows for anyone
west of Greenwich, which would put the night's HRV in a different row from the bedtime
that produced it - and every per-night baseline would be off by a day for half the
userbase. Around a DST change a local day is 23 or 25 hours long, and the row says so
through :attr:`~fitbit_monitor.hr.epochs.EpochSeries.span_seconds`.

The sleep window belongs to the night that *began* on that day, so a row can carry a
sleep window that runs past local midnight into the next morning; the rest window is
whatever survived outside it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo
from typing import Iterable, Sequence

from ..store.reader import ReadOnlyStore
from .epochs import DEFAULT_GAP_TOLERANCE, EpochSeries, Sample, epoch_series
from .exercise import (
    DEFAULT_ABSOLUTE_FLOOR_BPM,
    DEFAULT_COOLDOWN_MINUTES,
    DEFAULT_RESTING_MULTIPLE,
    EXERCISE_SUSTAINED_MINUTES,
    ExerciseMask,
    build_exercise_mask,
)
from .hrv import MIN_INTERVALS, HrvFeatures, hrv_features
from .util import day_sequence, local_clock, local_day_bounds, resolve_tz_name
from .windows import (
    DEFAULT_MIN_WINDOW_MINUTES,
    DEFAULT_PATIENCE_MINUTES,
    DEFAULT_SMOOTHING_MINUTES,
    REST_SUSTAINED_MINUTES,
    TimeWindow,
    detect_resting_window,
    detect_sleep_window,
    not_detected,
)

#: Which window HRV is computed over. ``sleep_first`` is the default: HRV is a resting
#: measure, the sleep window is the most reproducible resting window there is, and when
#: there is no sleep window the awake resting window is the next honest candidate.
HRV_WINDOWS = ("sleep_first", "sleep", "rest", "day")

#: The two "sustained" floors are different numbers on purpose: eight minutes of elevation
#: is exercise, thirty quiet minutes is a resting baseline. They are imported under names
#: that say which rule they belong to, because when they shared the name
#: ``DEFAULT_SUSTAINED_MINUTES`` the second import silently won and every exercise span in
#: a day had to last half an hour to be excluded at all.


@dataclass(frozen=True)
class FeatureOptions:
    """Every tunable assumption in one place, with the defaults this library ships.

    A plugin (see the hr-anomaly epic) maps its declared options onto one of these and
    passes it in, so "what thresholds produced this alert" has one answer to print -
    which is the point of keeping the constants here rather than inline.
    """

    tz: tzinfo | None = None
    epoch_seconds: float | None = None
    tolerance: float = DEFAULT_GAP_TOLERANCE
    impute_seconds: float | None = None
    min_plausible_bpm: float = 25.0
    max_plausible_bpm: float = 220.0
    sleep_block_minutes: float = 60.0
    sleep_min_block_minutes: float = 45.0
    sleep_extend_slack_bpm: float = 3.0
    sleep_patience_minutes: float = DEFAULT_PATIENCE_MINUTES
    sleep_smoothing_minutes: float = DEFAULT_SMOOTHING_MINUTES
    evening_hour: int = 18
    morning_hour: int = 12
    rest_sustained_minutes: float = REST_SUSTAINED_MINUTES
    rest_min_minutes: float = DEFAULT_MIN_WINDOW_MINUTES
    rest_extend_slack_bpm: float = 2.0
    rest_patience_minutes: float = DEFAULT_PATIENCE_MINUTES
    rest_smoothing_minutes: float = DEFAULT_SMOOTHING_MINUTES
    exercise_resting_multiple: float = DEFAULT_RESTING_MULTIPLE
    exercise_absolute_floor_bpm: float = DEFAULT_ABSOLUTE_FLOOR_BPM
    exercise_sustained_minutes: float = EXERCISE_SUSTAINED_MINUTES
    exercise_cooldown_minutes: float = DEFAULT_COOLDOWN_MINUTES
    min_intervals: int = MIN_INTERVALS
    hrv_window: str = "sleep_first"
    dataset: str = "heart_rate"
    field: str = "hr"

    def __post_init__(self) -> None:
        if self.hrv_window not in HRV_WINDOWS:
            raise ValueError(f"hrv_window must be one of {HRV_WINDOWS}, got {self.hrv_window!r}")

    @property
    def tz_name(self) -> str | None:
        return resolve_tz_name(self.tz)

    def as_dict(self) -> dict[str, object]:
        return {
            "timezone": self.tz_name,
            "epoch_seconds": self.epoch_seconds,
            "gap_tolerance_epochs": self.tolerance,
            "impute_seconds": self.impute_seconds,
            "plausible_band_bpm": [self.min_plausible_bpm, self.max_plausible_bpm],
            "sleep_block_minutes": self.sleep_block_minutes,
            "sleep_min_block_minutes": self.sleep_min_block_minutes,
            "sleep_extend_slack_bpm": self.sleep_extend_slack_bpm,
            "sleep_patience_minutes": self.sleep_patience_minutes,
            "rest_patience_minutes": self.rest_patience_minutes,
            "sleep_smoothing_minutes": self.sleep_smoothing_minutes,
            "rest_smoothing_minutes": self.rest_smoothing_minutes,
            "overnight_anchor_local": f"{self.evening_hour:02d}:00-{self.morning_hour:02d}:00",
            "rest_sustained_minutes": self.rest_sustained_minutes,
            "rest_min_minutes": self.rest_min_minutes,
            "rest_extend_slack_bpm": self.rest_extend_slack_bpm,
            "exercise_resting_multiple": self.exercise_resting_multiple,
            "exercise_absolute_floor_bpm": self.exercise_absolute_floor_bpm,
            "exercise_sustained_minutes": self.exercise_sustained_minutes,
            "exercise_cooldown_minutes": self.exercise_cooldown_minutes,
            "min_intervals": self.min_intervals,
            "hrv_window": self.hrv_window,
        }


@dataclass(frozen=True)
class DayFeatures:
    """Everything the library can say about one local day.

    ``sleep`` and ``rest`` are windows whether or not they were detected (read
    :attr:`~fitbit_monitor.hr.windows.TimeWindow.detected`), ``exercise`` is the day's
    mask, ``hrv`` was computed over the window named by :attr:`hrv_window`, and
    :attr:`series` is the underlying per-epoch data - so a caller can check the evidence
    for every claim instead of taking the summary on trust.
    """

    day: date
    series: EpochSeries
    sleep: TimeWindow
    rest: TimeWindow
    exercise: ExerciseMask
    hrv: HrvFeatures
    hrv_window: str
    options: FeatureOptions = field(default=FeatureOptions())
    #: The tail of the night that began *yesterday*. Excluded from the resting window - it
    #: is sleep, whatever row it lands in - but not reported as this day's sleep, because a
    #: night belongs to the day it started on and reporting it twice would count one night
    #: twice in a baseline.
    previous_sleep: TimeWindow = field(
        default_factory=lambda: not_detected("sleep", "lowest-load-overnight-block", "not evaluated")
    )
    #: Readings inside the resting window, and how many of them survived the exercise mask.
    resting_samples: int = 0
    resting_samples_after_exclusion: int = 0

    @property
    def timezone(self) -> str | None:
        return self.options.tz_name

    @property
    def resting_bpm(self) -> float | None:
        """The day's resting rate: the mean of the resting window, if there was one.

        This is the value a personal baseline is built from - deliberately *not* the
        sleeping rate and *not* the day's mean, either of which a resting threshold would
        misread.
        """
        return self.rest.mean_bpm

    @property
    def has_data(self) -> bool:
        return self.series.has_data

    @property
    def usable(self) -> bool:
        """Is there enough here for a rule to reason about?

        The weakest day the detector should see: a resting window that survived the
        exercise mask with at least a couple of readings in it. Stricter than "has_data"
        on purpose - a day with one 2 a.m. reading has data, and no baseline.
        """
        return self.resting_samples_after_exclusion >= 2

    def as_dict(self) -> dict[str, object]:
        return {
            "day": self.day.isoformat(),
            "timezone": self.timezone,
            "since": self.series.since.isoformat(),
            "until": self.series.until.isoformat(),
            "span_minutes": round(self.series.span_seconds / 60.0, 1),
            "has_data": self.has_data,
            "usable": self.usable,
            "samples": self.series.counts["measured"],
            "coverage": round(self.series.coverage, 4),
            "gaps": len(self.series.gaps),
            "impossible": len(self.series.impossible),
            "mean_bpm": None if self.series.mean_bpm is None else round(self.series.mean_bpm, 1),
            "resting_bpm": None if self.resting_bpm is None else round(self.resting_bpm, 1),
            "resting_samples": self.resting_samples,
            "resting_samples_after_exclusion": self.resting_samples_after_exclusion,
            "sleep": self.sleep.as_dict(),
            "previous_sleep": self.previous_sleep.as_dict(),
            "rest": self.rest.as_dict(),
            "exercise": self.exercise.as_dict(),
            "hrv": self.hrv.as_dict(),
            "hrv_window": self.hrv_window,
        }


def day_window(day: date, options: FeatureOptions | None = None) -> tuple[datetime, datetime]:
    """The half-open UTC range of one local day."""
    return local_day_bounds(day, (options or FeatureOptions()).tz)


def anchor_window(day: date, options: FeatureOptions | None = None) -> tuple[datetime, datetime]:
    """The half-open UTC range of *the night that began on* ``day``.

    ``[day evening_hour:00, day+1 morning_hour:00)`` in local time - by default 18:00 to
    noon the next day. A night does not respect the day boundary it started in, and a
    sleep window clipped at midnight is a sleep window whose second half has been
    quietly thrown away, so the overnight search gets its own range.
    """
    opts = options or FeatureOptions()
    return (
        local_clock(day, opts.tz, opts.evening_hour),
        local_clock(day + timedelta(days=1), opts.tz, opts.morning_hour),
    )


def _read(store: ReadOnlyStore, start: datetime, end: datetime, opts: FeatureOptions) -> EpochSeries:
    return epoch_series(
        store,
        start,
        end,
        epoch_seconds=opts.epoch_seconds,
        tolerance=opts.tolerance,
        impute_seconds=opts.impute_seconds,
        min_plausible_bpm=opts.min_plausible_bpm,
        max_plausible_bpm=opts.max_plausible_bpm,
        dataset=opts.dataset,
        field=opts.field,
    )


def _clip(source: EpochSeries | None, store: ReadOnlyStore, bounds, opts: FeatureOptions) -> EpochSeries:
    """The part of ``bounds`` that ``source`` already covers, reading it if there is none.

    Clipping to the source's own range is what keeps a range running past the last stored
    day - "give me tonight, at 21:00" - from looking like a night with data in it.
    """
    start, end = bounds
    if source is None:
        return _read(store, start, end, opts)
    low, high = max(start, source.since), min(end, source.until)
    return source.slice(low, max(low, high))


def _hrv_input(
    mode: str,
    *,
    night: EpochSeries,
    window: EpochSeries,
    exercise: ExerciseMask,
    sleep: TimeWindow,
    rest: TimeWindow,
) -> tuple[str, tuple[Sample, ...]]:
    """Which readings HRV is computed from, and the name that says so.

    Each window is read from the series it was found in: a night that runs from 23:50 to
    01:05 has almost none of its readings inside the *day* slice, and taking the day slice
    as the source would report "no intervals" for a perfectly measurable night. Exercise
    readings are excluded whichever window is used - a sleep window a motion artefact
    pushed over the exercise threshold is not a resting window any more.

    ``sleep_first`` (the default) uses the night and falls back to the awake resting
    window: HRV belongs to a still body, and if there is no night to measure the honest
    answer is the resting window or nothing.
    """
    if mode == "day":
        return "day", exercise.hrv_samples(window)
    wanted: list[tuple[str, TimeWindow, EpochSeries]] = []
    if mode in ("sleep", "sleep_first"):
        wanted.append(("sleep", sleep, night))
    if mode in ("rest", "sleep_first"):
        wanted.append(("rest", rest, window))
    for name, candidate, source in wanted:
        if not candidate.detected:
            continue
        chosen = tuple(
            sample for sample in exercise.hrv_samples(source) if candidate.covers(sample.ts)
        )
        if chosen:
            return name, chosen
    return f"{mode}:none", ()


def day_features(
    store: ReadOnlyStore,
    day: date,
    *,
    options: FeatureOptions | None = None,
    series: EpochSeries | None = None,
    overnight: EpochSeries | None = None,
) -> DayFeatures:
    """Every feature for one local day.

    Reads from ``store`` unless a ``series`` covering the day is handed in, which is how
    :func:`daily_features` reads one range once and slices it. ``overnight`` is the same
    for the night that began on this day (see :func:`anchor_window`); when it is not
    supplied it is read from ``store`` over exactly that range, because the sleep window
    has to be allowed to run past local midnight.
    """
    opts = options or FeatureOptions()
    start, end = day_window(day, opts)
    if series is None:
        series = _read(store, start, end, opts)
    window = series.slice(max(start, series.since), min(end, series.until))
    night = _clip(overnight, store, anchor_window(day, opts), opts)
    sleep = detect_sleep_window(
        night,
        tz=opts.tz,
        evening_hour=opts.evening_hour,
        morning_hour=opts.morning_hour,
        block_minutes=opts.sleep_block_minutes,
        min_block_minutes=opts.sleep_min_block_minutes,
        extend_slack_bpm=opts.sleep_extend_slack_bpm,
        patience_minutes=opts.sleep_patience_minutes,
        smoothing_minutes=opts.sleep_smoothing_minutes,
    )
    # Last night's sleep runs into this morning. It is not resting time to be spent on a
    # resting baseline, and it must not be the window a daytime elevated-rate rule reads.
    previous_night = _clip(overnight, store, anchor_window(day - timedelta(days=1), opts), opts)
    previous_sleep = detect_sleep_window(
        previous_night,
        tz=opts.tz,
        evening_hour=opts.evening_hour,
        morning_hour=opts.morning_hour,
        block_minutes=opts.sleep_block_minutes,
        min_block_minutes=opts.sleep_min_block_minutes,
        extend_slack_bpm=opts.sleep_extend_slack_bpm,
        patience_minutes=opts.sleep_patience_minutes,
        smoothing_minutes=opts.sleep_smoothing_minutes,
    )
    rest = detect_resting_window(
        window,
        exclude=[sleep, previous_sleep],
        sustained_minutes=opts.rest_sustained_minutes,
        min_minutes=opts.rest_min_minutes,
        extend_slack_bpm=opts.rest_extend_slack_bpm,
        patience_minutes=opts.rest_patience_minutes,
        smoothing_minutes=opts.rest_smoothing_minutes,
    )
    exercise = build_exercise_mask(
        window,
        resting_bpm=rest.mean_bpm,
        resting_multiple=opts.exercise_resting_multiple,
        absolute_floor_bpm=opts.exercise_absolute_floor_bpm,
        sustained_minutes=opts.exercise_sustained_minutes,
        cooldown_minutes=opts.exercise_cooldown_minutes,
    )
    resting = tuple(sample for sample in window.measured_samples if rest.covers(sample.ts))
    resting_kept = tuple(sample for sample in resting if not exercise.covers(sample.ts))
    hrv_window_name, hrv_samples = _hrv_input(
        opts.hrv_window, night=night, window=window, exercise=exercise, sleep=sleep, rest=rest
    )
    hrv = hrv_features(
        hrv_samples,
        epoch_seconds=max(night.effective_epoch_seconds, window.effective_epoch_seconds),
        min_intervals=opts.min_intervals,
        label=hrv_window_name,
    )
    return DayFeatures(
        day=day,
        series=window,
        sleep=sleep,
        rest=rest,
        exercise=exercise,
        hrv=hrv,
        hrv_window=hrv_window_name,
        options=opts,
        previous_sleep=previous_sleep,
        resting_samples=len(resting),
        resting_samples_after_exclusion=len(resting_kept),
    )


def daily_features(
    store: ReadOnlyStore,
    since: datetime,
    until: datetime,
    *,
    options: FeatureOptions | None = None,
    days: Iterable[date] | None = None,
) -> list[DayFeatures]:
    """One :class:`DayFeatures` per local day touched by the half-open ``[since, until)``.

    The store is read **once** for the whole range and every day is a slice of that, so a
    90-day scan costs one query instead of ninety - which is what keeps a 90-day anomaly
    scan inside a benchmark budget.
    """
    opts = options or FeatureOptions()
    whole = _read(store, since, until, opts)
    wanted: Sequence[date] = list(days) if days is not None else day_sequence(since, until, opts.tz)
    out: list[DayFeatures] = []
    for day in wanted:
        start, end = day_window(day, opts)
        if end <= since or start >= until:
            continue
        out.append(day_features(store, day, options=opts, series=whole, overnight=whole))
    return out


__all__ = [
    "HRV_WINDOWS",
    "DayFeatures",
    "FeatureOptions",
    "anchor_window",
    "daily_features",
    "day_features",
    "day_window",
]
