"""The assumptions every heart-rate feature shares: names, units, bounds, small maths.

This module exists so the numbers a downstream threshold depends on are stated in
exactly one place, with their reasoning, instead of being re-derived (differently) in
every rule. Three kinds of thing live here:

**Units.** Heart rate is ``beats per minute`` (``bpm`` when an abbreviation is needed),
every interval - SDNN, RMSSD, pNN50 thresholds, RR estimates - is ``milliseconds``,
a duration of coverage is reported in ``minutes``, and every instant stored or returned
is timezone-aware **UTC** (see :mod:`fitbit_monitor.store.reader`). Nothing in this
package converts a rate into an interval or back without saying so in a name:
:func:`bpm_to_ms` is the one conversion and it is explicit.

**Plausibility band.** :data:`DEFAULT_MIN_BPM` and :data:`DEFAULT_MAX_BPM` are the
inclusive bounds outside which a stored reading is treated as an artefact rather than
as a heart rate. Both are deliberately *not* clinical thresholds: they sit outside the
range of any reading a wearer should be reasoning about, so that a rule - not this
filter - is what decides whether 130 bpm at rest is interesting.

**Named heuristics.** Every window and mask in this package carries the constant below
as the name of the rule that produced it. A finding can then say "the sustained-high
rule fired on the ``quiet-hour-sustained-minimum`` window", which is the difference
between a number someone can audit and one they cannot.

The maths helpers are here rather than imported from a statistics library because the
choice of estimator is a stated assumption too: SDNN here is the population standard
deviation of the interval window it is named for, not a sample estimate of a wider
population, and :func:`pnn50_threshold_ms` says in its name that a 50 ms difference
between successive intervals is the 500 ms/60 bpm convention.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import TYPE_CHECKING, Iterable, Sequence

if TYPE_CHECKING:  # pragma: no cover - typing only, and there is no import cycle
    from .windows import TimeWindow as TimeWindowLike

# --- units -----------------------------------------------------------------
#: The unit of every rate in this package (same string the store catalog uses).
UNIT_BPM = "beats per minute"
#: The unit of every interval in this package (same string the store catalog uses).
UNIT_MS = "milliseconds"
#: Short forms, for a column header or a MetricResult unit field.
BPM = "bpm"
MS = "ms"

# --- plausibility band -----------------------------------------------------
#: Lowest reading treated as a heart rate, inclusive.
#
# Fitbit emits ``0`` for "no signal", and a real resting rate below this is rare
# enough that it belongs in a report as a *suspicious value* rather than silently
# averaging into a baseline. The floor is low (25) on purpose: trained athletes sit in
# the high 30s, and bradycardia - the thing the detector exists to notice - is defined
# around 60, nowhere near this bound.
DEFAULT_MIN_BPM = 25.0
#: Highest reading treated as a heart rate, inclusive.
#
# Above this a number is a sensor artefact: wrist optical heart rate saturates and
# clips when it loses the pulse, and the classic age-predicted maximum (220 - age) is
# under 200 for every adult. A reading of 255 in particular is what a Fitbit payload
# carries when the optical sensor overflows, so it must never reach a maximum-rate rule.
DEFAULT_MAX_BPM = 220.0

# --- flags a Sample carries ------------------------------------------------
#: No reading at this epoch; a value was interpolated from its neighbours, or left out.
FLAG_ABSENT = "absent"
#: The value was interpolated across a short gap: usable, but not measured.
FLAG_IMPUTED = "imputed"
#: The stored value is not a finite number.
FLAG_NOT_A_NUMBER = "not_a_number"
#: The stored value is outside the plausibility band (including the device's 0/255).
FLAG_OUT_OF_RANGE = "out_of_range"
#: The two samples on either side of a value are further apart than the grain allows,
#: so anything computed from that step spans a gap.
FLAG_SPANS_GAP = "spans_gap"

# --- named heuristics ------------------------------------------------------
#: Sleep onset/offset: the lowest mean heart rate over an overnight block, grown out
#: while the rate stays near that block's mean. See
#: :func:`fitbit_monitor.hr.windows.detect_sleep_window`.
SLEEP_HEURISTIC = "lowest-load-overnight-block"
#: Daytime rest: the sustained minimum outside the sleep window, grown out the same way.
#: See :func:`fitbit_monitor.hr.windows.detect_resting_window`.
REST_HEURISTIC = "quiet-hour-sustained-minimum"
#: Exercise exclusion: rates sustained well above the personal resting level.
#: See :func:`fitbit_monitor.hr.exercise.build_exercise_mask`.
EXERCISE_HEURISTIC = "relative-sustained-elevation"
#: Windowed HRV: RR estimates from successive same-epoch samples. See
#: :func:`fitbit_monitor.hr.hrv.hrv_features`.
HRV_HEURISTIC = "successive-epoch-interval-estimate"

# --- conversions -----------------------------------------------------------
MINUTES_PER_HOUR = 60.0
SECONDS_PER_MINUTE = 60.0
#: One minute of heart rate at 60 bpm is 60 beats, so 1 bpm = 1 beat/second = 1000 ms
#: of interval. This is the one line where a rate becomes an interval; everything that
#: needs the relationship calls this instead of dividing inline.
MS_PER_MINUTE = 60_000.0


def bpm_to_ms(bpm: float) -> float:
    """Heart rate in ``bpm`` as the mean beat interval in ``milliseconds``.

    ``60 bpm -> 1000 ms``. A rate of zero (or below) has no interval and returns
    ``math.inf`` rather than raising, because the callers are reporting a missing
    reading, not doing arithmetic.
    """
    if bpm <= 0:
        return math.inf
    return MS_PER_MINUTE / bpm


def ms_to_bpm(ms: float) -> float:
    """The inverse of :func:`bpm_to_ms`: an interval in ``ms`` as a rate in ``bpm``."""
    if ms <= 0:
        return math.inf
    return MS_PER_MINUTE / ms


def pnn50_threshold_ms(interval_ms: float, fraction: float = 0.05) -> float:
    """The absolute threshold used by pNN50, derived from the window's own rate.

    pNN50 counts successive intervals differing by more than 50 ms, which is what the
    literature means at a resting rate around 60 bpm - a mean interval of 1000 ms, so the
    classic bar is 5% of it. A window at 120 bpm has half that interval, and the same
    autonomic variability shows up as a *smaller* absolute difference, so a fixed 50 ms
    would under-count pNN50 there; taking ``fraction`` of the window's own mean interval
    keeps the bar proportionally equivalent (and reproduces exactly 50 ms at 60 bpm).
    """
    if interval_ms <= 0 or math.isinf(interval_ms):
        return 50.0
    return max(1.0, interval_ms * fraction)


# --- small maths -----------------------------------------------------------
def is_number(value: object) -> bool:
    """True for a real, finite number (``nan``, ``inf`` and non-numbers are not)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def mean(values: Sequence[float]) -> float:
    """Arithmetic mean. The caller must have checked the sequence is not empty."""
    return float(sum(values)) / len(values)


