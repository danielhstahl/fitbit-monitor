"""Sleep and resting windows: the heuristic named, the edges exact, the refusals stated.

The plateaus here are deliberately flat: a window heuristic can only be checked against
readings whose correct answer can be stated exactly. Where the point is that the rule
survives *noise*, one case adds deterministic jitter and asserts a tolerance instead.

``smoothing_minutes=0`` appears often for the same reason - it removes a step the test is
not about, so an edge is expected at the exact minute the rate changes.
"""

from __future__ import annotations

import unittest
from datetime import timedelta
from zoneinfo import ZoneInfo

from fitbit_monitor.hr import detect_resting_window, detect_sleep_window
from fitbit_monitor.hr.util import REST_HEURISTIC, SLEEP_HEURISTIC
from fitbit_monitor.hr.windows import TimeWindow, growth_slack, not_detected, smoothed, smoothing_count
from hr_support import HrStoreTestCase, START, hand_series, plateau

DAY_TWO = START + timedelta(days=1)


def two_nights(*, step_minutes: int = 1):
    """Two identical local days: asleep 0-6 and 23-24, awake at 80 bpm in between."""
    points = []
    for day in (START, DAY_TWO):
        points += plateau(
            (0, 6, 55.0), (6, 23, 80.0), (23, 24, 55.0),
            step_minutes=step_minutes, day=day,
        )
    return points


def one_day(*, step_minutes: int = 1, jitter: float = 0.0):
    """A day whose awake quiet hour (09:00-10:00) is unambiguously the quietest of them."""
    import math

    def wob(base: float, index: int) -> float:
        return base + jitter * math.sin(index * 1.7) if jitter else base

    points = []
    segments = [(0, 6, 52.0), (6, 9, 78.0), (9, 10, 65.0), (10, 22, 82.0), (22, 24, 70.0)]
    for from_hour, to_hour, value in segments:
        for index, (stamp, reading) in enumerate(
            plateau((from_hour, to_hour, value), step_minutes=step_minutes, day=START)
        ):
            points.append((stamp, round(wob(reading, index), 1)))
    return points


