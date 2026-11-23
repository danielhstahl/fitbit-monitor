"""Shared fixtures for the anomaly engine's tests: hand-built multi-day series in a real store.

Same discipline as ``tests/hr_support.py``: every number an engine test asserts has to be
workable out on paper from the readings that produced it, so the profiles here are plateaus
with a named value per stretch, and the episodes are inserted as plateaus too. The store is
the real read API in a temp file, so "the engine reads the store through the feature library"
is exercised rather than asserted, and nothing here is synthetic-corpus data - the seeded
corpus has its own suite (``test_seed_corpus.py``, and this epic's labelled gate after it).

The shape every scenario uses is one local day of:

``00:00-06:00``  asleep, ``night`` bpm
``06:00-23:00``  awake, ``awake`` bpm
``23:00-24:00``  asleep, ``night`` bpm

which is enough for the feature library to find both a sleep window (the 00:00-06:00 block,
owned by the night that began the previous evening) and a resting window (the quietest
sustained stretch of the awake plateau), and it keeps the two contexts the engine detects in
separated by hours rather than by a heuristic nobody can see in the test.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Callable, Iterable, Sequence

#: Same anchor as ``hr_support``: a fixed, DST-free start of a local day.
START = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
DAY = timedelta(days=1)

#: What a quiet stretch, a clear episode and a clear non-episode look like in these fixtures.
QUIET_AWAKE = 75.0
QUIET_NIGHT = 55.0
HIGH_EPISODE = 105.0
LOW_EPISODE = 40.0
#: Alternates hard, which is what the scatter rule is built to notice: the mean of the two
#: values is close to the night's own level, so the *level* rules stay quiet while the
#: interval-to-interval scatter is enormous.
IRREGULAR_HIGH = 78.0
IRREGULAR_LOW = 46.0

Override = tuple[float, float, float | Callable[[int], float]]


def day_points(
    day: datetime,
    *,
    night: float = QUIET_NIGHT,
    awake: float = QUIET_AWAKE,
    step_minutes: int = 1,
    jitter: float = 0.0,
    overrides: Sequence[Override] = (),
) -> list[tuple[datetime, float]]:
    """One local day of readings, as ``(instant, bpm)`` pairs.

    ``overrides`` are ``(from_hour, to_hour, value)`` segments applied *after* the base
    plateaus, so an episode replaces the readings it covers rather than sitting next to
    them. A callable value is given the index of the reading inside its own segment, which
    is how a test writes an alternating rhythm without spelling out 40 literals.

    ``jitter`` is a deterministic sine wobble in bpm: real data never repeats a value exactly,
    and a fixture that is perfectly flat gives every robust spread in the engine a measured
    value of zero, which hides the floor constants that exist to cope with that.
    """
    base: list[Override] = [(0, 6, night), (6, 23, awake), (23, 24, night)]
    points: list[tuple[datetime, float]] = []
    for segments in (base, list(overrides)):
        for from_hour, to_hour, value in segments:
            stamp = day + timedelta(hours=from_hour)
            stop = day + timedelta(hours=to_hour)
            index = 0
            while stamp < stop:
                reading = value(index) if callable(value) else float(value)
                if jitter:
                    phase = (stamp.hour * 60 + stamp.minute) * 1.7
                    reading = round(reading + jitter * math.sin(phase), 2)
                points.append((stamp, reading))
                stamp += timedelta(minutes=step_minutes)
                index += 1
    # Stable sort: the base segments come first, so an override on the same instant wins.
    return sorted(points, key=lambda pair: pair[0])


def scenario_points(
    days: int,
    *,
    start: datetime = START,
    step_minutes: int = 1,
    jitter: float = 0.0,
    night: float = QUIET_NIGHT,
    awake: float = QUIET_AWAKE,
    episodes: Sequence[tuple[int, Override]] = (),
    **kwargs,
) -> list[tuple[datetime, float]]:
    """``days`` consecutive days, with episodes inserted on the day indices named.

    ``episodes`` is ``((day_index, (from_hour, to_hour, value)), ...)`` - one less level of
    nesting than reading as prose, and it keeps "what the fixture claims to contain" and
    "what the engine is asserted to have found" one screen apart.
    """
    by_day: dict[int, list[Override]] = {}
    for index, override in episodes:
        by_day.setdefault(index, []).append(override)
    points: list[tuple[datetime, float]] = []
    for index in range(days):
        points += day_points(
            start + index * DAY,
            night=night,
            awake=awake,
            step_minutes=step_minutes,
            jitter=jitter,
            overrides=by_day.get(index, ()),
            **kwargs,
        )
    return points


def irregular_pattern(
    high: float = IRREGULAR_HIGH, low: float = IRREGULAR_LOW
) -> Callable[[int], float]:
    """An alternating two-valued rhythm: huge interval-to-interval scatter, flat mean."""

    def value(index: int) -> float:
        return high if index % 2 else low

    return value


def span_of(points: Iterable[tuple[datetime, float]]) -> tuple[datetime, datetime]:
    """The half-open range a list of readings covers, plus one epoch on the end."""
    stamps = [stamp for stamp, _value in points]
    return min(stamps), max(stamps) + timedelta(minutes=1)
