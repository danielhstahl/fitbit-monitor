"""Baselines and contexts: personal levels, robust spreads, provisional history, exclusion accounting.

``test_anomaly_engine.py`` asks what the engine reported; this file asks whether the numbers
the rules were handed are the ones a reader would compute from the same days by hand. The
fixtures are deliberately small - a handful of days at a coarse epoch - because a baseline
built from thousands of readings is exactly the case where nobody can check the arithmetic.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta

from fitbit_monitor.anomaly.baseline import (
    MAD_TO_SIGMA,
    RateBaseline,
    build_rate_baseline,
    build_scatter_baseline,
    median_absolute_deviation,
    robust_sigma,
)
from fitbit_monitor.anomaly.config import AnomalyConfig, AnomalyConfigError
from fitbit_monitor.anomaly.context import ContextDay, context_days
from fitbit_monitor.hr import FeatureOptions
from fitbit_monitor.hr.daily import day_features
from fitbit_monitor.hr.epochs import epoch_series
from fitbit_monitor.hr.util import day_sequence
from fitbit_monitor.hr.windows import TimeWindow
from hr_support import HrStoreTestCase

from anomaly_support import QUIET_AWAKE, QUIET_NIGHT, START, scenario_points, span_of

#: Deliberately coarse and short: four nights of 1-minute data would take a minute to run and
#: nobody could verify the numbers by hand. Five-minute epochs, three quiet days.
EPOCH = 300


def _samples(day: datetime, values: list[float]) -> list[tuple[datetime, float]]:
    return [(day + timedelta(seconds=EPOCH * index), value) for index, value in enumerate(values)]


class RobustSigmaTestCase(HrStoreTestCase):
    """The spread every threshold is scaled by: MAD-scaled, with a floor that is a policy."""

    def test_a_clean_run_scales_by_the_median_absolute_deviation(self) -> None:
        # Symmetric, no outliers: the MAD of (70, 75, 80) is 5.0, and MAD_TO_SIGMA is the
        # conversion that makes it agree with a standard deviation on data that is normal.
        self.assertAlmostEqual(median_absolute_deviation([70.0, 75.0, 80.0]), 5.0, places=6)
        self.assertAlmostEqual(robust_sigma([70.0, 75.0, 80.0]), MAD_TO_SIGMA * 5.0, places=6)

    def test_outliers_cannot_drag_the_spread_the_thresholds_are_scaled_by(self) -> None:
        clean = [58.0, 59.0, 60.0, 61.0, 62.0, 63.0, 64.0]
        with_spikes = clean + [140.0, 150.0, 160.0]
        self.assertLess(robust_sigma(with_spikes), 2.0 * robust_sigma(clean))
        # The contrast that justifies the whole module: the ordinary standard deviation of the
        # same eight-plus-three numbers is several times its own value on the clean data, so a
        # threshold scaled by it would widen every time the wearer had an episode - the
        # detector would learn to tolerate its own findings.
        spread = statistics.stdev
        self.assertGreater(spread(with_spikes), 4.0 * spread(clean))

    def test_a_perfectly_flat_window_still_has_a_usable_spread(self) -> None:
        # The floor is ``min_spread_bpm`` and it is not cosmetic: a spread of zero would make
        # ``level + z * spread`` the level itself, and every reading above the wearer's own
        # resting mean would be a sustained elevation.
        self.assertEqual(robust_sigma([55.0] * 20), 0.0)
        baseline = RateBaseline(context="rest", daily=((START.date(), 55.0),) * 9)
        self.assertEqual(baseline.sigma_bpm, AnomalyConfig().min_spread_bpm)
        self.assertGreater(baseline.high_threshold_bpm, baseline.level_bpm)

    def test_the_floor_is_configurable_because_it_is_an_opinion(self) -> None:
        tight = RateBaseline(
            context="rest",
            daily=((START.date(), 55.0),) * 9,
            config=AnomalyConfig(min_spread_bpm=6.0),
        )
        self.assertEqual(tight.sigma_bpm, 6.0)
        self.assertAlmostEqual(
            tight.high_threshold_bpm, 55.0 + 6.0 * AnomalyConfig().z_high, places=6
        )

    def test_nothing_to_measure_is_zero_not_an_error(self) -> None:
        self.assertEqual(robust_sigma([]), 0.0)
        self.assertEqual(robust_sigma([55.0]), 0.0)


class RateBaselineTestCase(HrStoreTestCase):
    """The personal level, and what it refuses to claim when the history is thin."""

    def setUp(self) -> None:
        super().setUp()
        self.config = AnomalyConfig()

    def day(self, index: int, night: float, awake: float) -> ContextDay:
        """One hand-built context day: eight resting readings at ``awake``."""
        points = _samples(START + index * timedelta(days=1), [awake] * 8)
        return ContextDay(
            day=(START + index * timedelta(days=1)).date(),
            context="rest",
            window=_detected_window(points),
            baseline_samples=tuple(_Sample(stamp, value) for stamp, value in points),
            detection_samples=tuple(_Sample(stamp, value) for stamp, value in points),
            epoch_seconds=float(EPOCH),
        )

        days = [self.day(index, QUIET_NIGHT, QUIET_AWAKE) for index in range(10)]
        baseline = build_rate_baseline("rest", days, self.config)
        self.assertTrue(baseline.usable, baseline.reason)
        self.assertTrue(baseline.established)
        self.assertEqual(baseline.days, 10)
        self.assertAlmostEqual(baseline.level_bpm, QUIET_AWAKE, places=3)
        self.assertIsNone(baseline.reason)

    def test_seven_days_is_the_line_and_the_reason_says_which_side_it_is_on(self) -> None:
        seven = build_rate_baseline("rest", [self.day(i, 0, 70.0) for i in range(7)], self.config)
        six = build_rate_baseline("rest", [self.day(i, 0, 70.0) for i in range(6)], self.config)
        self.assertTrue(seven.established)
        self.assertFalse(six.established)
        self.assertIn("provisional", six.reason)
        self.assertTrue(six.usable)

    def test_the_floor_below_which_nothing_is_claimed_is_configurable(self) -> None:
        config = AnomalyConfig()
        two = build_rate_baseline("rest", [self.day(i, 0, 70.0) for i in range(2)], config)
        self.assertFalse(two.usable)
        self.assertIn(str(config.provisional_min_days), two.reason)
        three = build_rate_baseline("rest", [self.day(i, 0, 70.0) for i in range(3)], config)
        self.assertTrue(three.usable)
        self.assertFalse(three.established)
        # An operator who wants to run on a fortnight of data instead of a month says so in
        # the config; the refusal moves with the number rather than being argued in code.
        eager = AnomalyConfig(provisional_min_days=2)
        self.assertTrue(
            build_rate_baseline("rest", [self.day(i, 0, 70.0) for i in range(2)], eager).usable
        )

    def test_one_odd_night_does_not_move_the_level(self) -> None:
        days = [self.day(index, QUIET_NIGHT, QUIET_AWAKE) for index in range(9)]
        days.append(self.day(9, QUIET_NIGHT, 130.0))
        baseline = build_rate_baseline("rest", days, self.config)
        self.assertAlmostEqual(baseline.level_bpm, QUIET_AWAKE, places=3)

    def test_thresholds_use_the_wearer_not_a_constant(self) -> None:
        quiet = build_rate_baseline("rest", [self.day(i, 0, 52.0) for i in range(9)], self.config)
        fit = build_rate_baseline("rest", [self.day(i, 0, 78.0) for i in range(9)], self.config)
        self.assertLess(quiet.high_threshold_bpm, fit.high_threshold_bpm)
        self.assertLess(quiet.low_threshold_bpm, fit.low_threshold_bpm)
        for baseline in (quiet, fit):
            self.assertGreater(baseline.high_threshold_bpm, baseline.level_bpm)
            self.assertLess(baseline.low_threshold_bpm, baseline.level_bpm)

    def test_the_minimum_step_swallows_trivially_small_differences(self) -> None:
        # ``min_high_delta_bpm`` exists because a wearer whose own spread is 1 bpm would
        # otherwise be told a single extra beat is an episode.
        baseline = build_rate_baseline(
            "rest", [self.day(i, 0, 70.0) for i in range(9)], self.config
        )
        self.assertGreaterEqual(
            baseline.high_threshold_bpm, baseline.level_bpm + self.config.min_high_delta_bpm
        )
        self.assertLessEqual(
            baseline.low_threshold_bpm, baseline.level_bpm - self.config.min_low_delta_bpm
        )

    def test_a_window_of_recent_days_can_be_asked_for(self) -> None:
        days = [self.day(index, 0, 60.0 + index) for index in range(12)]
        baseline = build_rate_baseline("rest", days, AnomalyConfig(baseline_window_days=8))
        self.assertEqual(baseline.days, 8)
        # Only the last eight days are in the level, so it sits above the whole-history median.
        self.assertGreater(
            baseline.level_bpm, build_rate_baseline("rest", days, self.config).level_bpm
        )

    def test_confidence_grows_with_history_and_says_so(self) -> None:
        thin = build_rate_baseline("rest", [self.day(i, 0, 70.0) for i in range(3)], self.config)
        more = build_rate_baseline("rest", [self.day(i, 0, 70.0) for i in range(5)], self.config)
        full = build_rate_baseline("rest", [self.day(i, 0, 70.0) for i in range(21)], self.config)
        self.assertLess(thin.confidence_factor, more.confidence_factor)
        self.assertLess(more.confidence_factor, full.confidence_factor)
        self.assertEqual(full.confidence_factor, 1.0)
        # Maturity is one thing and certainty is another: the cap that stops a finding claiming
        # certainty is applied by the engine (and tested there), not by the baseline.
        self.assertLess(self.config.confidence_ceiling, 1.0)


class ScatterBaselineTestCase(HrStoreTestCase):
    """The wearer's ordinary scatter, pooled from window *scatter* rather than window means."""

    def days(self, value: float, count: int = 5):
        return [((START + timedelta(days=index)).date(), [value] * 10) for index in range(count)]

    def test_pooled_windows_give_a_median_and_a_threshold(self) -> None:
        baseline = build_scatter_baseline("rest", self.days(0.05), AnomalyConfig())
        self.assertTrue(baseline.usable, baseline.reason)
        self.assertAlmostEqual(baseline.median_index, 0.05, places=4)
        self.assertGreater(baseline.threshold, baseline.median_index)

    def test_the_floor_holds_when_the_wearer_is_measured_too_coarsely_to_have_a_spread(
        self,
    ) -> None:
        baseline = build_scatter_baseline("rest", self.days(0.0), AnomalyConfig())
        self.assertEqual(baseline.threshold, AnomalyConfig().irregularity_floor)

    def test_too_few_days_is_reported_not_assumed(self) -> None:
        baseline = build_scatter_baseline("rest", self.days(0.1, count=1), AnomalyConfig())
        self.assertFalse(baseline.usable)
        self.assertIn("day", baseline.reason)