class SleepWindowTest(HrStoreTestCase):
    """``lowest-load-overnight-block``."""

    def test_the_lowest_overnight_block_is_found_and_grown_to_the_rate_rise(self) -> None:
        series = hand_series(two_nights())
        anchor_start, anchor_end = START + timedelta(hours=18), DAY_TWO + timedelta(hours=12)
        window = detect_sleep_window(
            series.slice(anchor_start, anchor_end), smoothing_minutes=0.0
        )
        self.assertEqual(window.heuristic, SLEEP_HEURISTIC)
        self.assertEqual(window.kind, "sleep")
        # The seed block is the first hour of the plateau; growth walks it to the rate rise
        # at each end, which is what decides the edges. The extension guard is a runaway
        # guard only, and a default that bites here would truncate every long night.
        self.assertWindow(window, START + timedelta(hours=23), DAY_TWO + timedelta(hours=6))
        self.assertEqual(window.minutes, 420.0)
        self.assertEqual(window.samples, 420)
        self.assertEqual(window.mean_bpm, 55.0)
        self.assertEqual(window.min_bpm, 55.0)
        self.assertEqual(window.detail["block_mean_bpm"], 55.0)

    def test_the_growth_guard_can_be_tightened_and_truncates_the_night(self) -> None:
        series = hand_series(two_nights())
        window = detect_sleep_window(
            series.slice(START + timedelta(hours=18), DAY_TWO + timedelta(hours=12)),
            smoothing_minutes=0.0,
            max_extension_minutes=120.0,
        )
        self.assertWindow(window, START + timedelta(hours=23), DAY_TWO + timedelta(hours=2))
        self.assertEqual(window.minutes, 180.0)

    def test_the_anchor_is_the_wearer_s_clock_not_utc(self) -> None:
        # Same shape, one hour of zone: the anchored window moves with the wall clock, so
        # the detected window - which is anchored on the plateau - moves with it.
        zone = ZoneInfo("America/New_York")
        points = []
        for offset in range(24 * 60):
            stamp = START + timedelta(minutes=offset)
            local_hour = (stamp.astimezone(zone).hour + stamp.astimezone(zone).minute / 60) % 24
            points.append((stamp, 55.0 if local_hour < 6 or local_hour >= 23 else 80.0))
        series = hand_series(points)
        anchored = series.slice(START, DAY_TWO)
        window = detect_sleep_window(anchored, tz=zone, smoothing_minutes=0.0)
        self.assertTrue(window.detected)
        local_start = window.start.astimezone(zone)
        self.assertEqual((local_start.hour, local_start.minute), (23, 0))

    def test_a_night_with_too_few_readings_is_refused_with_a_count(self) -> None:
        sparse = [(DAY_TWO + timedelta(hours=23, minutes=5 * i), 55.0) for i in range(3)]
        series = hand_series(sparse, epoch_seconds=300.0)
        window = detect_sleep_window(series.slice(DAY_TWO + timedelta(hours=18), DAY_TWO + timedelta(days=1)))
        self.assertNotWindow(window, mentions="only 3 measured readings overnight")

    def test_a_night_with_no_sustained_stretch_is_refused(self) -> None:
        sparse = [(DAY_TWO + timedelta(hours=23, minutes=5 * i), 55.0) for i in range(8)]
        series = hand_series(sparse, epoch_seconds=300.0)
        window = detect_sleep_window(series.slice(DAY_TWO + timedelta(hours=18), DAY_TWO + timedelta(days=1)))
        self.assertNotWindow(window, mentions="no 45-minute stretch")

    def test_a_range_that_holds_no_night_says_so(self) -> None:
        morning = [(START + timedelta(minutes=i), 70.0) for i in range(0, 6 * 60, 5)]
        series = hand_series(morning, epoch_seconds=300.0)
        window = detect_sleep_window(series.slice(START, START + timedelta(hours=6)))
        self.assertNotWindow(window, mentions="contains no part of the night")

    def test_ordinary_jitter_does_not_shorten_the_night(self) -> None:
        # Same plateau, a deterministic +-2 bpm wobble: a wrist band at rest really does
        # wander by that much, and a growth rule that ended at the first wobble above the
        # block mean would report a one-hour night. The default smoothing plus the
        # noise-aware tolerance is what keeps the window the length of the plateau.
        points = plateau((0, 6, 52.0), (6, 23, 80.0), (23, 24, 52.0), day=START)
        points += plateau((0, 6, 52.0), (6, 23, 80.0), (23, 24, 52.0), day=DAY_TWO)
        wobbed = [
            (stamp, value + (2.0 if index % 7 in (0, 3) else -1.5 if index % 7 in (1, 4) else 0.5))
            for index, (stamp, value) in enumerate(points)
        ]
        series = hand_series(wobbed)
        anchored = series.slice(START + timedelta(hours=18), DAY_TWO + timedelta(hours=12))
        window = detect_sleep_window(anchored)
        self.assertTrue(window.detected)
        self.assertGreaterEqual(window.minutes, 400.0)
        self.assertAlmostEqual(window.mean_bpm, 52.0, delta=1.0)
        self.assertGreaterEqual(window.detail["growth_slack_bpm"], 3.0)
        # The same night read with smoothing off is not shorter either - the noise-aware
        # tolerance covers jitter on its own; smoothing is what stops the *block* search
        # latching onto a lucky low minute.
        strict = detect_sleep_window(anchored, smoothing_minutes=0.0)
        self.assertTrue(strict.detected)
        self.assertGreaterEqual(strict.minutes, 300.0)

    def test_an_excursion_shorter_than_patience_does_not_end_the_night(self) -> None:
        # One 10-minute spike in the middle of the plateau is not a waking episode.
        points = plateau((0, 6, 55.0), (6, 23, 80.0), (23, 24, 55.0))
        points += plateau((0, 6, 55.0), (6, 23, 80.0), (23, 24, 55.0), day=DAY_TWO)
        spike_start = DAY_TWO + timedelta(hours=2)
        spiked = [
            (stamp, 95.0 if spike_start <= stamp < spike_start + timedelta(minutes=10) else value)
            for stamp, value in points
        ]
        series = hand_series(spiked)
        patient = detect_sleep_window(
            series.slice(START + timedelta(hours=18), DAY_TWO + timedelta(hours=12)),
            smoothing_minutes=0.0, patience_minutes=30.0,
        )
        impatient = detect_sleep_window(
            series.slice(START + timedelta(hours=18), DAY_TWO + timedelta(hours=12)),
            smoothing_minutes=0.0, patience_minutes=0.0,
        )
        self.assertWindow(patient, START + timedelta(hours=23), DAY_TWO + timedelta(hours=6))
        self.assertEqual(patient.minutes, 420.0)
        self.assertTrue(impatient.detected)
        self.assertLess(impatient.minutes, patient.minutes)


