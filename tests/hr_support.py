"""Shared fixtures for the heart-rate feature tests: hand-built series in a real store.

Every numeric assertion in ``test_hr_*.py`` is made against a series written out here
minute by minute, because a feature library is only checked if the expected number can be
worked out on paper from the readings that produced it. The store is the real read API in
a temp file, so "built on the read API" is exercised rather than asserted in a comment -
and nothing here is synthetic-corpus data, which is what ``fitbit_monitor.plugins.testing``
and ``test_seed_corpus.py`` are for.

Importable as ``hr_support`` from pytest and from ``unittest discover`` (no package-relative
imports, no pytest-only fixtures), the same way ``tests/support.py`` is.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterable, Sequence

from fitbit_monitor.hr.epochs import EpochSeries, build_samples
from fitbit_monitor.store.reader import ReadOnlyStore
from fitbit_monitor.store.writer import SeriesStore

#: A fixed, DST-free anchor: every test that does not care about zones starts here.
START = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)


def stamps(count: int, *, start: datetime = START, step_seconds: float = 60.0) -> list[datetime]:
    """``count`` evenly spaced instants starting at ``start``."""
    return [start + timedelta(seconds=step_seconds * i) for i in range(count)]


def hourly(day: datetime, *, from_hour: float, to_hour: float, value: float, step_minutes: int = 1):
    """Readings from one hour of the day to another (half-open), as ``(instant, value)``.

    ``from_hour``/``to_hour`` are decimal hours after midnight *of ``day``'s date*, so a
    test can say "the night was 55 and the day was 80" without writing 1440 literals.
    """
    base = datetime(day.year, day.month, day.day, tzinfo=UTC)
    first = base + timedelta(hours=from_hour)
    stop = base + timedelta(hours=to_hour)
    out = []
    stamp = first
    while stamp < stop:
        out.append((stamp, value))
        stamp += timedelta(minutes=step_minutes)
    return out


def plateau(*segments: tuple[float, float, float], step_minutes: int = 1, day: datetime = START):
    """Build a day of readings from ``(from_hour, to_hour, value)`` plateaus.

    Plateaus are the shape a window heuristic is *checkable* against: constant so the
    expected window is exact, with the transitions at hours the test can name.
    """
    points: list[tuple[datetime, float]] = []
    for start_hour, stop_hour, value in segments:
        points.extend(hourly(day, from_hour=start_hour, to_hour=stop_hour, value=value, step_minutes=step_minutes))
    return sorted(points, key=lambda pair: pair[0])


def hand_series(
    pairs: Sequence[tuple[datetime, float | None]],
    *,
    epoch_seconds: float = 60.0,
    **kwargs,
) -> EpochSeries:
    """An :class:`EpochSeries` from hand-written readings, with no store in the way.

    Goes through the same classification, imputation and gap logic
    :func:`fitbit_monitor.hr.epochs.epoch_series` uses, so a test written against this is
    testing the library and not a mock.
    """
    if not pairs:
        raise ValueError("an empty series needs a window; build one on a store instead")
    since = min(ts for ts, _ in pairs)
    until = max(ts for ts, _ in pairs) + timedelta(seconds=epoch_seconds)
    samples = build_samples(pairs, epoch_seconds=epoch_seconds, **kwargs)
    from fitbit_monitor.hr.epochs import EpochSeries as _Series, find_gaps

    return _Series(
        since=since,
        until=until,
        samples=samples,
        gaps=find_gaps(samples, epoch_seconds=epoch_seconds, since=since, until=until),
        epoch_seconds=epoch_seconds,
        impute_seconds=float(kwargs.get("impute_seconds") or 0.0),
    )


class HrStoreTestCase(unittest.TestCase):
    """A temp store plus the helpers every feature test needs."""

    dataset = "heart_rate"
    field = "hr"

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "store.sqlite3"
        self.writer = SeriesStore(self.path)
        self.addCleanup(self.writer.close)

    # --- writing -----------------------------------------------------------
    def put(self, pairs: Iterable[tuple[datetime, object]], *, dataset: str | None = None) -> int:
        """Store ``(instant, value)`` readings; a ``None`` value stores a null field."""
        name = dataset or self.dataset
        count = 0
        for stamp, value in pairs:
            payload: dict[str, float] | dict[str, None] = {self.field: None} if value is None else {self.field: float(value)}
            self.writer.put(name, stamp, dict(payload))
            count += 1
        self.writer.commit()
        return count

    def put_flat(self, *values: float, start: datetime = START, step_seconds: float = 60.0) -> list[datetime]:
        """Store one reading every ``step_seconds``; returns the instants written."""
        instants = stamps(len(values), start=start, step_seconds=step_seconds)
        self.put(zip(instants, values))
        return instants

    # --- reading -----------------------------------------------------------
    def reader(self) -> ReadOnlyStore:
        store = ReadOnlyStore(self.path)
        self.addCleanup(store.close)
        return store

    def series(self, since: datetime, until: datetime, **kwargs) -> EpochSeries:
        from fitbit_monitor.hr import epoch_series

        return epoch_series(self.reader(), since, until, **kwargs)

    def day_series(self, pairs: Sequence[tuple[datetime, float | None]], **kwargs) -> EpochSeries:
        """Store ``pairs`` and read the whole range back through the read API."""
        if not pairs:
            raise ValueError("put nothing and read an explicit window instead")
        self.put(pairs)
        since = min(ts for ts, _ in pairs)
        until = max(ts for ts, _ in pairs) + timedelta(seconds=kwargs.pop("epoch_seconds", 60))
        return self.series(since, until, **kwargs)

    # --- assertions worth having in more than one file ---------------------
    assertEqualTimes = unittest.TestCase.assertEqual

    def assertWindow(self, window, start: datetime, stop: datetime) -> None:
        self.assertTrue(window.detected, f"window not detected: {window.reason}")
        self.assertEqual(window.start, start)
        self.assertEqual(window.end, stop)

    def assertNotWindow(self, window, *, mentions: str = "") -> None:
        self.assertFalse(window.detected, f"unexpectedly detected: {window.start}..{window.end}")
        if mentions:
            self.assertIn(mentions, window.reason or "", "the refusal should say why")
