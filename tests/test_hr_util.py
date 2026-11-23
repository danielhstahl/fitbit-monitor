"""The shared assumptions: units, the conversion, the band, the clock, the estimator.

Every other module in :mod:`fitbit_monitor.hr` states its thresholds in terms of something
here, so this file is where the *definitions* get pinned rather than re-asserted per module:
that a rate becomes an interval in exactly one way, that SDNN is the population estimate,
that a local day is a wall-clock day (23 or 25 hours long around DST), and that a naive
instant is refused instead of guessed at.
"""

from __future__ import annotations

import math
import unittest
from datetime import UTC, date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fitbit_monitor.hr.util import (
    BPM,
    DEFAULT_MAX_BPM,
    DEFAULT_MIN_BPM,
    EXERCISE_HEURISTIC,
    HRV_HEURISTIC,
    MS,
    REST_HEURISTIC,
    SLEEP_HEURISTIC,
    UNIT_BPM,
    UNIT_MS,
    bpm_to_ms,
    day_sequence,
    inside,
    is_number,
    local_clock,
    local_date,
    local_day,
    local_day_bounds,
    mean,
    median,
    minutes,
    ms_to_bpm,
    pnn50_threshold_ms,
    population_stdev,
    resolve_tz_name,
)

NY = ZoneInfo("America/New_York")


class UnitTest(unittest.TestCase):
    def test_the_unit_strings_are_the_catalog_s(self) -> None:
        self.assertEqual(UNIT_BPM, "beats per minute")
        self.assertEqual(UNIT_MS, "milliseconds")
        self.assertEqual((BPM, MS), ("bpm", "ms"))

    def test_the_plausibility_band_is_stated_and_open_ended(self) -> None:
        # Both bounds sit outside anything a wearer should be reasoning about, so that a
        # rule - not this filter - decides whether 130 bpm at rest is interesting.
        self.assertEqual(DEFAULT_MIN_BPM, 25.0)
        self.assertEqual(DEFAULT_MAX_BPM, 220.0)
        self.assertLess(DEFAULT_MIN_BPM, 40.0, "trained athletes sit in the high 30s")
        self.assertGreater(DEFAULT_MAX_BPM, 200.0, "220 - age is under 200 for every adult")

    def test_every_heuristic_name_is_distinct(self) -> None:
        names = {SLEEP_HEURISTIC, REST_HEURISTIC, EXERCISE_HEURISTIC, HRV_HEURISTIC}
        self.assertEqual(len(names), 4, "a finding has to say which rule produced it")


class ConversionTest(unittest.TestCase):
    def test_one_minute_at_sixty_is_one_thousand_milliseconds(self) -> None:
        self.assertEqual(bpm_to_ms(60.0), 1000.0)
        self.assertEqual(ms_to_bpm(1000.0), 60.0)
        self.assertAlmostEqual(bpm_to_ms(52.0), 1153.8461538461538, places=9)

    def test_the_round_trip_is_exact_enough_to_report(self) -> None:
        for bpm in (37.5, 52.0, 61.3, 148.0):
            self.assertAlmostEqual(ms_to_bpm(bpm_to_ms(bpm)), bpm, places=9)

    def test_a_rate_that_is_not_a_rate_has_no_interval(self) -> None:
        # Callers are reporting a missing reading, not doing arithmetic: inf beats ZeroDivisionError.
        self.assertEqual(bpm_to_ms(0.0), math.inf)
        self.assertEqual(bpm_to_ms(-1.0), math.inf)
        self.assertEqual(ms_to_bpm(0.0), math.inf)

    def test_the_pnn50_bar_follows_the_window_and_reproduces_the_convention(self) -> None:
        self.assertEqual(pnn50_threshold_ms(1000.0), 50.0, "5% of a second is the classic 50 ms")
        self.assertEqual(pnn50_threshold_ms(500.0), 25.0)
        self.assertEqual(pnn50_threshold_ms(1000.0, fraction=0.1), 100.0)

    def test_the_pnn50_bar_degrades_to_the_convention_on_nonsense(self) -> None:
        self.assertEqual(pnn50_threshold_ms(0.0), 50.0)
        self.assertEqual(pnn50_threshold_ms(-5.0), 50.0)
        self.assertEqual(pnn50_threshold_ms(math.inf), 50.0)

    def test_is_number_refuses_the_things_the_device_sends(self) -> None:
        self.assertTrue(is_number(61.0))
        self.assertTrue(is_number(0))
        self.assertFalse(is_number(float("nan")))
        self.assertFalse(is_number(math.inf))
        self.assertFalse(is_number(None))
        self.assertFalse(is_number("61"))