class RestingWindowTest(HrStoreTestCase):
    """``quiet-hour-sustained-minimum`` - the awake quietest stretch, not the night."""

    def sleep_window(self) -> TimeWindow:
        return TimeWindow(
            kind="sleep", heuristic=SLEEP_HEURISTIC,
            start=START, end=START + timedelta(hours=6),
            values=(52.0,) * 360, epoch_seconds=60.0,
        )

    def test_the_quiet_waking_hour_is_the_window(self) -> None:
        series = hand_series(one_day())
        window = detect_resting_window(
            series.slice(START, DAY_TWO), exclude=[self.sleep_window()], smoothing_minutes=0.0
        )
        self.assertEqual(window.heuristic, REST_HEURISTIC)
        self.assertWindow(window, START + timedelta(hours=9), START + timedelta(hours=10))
        self.assertEqual(window.minutes, 60.0)
        self.assertEqual(window.mean_bpm, 65.0)
        self.assertEqual(window.median_bpm, 65.0)
        self.assertEqual(window.samples, 60)
        self.assertTrue(window.covers(START + timedelta(hours=9, minutes=30)))
        self.assertFalse(window.covers(START + timedelta(hours=10)), "the end is exclusive")

    def test_sleep_is_not_resting_even_though_it_is_quieter(self) -> None:
        series = hand_series(one_day())
        asleep = self.sleep_window()
        with_exclusion = detect_resting_window(
            series.slice(START, DAY_TWO), exclude=[asleep], smoothing_minutes=0.0
        )
        without = detect_resting_window(series.slice(START, DAY_TWO), smoothing_minutes=0.0)
        self.assertEqual(with_exclusion.mean_bpm, 65.0)
        self.assertEqual(without.mean_bpm, 52.0, "without the exclusion the night wins")
        self.assertFalse(with_exclusion.overlaps(asleep.start, asleep.end))

    def test_a_day_shorter_than_the_sustained_block_shrinks_instead_of_failing(self) -> None:
        short = plateau((0, 0.5, 61.0))  # 30 minutes of readings
        series = hand_series(short)
        window = detect_resting_window(series, sustained_minutes=30.0, min_minutes=15.0, smoothing_minutes=0.0)
        self.assertTrue(window.detected)
        self.assertGreaterEqual(window.minutes, 15.0)

    def test_a_day_with_almost_nothing_is_refused_with_the_count(self) -> None:
        thin = plateau((0, 0.1, 61.0))  # 6 minutes
        series = hand_series(thin)
        window = detect_resting_window(series, smoothing_minutes=0.0)
        self.assertNotWindow(window, mentions="eligible minutes")

    def test_too_few_eligible_readings_after_exclusion_is_refused(self) -> None:
        series = hand_series(one_day())
        huge = TimeWindow(
            kind="sleep", heuristic=SLEEP_HEURISTIC,
            start=START, end=DAY_TWO - timedelta(minutes=3),
            values=(52.0,) * 100, epoch_seconds=60.0,
        )
        window = detect_resting_window(series.slice(START, DAY_TWO), exclude=[huge], smoothing_minutes=0.0)
        self.assertNotWindow(window, mentions="eligible readings outside the sleep window")

    def test_growth_does_not_cross_an_excluded_window(self) -> None:
        # Two quiet stretches either side of the night; growth from the lower one may not
        # walk through the sleep window into the other.
        points = plateau((0, 6, 55.0), (6, 7, 66.0), (7, 20, 80.0), (20, 21, 68.0), (21, 24, 80.0))
        series = hand_series(points)
        asleep = TimeWindow(
            kind="sleep", heuristic=SLEEP_HEURISTIC,
            start=START, end=START + timedelta(hours=6), values=(55.0,) * 360, epoch_seconds=60.0,
        )
        window = detect_resting_window(
            series.slice(START, DAY_TWO), exclude=[asleep], smoothing_minutes=0.0
        )
        self.assertWindow(window, START + timedelta(hours=6), START + timedelta(hours=7))


