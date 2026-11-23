"""Windowed HRV features, with every value worked out by hand.

The hand-built series here are the point: an alternating 60/70 bpm sequence at a one-minute
epoch has an SDNN, an RMSSD, a pNN50 and an irregularity index that can be written down
before the code runs, so a change in a formula cannot slide past unnoticed.

The count gate is asserted as hard as the numbers, because it is the one that decides
whether a value exists at all: below ``MIN_INTERVALS`` intervals the features are ``None``
and ``reason`` says how many there were.
"""

from __future__ import annotations

import unittest
from datetime import timedelta

from fitbit_monitor.hr import estimate_intervals, hrv_features
from fitbit_monitor.hr.hrv import MIN_INTERVALS, HrvFeatures, Interval
from fitbit_monitor.hr.util import HRV_HEURISTIC, UNIT_MS
from hr_support import HrStoreTestCase, START, hand_series, plateau, stamps

# 60 and 70 bpm at one-minute spacing: 1000 ms and 60000/70 ms intervals, alternating.
FAST_MS = 60_000.0 / 70.0


def alternating(count: int = 9) -> list[tuple]:
    rates = [60.0, 70.0] * (count // 2 + 1)
    return list(zip(stamps(count), rates[:count]))


def steady(count: int = 9, *, bpm: float = 60.0) -> list[tuple]:
    return list(zip(stamps(count), [bpm] * count))


class IntervalEstimateTest(HrStoreTestCase):
    """RR estimates come from consecutive *measured* readings one epoch apart."""

    def test_each_interval_is_the_rate_of_the_reading_it_starts_at(self) -> None:
        series = hand_series(alternating(4))
        intervals, skipped = estimate_intervals(series.measured_samples, epoch_seconds=60.0)
        self.assertEqual(skipped, 0)
        self.assertEqual(len(intervals), 3)
        self.assertEqual([round(i.ms, 4) for i in intervals], [1000.0, round(FAST_MS, 4), 1000.0])
        self.assertEqual(intervals[0].start, START)
        self.assertEqual(intervals[0].end, START + timedelta(minutes=1))
        self.assertEqual(intervals[0].span_seconds, 60.0)
        self.assertEqual(intervals[0].unit, UNIT_MS)
        self.assertEqual(intervals[0].as_dict()["ms"], 1000.0)

    def test_a_pair_that_straddles_a_gap_is_counted_as_skipped_not_dropped(self) -> None:
        instants = stamps(5)
        missing = instants[2]
        samples = hand_series([(ts, 60.0) for ts in instants if ts != missing], epoch_seconds=60.0)
        intervals, skipped = estimate_intervals(samples.measured_samples, epoch_seconds=60.0)
        self.assertEqual(len(intervals), 2, "four readings, one straddling pair skipped")
        self.assertEqual(skipped, 1)

    def test_an_imputed_reading_never_becomes_an_interval(self) -> None:
        instants = stamps(9)
        holed = [(ts, 60.0) for ts in instants if ts != instants[4]]
        filled = hand_series(holed, epoch_seconds=60.0, impute_seconds=120.0)
        self.assertEqual(filled.counts["imputed"], 1)
        intervals, skipped = estimate_intervals(filled.measured_samples, epoch_seconds=60.0)
        self.assertEqual(skipped, 1, "the filled minute is not evidence; the step over it is skipped")
        self.assertEqual(len(intervals), 6)

    def test_a_refused_value_skips_its_pairs(self) -> None:
        points = steady(6)
        points[2] = (points[2][0], 0.0)
        series = hand_series(points, epoch_seconds=60.0)
        intervals, skipped = estimate_intervals(series.measured_samples, epoch_seconds=60.0)
        self.assertEqual(len(intervals), 3)
        self.assertEqual(skipped, 1, "the step where the refused reading was spans two epochs")


class HrvFeatureTest(HrStoreTestCase):
    """The four features, on series whose answers are known on paper."""

    def test_an_alternating_series_computes_to_the_hand_worked_numbers(self) -> None:
        features = hrv_features(hand_series(alternating(9)), epoch_seconds=60.0)
        self.assertEqual(features.heuristic, HRV_HEURISTIC)
        self.assertTrue(features.emitted, features.reason)
        self.assertEqual(features.interval_count, 8)
        self.assertEqual(features.sample_count, 9)
        self.assertEqual(features.skipped_pairs, 0)
        # intervals: 1000, 857.14, x4 -> mean 928.571, sd 71.43; every successive
        # difference is 1000 - 60000/70 = 142.857 ms, so RMSSD is that number exactly.
        self.assertAlmostEqual(features.mean_interval_ms, 928.5714285714286, places=6)
        self.assertAlmostEqual(features.median_interval_ms, 928.5714285714286, places=6)
        self.assertAlmostEqual(features.sdnn_ms, 71.4285714285714, places=6)
        self.assertAlmostEqual(features.rmssd_ms, 142.8571428571429, places=6)
        self.assertAlmostEqual(features.irregularity_index, 142.8571428571429 / 928.5714285714286, places=6)
        # pNN50 threshold is 5% of the mean interval here (46.43 ms), and every difference
        # is 142.9 ms, so the window is maximally irregular on that measure too.
        self.assertAlmostEqual(features.pnn50_threshold_ms, 46.42857142857143, places=6)
        self.assertEqual(features.pnn50, 1.0)
        self.assertAlmostEqual(features.mean_bpm, 64.6153846153846, places=6)

    def test_a_perfectly_regular_series_reads_zero_on_every_irregularity_measure(self) -> None:
        features = hrv_features(hand_series(steady(9)), epoch_seconds=60.0)
        self.assertTrue(features.emitted)
        self.assertEqual(features.interval_count, 8)
        self.assertEqual(features.sdnn_ms, 0.0)
        self.assertEqual(features.rmssd_ms, 0.0)
        self.assertEqual(features.pnn50, 0.0)
        self.assertEqual(features.irregularity_index, 0.0)
        self.assertAlmostEqual(features.mean_interval_ms, 1000.0, places=6)
        self.assertAlmostEqual(features.mean_bpm, 60.0, places=6)
        self.assertEqual(features.pnn50_threshold_ms, 50.0, "5% of a second is the classic 50 ms")

    def test_a_steady_but_faster_window_keeps_the_pnn50_bar_proportional(self) -> None:
        features = hrv_features(hand_series(steady(9, bpm=120.0)), epoch_seconds=60.0)
        self.assertAlmostEqual(features.mean_interval_ms, 500.0, places=6)
        self.assertAlmostEqual(features.pnn50_threshold_ms, 25.0, places=6)

    def test_below_the_minimum_sample_count_nothing_is_emitted(self) -> None:
        self.assertEqual(MIN_INTERVALS, 8)
        features = hrv_features(hand_series(steady(5)), epoch_seconds=60.0)
        self.assertFalse(features.emitted)
        self.assertEqual(features.interval_count, 4)
        self.assertIsNone(features.sdnn_ms)
        self.assertIsNone(features.rmssd_ms)
        self.assertIsNone(features.pnn50)
        self.assertIsNone(features.irregularity_index)
        self.assertIsNone(features.mean_interval_ms)
        self.assertIn("4 successive intervals", features.not_emitted_reason or "")
        self.assertIn("need 8", features.not_emitted_reason or "")

    def test_the_minimum_is_the_caller_s_to_raise(self) -> None:
        series = hand_series(alternating(9))
        self.assertTrue(hrv_features(series, epoch_seconds=60.0, min_intervals=8).emitted)
        strict = hrv_features(series, epoch_seconds=60.0, min_intervals=20)
        self.assertFalse(strict.emitted)
        self.assertIn("need 20", strict.not_emitted_reason or "")

    def test_a_window_broken_by_a_gap_loses_exactly_the_intervals_it_should(self) -> None:
        instants = stamps(11)
        holed = [(ts, 60.0) for i, ts in enumerate(instants) if i != 5]
        features = hrv_features(hand_series(holed, epoch_seconds=60.0), epoch_seconds=60.0)
        self.assertEqual(features.sample_count, 10)
        self.assertEqual(features.skipped_pairs, 1)
        self.assertEqual(features.interval_count, 8, "9 pairs, one spanning the hole")
        self.assertTrue(features.emitted)

    def test_five_minute_data_uses_the_data_s_own_epoch(self) -> None:
        points = plateau((0, 1, 62.0), step_minutes=5)
        series = hand_series(points, epoch_seconds=300.0)
        features = hrv_features(series)
        self.assertEqual(features.epoch_seconds, 300.0)
        self.assertEqual(features.interval_count, 11)
        self.assertTrue(features.emitted)
        self.assertAlmostEqual(features.mean_interval_ms, 60_000.0 / 62.0, places=6)

    def test_a_feature_set_can_be_built_from_a_plain_list_of_samples(self) -> None:
        samples = hand_series(steady(9), epoch_seconds=60.0).measured_samples
        features = hrv_features(samples)
        self.assertTrue(features.emitted)
        self.assertEqual(features.since, START)
        self.assertEqual(features.until, START + timedelta(minutes=8))

    def test_an_empty_window_reports_instead_of_dividing_by_zero(self) -> None:
        features = hrv_features(())
        self.assertFalse(features.emitted)
        self.assertEqual(features.interval_count, 0)
        self.assertIsNone(features.since)
        self.assertIn("0 successive intervals", features.not_emitted_reason or "")

    def test_a_series_with_a_single_reading_reports_itself_as_unmeasurable(self) -> None:
        features = hrv_features(hand_series(steady(1), epoch_seconds=60.0), epoch_seconds=60.0)
        self.assertFalse(features.emitted)
        self.assertEqual(features.sample_count, 1)

    def test_as_dict_carries_units_counts_and_the_reason_together(self) -> None:
        payload = hrv_features(hand_series(alternating(9)), epoch_seconds=60.0).as_dict()
        self.assertEqual(payload["unit"], UNIT_MS)
        self.assertEqual(payload["heuristic"], HRV_HEURISTIC)
        self.assertTrue(payload["emitted"])
        self.assertEqual(payload["intervals"], 8)
        self.assertEqual(payload["min_intervals"], 8)
        self.assertAlmostEqual(payload["sdnn_ms"], 71.43, places=2)
        self.assertAlmostEqual(payload["rmssd_ms"], 142.86, places=2)
        self.assertEqual(payload["pnn50"], 1.0)
        self.assertAlmostEqual(payload["irregularity_index"], 0.1538, places=4)
        self.assertIsNone(payload["reason"])
        self.assertTrue(payload["since"].startswith("2026-05-01"))

    def test_the_refused_path_still_serialises_with_nones(self) -> None:
        payload = hrv_features(()).as_dict()
        self.assertIsNone(payload["sdnn_ms"])
        self.assertIsNone(payload["rmssd_ms"])
        self.assertIsNone(payload["irregularity_index"])
        self.assertFalse(payload["emitted"])
        self.assertTrue(payload["reason"])

    def test_labels_and_defaults_survive_into_the_output(self) -> None:
        features = hrv_features(hand_series(alternating(9)), epoch_seconds=60.0, label="sleep")
        self.assertEqual(features.detail["label"], "sleep")
        self.assertEqual(features.as_dict()["detail"]["label"], "sleep")

    def test_defaults_on_a_fresh_object_are_a_valid_empty_window(self) -> None:
        empty = HrvFeatures()
        self.assertEqual(empty.min_intervals, MIN_INTERVALS)
        self.assertEqual(empty.values_ms, ())
        self.assertEqual(empty.interval_count, 0)
        self.assertFalse(empty.emitted)
        self.assertEqual(Interval(START, START, 1000.0, 60.0).ms, 1000.0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