class MathsTest(unittest.TestCase):
    def test_stdev_is_the_population_estimator_not_the_sample_one(self) -> None:
        # SDNN is defined over the intervals in the window, so n-1 would report a
        # variance of a population wider than the one that was measured.
        self.assertEqual(population_stdev([1.0, 1.0]), 0.0)
        self.assertAlmostEqual(population_stdev([1.0, 2.0, 3.0, 4.0]), math.sqrt(1.25), places=12)
        self.assertNotAlmostEqual(population_stdev([1.0, 2.0, 3.0, 4.0]), math.sqrt(5 / 3.0), places=6)

    def test_stdev_of_nothing_is_zero_rather_than_an_error(self) -> None:
        self.assertEqual(population_stdev([]), 0.0)
        self.assertEqual(population_stdev([7.0]), 0.0)

    def test_mean_and_median(self) -> None:
        self.assertEqual(mean([1.0, 2.0, 3.0]), 2.0)
        self.assertEqual(median([3.0, 1.0, 2.0]), 2.0)
        self.assertEqual(median([4.0, 1.0, 2.0, 3.0]), 2.5)

    def test_durations_report_in_tenths_of_a_minute(self) -> None:
        self.assertEqual(minutes(90), 1.5)
        self.assertEqual(minutes(0), 0.0)
        self.assertEqual(minutes(3600), 60.0)


class ClockTest(unittest.TestCase):
    def test_a_naive_instant_is_refused_not_guessed(self) -> None:
        # Silently reading a naive datetime as local time is how a nightly baseline ends up
        # a day out for half the userbase.
        with self.assertRaises(ValueError):
            local_date(datetime(2026, 5, 1, 3, 0), NY)

    def test_utc_is_the_default_clock(self) -> None:
        instant = datetime(2026, 5, 1, 23, 30, tzinfo=UTC)
        self.assertEqual(local_date(instant, None), instant.date())
        self.assertEqual(local_day_bounds(date(2026, 5, 1), None)[0], datetime(2026, 5, 1, tzinfo=UTC))

    def test_the_same_instant_is_a_different_local_date(self) -> None:
        instant = datetime(2026, 5, 1, 2, 0, tzinfo=UTC)
        self.assertEqual(local_date(instant, UTC), date(2026, 5, 1))
        self.assertEqual(local_date(instant, NY), date(2026, 4, 30))

    def test_local_clock_builds_a_wall_clock_instant_in_utc(self) -> None:
        self.assertEqual(
            local_clock(date(2026, 5, 10), NY, 18), datetime(2026, 5, 10, 22, tzinfo=UTC)
        )
        self.assertEqual(local_day(date(2026, 5, 10), NY).astimezone(NY).hour, 0)

    def test_a_local_day_is_24_hours_except_around_dst(self) -> None:
        for day, hours in ((date(2026, 5, 10), 24), (date(2026, 3, 8), 23), (date(2026, 11, 1), 25)):
            start, end = local_day_bounds(day, NY)
            self.assertEqual(end - start, timedelta(hours=hours), day)
            self.assertEqual(end.astimezone(NY).hour, 0)

    def test_the_day_sequence_is_exclusive_at_the_end(self) -> None:
        start = datetime(2026, 5, 1, tzinfo=UTC)
        self.assertEqual(day_sequence(start, start + timedelta(days=3), None),
                         [date(2026, 5, 1), date(2026, 5, 2), date(2026, 5, 3)])
        self.assertEqual(day_sequence(start, start, None), [])
        self.assertEqual(day_sequence(start, start + timedelta(hours=4), None), [date(2026, 5, 1)])

    def test_the_day_sequence_counts_a_dst_day_once(self) -> None:
        start, _ = local_day_bounds(date(2026, 3, 7), NY)
        _, end = local_day_bounds(date(2026, 3, 9), NY)
        self.assertEqual(day_sequence(start, end, NY), [date(2026, 3, 7), date(2026, 3, 8), date(2026, 3, 9)])

    def test_the_timezone_name_is_printable_and_none_stays_none(self) -> None:
        self.assertEqual(resolve_tz_name(NY), "America/New_York")
        self.assertEqual(resolve_tz_name(UTC), "UTC")
        self.assertEqual(resolve_tz_name(timezone(timedelta(hours=5, minutes=30))), "UTC+05:30")
        self.assertIsNone(resolve_tz_name(None))


class InsideTest(unittest.TestCase):
    class Window:
        def __init__(self, start: datetime, end: datetime) -> None:
            self.start, self.end = start, end

        def covers(self, instant: datetime) -> bool:
            return self.start <= instant < self.end

    def test_inside_any_window_that_has_one(self) -> None:
        start = datetime(2026, 5, 1, tzinfo=UTC)
        windows = [self.Window(start, start + timedelta(hours=1))]
        self.assertTrue(inside(start + timedelta(minutes=30), windows))
        self.assertFalse(inside(start + timedelta(hours=1), windows), "the end is exclusive")
        self.assertFalse(inside(start, []))
        self.assertFalse(inside(start, [self.Window(start + timedelta(days=1), start + timedelta(days=2))]))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