class WindowApiTest(HrStoreTestCase):
    """The shape a caller reads, detected or not."""

    def test_a_detected_window_reports_its_numbers_and_its_unit(self) -> None:
        window = TimeWindow(
            kind="rest", heuristic=REST_HEURISTIC,
            start=START, end=START + timedelta(minutes=30),
            values=(61.0, 59.0, 60.0), epoch_seconds=60.0,
        )
        self.assertTrue(bool(window))
        payload = window.as_dict()
        self.assertEqual(payload["unit"], "beats per minute")
        self.assertEqual(payload["minutes"], 30.0)
        self.assertAlmostEqual(payload["mean_bpm"], 60.0, places=6)
        self.assertEqual(payload["samples"], 3)
        self.assertIsNone(payload["reason"])
        self.assertEqual(window.covers(START), True)
        self.assertEqual(window.overlaps(START + timedelta(hours=1), START + timedelta(hours=2)), False)

    def test_a_refused_window_carries_a_reason_and_no_edges(self) -> None:
        window = not_detected("sleep", SLEEP_HEURISTIC, "no readings", samples=0)
        self.assertFalse(bool(window))
        self.assertFalse(window.detected)
        self.assertFalse(window.covers(START))
        self.assertEqual(window.minutes, 0.0)
        self.assertIsNone(window.mean_bpm)
        self.assertEqual(window.not_detected_reason, "no readings")
        self.assertEqual(window.as_dict()["detail"], {"samples": 0})

    def test_an_empty_window_is_not_detected_even_with_edges(self) -> None:
        self.assertFalse(TimeWindow(kind="rest", heuristic=REST_HEURISTIC, start=START, end=START).detected)


class SmoothingMathTest(unittest.TestCase):
    def test_smoothing_count_translates_minutes_into_readings(self) -> None:
        self.assertEqual(smoothing_count(60.0, 15.0), 15)
        self.assertEqual(smoothing_count(300.0, 15.0), 3)
        self.assertEqual(smoothing_count(300.0, 0.0), 1)

    def test_a_centred_mean_does_not_shift_the_edge(self) -> None:
        self.assertEqual(smoothed([1.0, 2.0, 3.0, 4.0], 3), [1.5, 2.0, 3.0, 3.5])
        self.assertEqual(smoothed([5.0], 4), [5.0])
        self.assertEqual(smoothed([5.0, 6.0], 1), [5.0, 6.0])

    def test_growth_tolerance_never_ignores_the_block_s_own_noise(self) -> None:
        self.assertEqual(growth_slack([55.0] * 10, 3.0), 3.0)
        self.assertEqual(growth_slack([50.0, 60.0] * 5, 3.0), 10.0)
        self.assertEqual(growth_slack([55.0], 3.0), 3.0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