class ContextTestCase(HrStoreTestCase):
    """How the store becomes per-context days, and how every exclusion stays visible."""

    def setUp(self) -> None:
        super().setUp()
        # An exercise burst on one of the days, so the rest context has something to subtract
        # and therefore something to report having subtracted.
        self.points = scenario_points(
            4, step_minutes=EPOCH // 60, jitter=1.0, episodes=[(1, (13.0, 14.0, 150.0))]
        )
        self.put(self.points)
        self.since, self.until = span_of(self.points)
        self.options = FeatureOptions(epoch_seconds=EPOCH)
        self.series = epoch_series(self.reader(), self.since, self.until, epoch_seconds=EPOCH)
        self.days = [
            day_features(self.reader(), day, options=self.options, series=self.series)
            for day in day_sequence(self.since, self.until, self.options.tz)
        ]

    def test_rest_exercise_exclusion_is_counted_not_invisible(self) -> None:
        scanned = context_days(self.days, self.series, "rest", AnomalyConfig())
        self.assertTrue(scanned)
        self.assertTrue(all(item.context == "rest" for item in scanned))
        self.assertTrue(
            any(item.excluded_samples for item in scanned), "the seeded days contain exercise"
        )

    def test_sleep_context_subtracts_nothing_and_keeps_the_night(self) -> None:
        scanned = context_days(self.days, self.series, "sleep", AnomalyConfig())
        usable = [item for item in scanned if item.usable]
        self.assertTrue(usable)
        for item in usable:
            self.assertEqual(item.excluded_samples, 0)
            self.assertAlmostEqual(item.window.mean_bpm, QUIET_NIGHT, delta=4.0)

    def test_rest_level_is_the_awake_plateau_not_the_night(self) -> None:
        baseline = build_rate_baseline(
            "rest", context_days(self.days, self.series, "rest", AnomalyConfig())
        )
        self.assertTrue(baseline.usable)
        self.assertAlmostEqual(baseline.level_bpm, QUIET_AWAKE, delta=3.0)

    def test_an_unknown_context_is_rejected_at_the_door(self) -> None:

        with self.assertRaises(AnomalyConfigError):
            context_days(self.days, self.series, "sauna", AnomalyConfig())


class _Sample:
    """The two attributes :class:`ContextDay` reads off a sample."""

    def __init__(self, ts: datetime, bpm: float) -> None:
        self.ts = ts
        self.bpm = bpm


def _detected_window(points, kind: str = "resting"):
    """A detected :class:`TimeWindow` spanning hand-built readings."""
    return TimeWindow(
        kind=kind,
        heuristic="hand-built",
        start=points[0][0],
        end=points[-1][0] + timedelta(seconds=EPOCH),
        values=tuple(value for _stamp, value in points),
        epoch_seconds=float(EPOCH),
        detail={"hand_built": True},
    )