def median(values: Sequence[float]) -> float:
    """Median of a non-empty sequence (sorted copy, so input order is untouched)."""
    ordered = sorted(float(v) for v in values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return (float(ordered[middle - 1]) + float(ordered[middle])) / 2.0


def population_stdev(values: Sequence[float]) -> float:
    """Standard deviation with ``n`` in the denominator.

    Stated because it matters: SDNN is defined over the intervals *in the window*, so
    it is a description of that window, not an estimate of a wider population. Using
    ``n - 1`` here would inflate SDNN by ~6% on the 8-interval minimum and make every
    threshold downstream silently mean something else.
    """
    n = len(values)
    if n == 0:
        return 0.0
    centre = mean(values)
    return math.sqrt(sum((float(v) - centre) ** 2 for v in values) / n)


def minutes(seconds: float) -> float:
    """Seconds as minutes, rounded to 0.1 - the reporting shape for every duration."""
    return round(seconds / SECONDS_PER_MINUTE, 1)


def iso(value: datetime | None) -> str | None:
    """An instant in ISO-8601, or ``None`` for "there was no such instant"."""
    return value.isoformat() if value is not None else None


def resolve_tz_name(zone: tzinfo | None) -> str | None:
    """The printable name of a timezone (``"Europe/Berlin"``, ``"UTC"``); ``None`` stays ``None``.

    Reported with every window so a reader can tell a 03:00 UTC instant from a 03:00
    wall-clock one - the distinction a nightly baseline lives or dies by.
    """
    if zone is None:
        return None
    key = getattr(zone, "key", None)
    return str(key) if key else str(zone)


# --- time windows ----------------------------------------------------------
def local_day(day: date, tz: tzinfo | None) -> datetime:
    """The instant ``day`` 00:00 begins in ``tz`` (UTC when ``tz`` is None), as UTC."""
    naive = datetime(day.year, day.month, day.day)
    aware = naive if tz is None else naive.replace(tzinfo=tz)
    return aware.astimezone(UTC)


def local_day_bounds(day: date, tz: tzinfo | None) -> tuple[datetime, datetime]:
    """Half-open ``[midnight, next midnight)`` for one local day, as UTC instants.

    Around a DST transition these two instants are 23 or 25 hours apart, which is the
    whole point: the day is the wall-clock day the wearer means, not 24 hours of UTC.
    """
    start = local_day(day, tz)
    return start, local_day(day + timedelta(days=1), tz)


def local_date(instant: datetime, tz: tzinfo | None) -> date:
    """The local calendar date ``instant`` falls on in ``tz`` (UTC when None)."""
    if instant.tzinfo is None:
        raise ValueError(f"instant must be timezone-aware: {instant!r}")
    return instant.astimezone(tz or UTC).date()


def local_clock(day: date, tz: tzinfo | None, hour: int, minute: int = 0) -> datetime:
    """The UTC instant of ``hour:minute`` wall-clock on local ``day``.

    Used to anchor a window to the wearer's clock ("the night beginning at 18:00")
    instead of to a UTC hour. On a day whose wall-clock hour does not exist - the spring
    forward in most zones - Python's fold rules pick the later instant, which is the one
    that leaves the shortest, most conservative window.
    """
    naive = datetime(day.year, day.month, day.day, hour, minute)
    aware = naive if tz is None else naive.replace(tzinfo=tz)
    return aware.astimezone(UTC)


def day_sequence(since: datetime, until: datetime, tz: tzinfo | None) -> list[date]:
    """Every local calendar date touched by the half-open range ``[since, until)``.

    ``until`` is exclusive, so a range ending exactly at midnight does not include that
    day - and a range shorter than a day (10:00 to 14:00) still touches the day it is in.
    """
    last = local_date(until - timedelta(microseconds=1), tz)
    days: list[date] = []
    day = local_date(since, tz)
    while day <= last:
        days.append(day)
        day = day + timedelta(days=1)
    return days


def inside(instant: datetime, windows: Iterable[TimeWindowLike]) -> bool:
    """True when ``instant`` falls inside any window that has a ``covers`` method."""
    return any(bool(window.covers(instant)) for window in windows)


__all__ = [
    "BPM",
    "DEFAULT_MAX_BPM",
    "DEFAULT_MIN_BPM",
    "FLAG_ABSENT",
    "FLAG_IMPUTED",
    "FLAG_NOT_A_NUMBER",
    "FLAG_OUT_OF_RANGE",
    "HRV_HEURISTIC",
    "MS",
    "REST_HEURISTIC",
    "SLEEP_HEURISTIC",
    "UNIT_BPM",
    "UNIT_MS",
    "bpm_to_ms",
    "day_sequence",
    "inside",
    "is_number",
    "iso",
    "local_date",
    "local_clock",
    "local_day",
    "local_day_bounds",
    "mean",
    "median",
    "minutes",
    "resolve_tz_name",
    "ms_to_bpm",
    "pnn50_threshold_ms",
    "population_stdev",
]
