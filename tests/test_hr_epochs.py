"""The per-epoch series: one row per epoch, with gaps and impossible values identified.

The promise under test is the one the rest of the package stands on - *identified, not
silently dropped*. So each case here writes a series whose defect is known in advance (a
dropout, a null, a two-hour hole, a day with three readings, an empty day) and asserts the
number the library reports for it, including the count that a downstream threshold would
divide by.

The store is real (a temp SQLite file behind the read API) and the grain is real (60 s from
the catalog), because "the corpus is 5-minute data and the catalog says 60 s" is exactly the
mismatch that makes a coverage number lie.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from fitbit_monitor.hr import epochs
from fitbit_monitor.hr.epochs import classify, epoch_series, find_gaps, grain_seconds
from fitbit_monitor.hr.util import (
    DEFAULT_MAX_BPM,
    DEFAULT_MIN_BPM,
    FLAG_IMPUTED,
    FLAG_NOT_A_NUMBER,
    FLAG_OUT_OF_RANGE,
)
from hr_support import HrStoreTestCase, START, hand_series, plateau, stamps


class ClassificationTest(HrStoreTestCase):
    """What a stored value becomes: a rate, or a record of why it is not one."""

    def judge(self, value, *, minimum=25.0, maximum=220.0):
        return classify(START, value, epoch_seconds=60.0, min_plausible_bpm=minimum, max_plausible_bpm=maximum)

    def test_a_ordinary_reading_is_measured_and_carries_the_unit(self) -> None:
        sample = self.judge(61.4)
        self.assertTrue(sample.measured)
        self.assertTrue(sample.usable)
        self.assertFalse(sample.refused)
        self.assertEqual(sample.bpm, 61.4)
        self.assertEqual(sample.raw, 61.4)
        self.assertEqual(sample.unit, "beats per minute")
        self.assertIsNone(sample.reason)

    def test_zero_is_the_device_saying_no_signal_not_a_heart_rate(self) -> None:
        sample = self.judge(0)
        self.assertFalse(sample.usable)
        self.assertTrue(sample.refused)
        self.assertEqual(sample.flags, (FLAG_OUT_OF_RANGE,))
        self.assertEqual(sample.raw, 0.0)
        self.assertIn("outside the plausible band", sample.reason or "")

    def test_the_band_bounds_are_inclusive_and_the_band_is_the_documented_one(self) -> None:
        self.assertTrue(self.judge(25.0).measured)
        self.assertTrue(self.judge(220.0).measured)
        self.assertTrue(self.judge(24.9).refused)
        self.assertTrue(self.judge(220.1).refused)
        self.assertEqual((DEFAULT_MIN_BPM, DEFAULT_MAX_BPM), (25.0, 220.0))

    def test_the_optical_sensor_saturation_value_is_refused_not_reported_as_a_maximum(self) -> None:
        # 255 is what a Fitbit payload carries when the optical sensor clips; a rule that
        # reads it as "255 bpm" will never be wrong in a direction anyone notices.
        self.assertTrue(self.judge(255).refused)
        self.assertTrue(self.judge(999).refused)

    def test_null_and_non_numeric_payloads_are_not_a_number(self) -> None:
        for junk in (None, "61", float("nan"), float("inf"), True):
            with self.subTest(value=junk):
                sample = self.judge(junk)
                self.assertEqual(sample.flags, (FLAG_NOT_A_NUMBER,))
                self.assertIsNone(sample.bpm)
                self.assertIn("is not a number", sample.reason or "")

    def test_a_negative_rate_is_out_of_range(self) -> None:
        self.assertTrue(self.judge(-4).refused)


class EpochSeriesShapeTest(HrStoreTestCase):
    """Counts, coverage and the epoch the durations are measured in."""

    def test_a_clean_half_hour_counts_itself_exactly(self) -> None:
        instants = self.put_flat(*[60.0] * 30)
        series = self.series(START, START + timedelta(minutes=30))
        self.assertEqual(series.counts["samples"], 30)
        self.assertEqual(series.counts["measured"], 30)
        self.assertEqual(series.counts["impossible"], 0)
        self.assertEqual(series.counts["imputed"], 0)
        self.assertEqual(series.first_ts, instants[0])
        self.assertEqual(series.last_ts, instants[-1])
        self.assertEqual(len(series.values), 30)
        # 30 readings x 60 s over a 1800 s window: complete, not approximately so.
        self.assertAlmostEqual(series.coverage, 1.0, places=6)
        self.assertEqual(series.observed_seconds, 1800.0)
        self.assertEqual(series.gaps, ())

    def test_the_band_can_be_widened_by_the_caller_and_not_only_narrowed(self) -> None:
        self.put_flat(24.0, 240.0)
        strict = self.series(START, START + timedelta(minutes=2), impute_seconds=0.0)
        self.assertEqual(strict.counts["impossible"], 2)
        loose = self.series(
            START,
            START + timedelta(minutes=2),
            min_plausible_bpm=20.0,
            max_plausible_bpm=260.0,
            impute_seconds=0.0,
        )
        self.assertEqual(loose.counts["impossible"], 0)
        self.assertEqual(loose.values, (24.0, 240.0))

    def test_impossible_values_are_listed_with_their_raw_value_not_dropped(self) -> None:
        self.put_flat(60.0, 0.0, 61.0, None, 255.0, 62.0)
        series = self.series(START, START + timedelta(minutes=6), impute_seconds=0.0)
        self.assertEqual(series.values, (60.0, 61.0, 62.0))
        self.assertEqual(series.measured_values, (60.0, 61.0, 62.0))
        impossible = series.impossible
        self.assertEqual(len(impossible), 3)
        self.assertEqual([s.raw for s in impossible], [0.0, None, 255.0])
        self.assertEqual(series.counts["out_of_range"], 2)
        self.assertEqual(series.counts["not_a_number"], 1)
        # The mean the rules use is over the three usable readings, and the count saying
        # "three" is what keeps a threshold honest about that.
        self.assertAlmostEqual(series.mean_bpm, 61.0, places=6)
        self.assertEqual(len(series.values), 3)

    def test_five_minute_data_is_measured_in_five_minute_epochs_not_the_catalog_sixty(self) -> None:
        # The synthetic corpus (and any sparse import) arrives at 5-minute spacing while
        # the catalog grain is 60 s. Coverage measured in catalog seconds would call a
        # complete day 20% empty.
        self.put_flat(*[65.0] * 12, step_seconds=300)
        series = self.series(START, START + timedelta(hours=1))
        self.assertEqual(series.epoch_seconds, 60.0)
        self.assertEqual(series.observed_epoch_seconds, 300.0)
        self.assertEqual(series.effective_epoch_seconds, 300.0)
        self.assertAlmostEqual(series.coverage, 1.0, places=6)
        self.assertEqual(series.gaps, (), "5-minute data is sparse, not broken")

    def test_the_observed_grain_needs_evidence_before_it_is_trusted(self) -> None:
        instants = stamps(4, step_seconds=300)
        self.put(zip(instants, [70.0] * 4))
        series = self.series(instants[0], instants[-1] + timedelta(seconds=300))
        self.assertIsNone(series.observed_epoch_seconds, "4 spacings is not a grain")
        self.assertEqual(series.effective_epoch_seconds, 60.0)

    def test_a_day_with_no_data_says_so_with_one_gap_and_no_values(self) -> None:
        self.put_flat(*[60.0] * 6, start=START + timedelta(days=3))
        empty = self.series(START, START + timedelta(days=1))
        self.assertFalse(empty.has_data)
        self.assertEqual(empty.values, ())
        self.assertIsNone(empty.mean_bpm)
        self.assertEqual(empty.coverage, 0.0)
        self.assertEqual(len(empty.gaps), 1)
        self.assertEqual(empty.gaps[0].seconds, 86400.0)
        self.assertEqual(empty.gaps[0].start, START)
        self.assertEqual(empty.counts["samples"], 0)
        self.assertFalse(empty.as_dict()["impossible"])

    def test_a_day_with_only_a_few_samples_reports_them_and_the_holes_around_them(self) -> None:
        instants = [START + timedelta(hours=9, minutes=10), START + timedelta(hours=9, minutes=11)]
        self.put(zip(instants, [72.0, 74.0]))
        series = self.series(START, START + timedelta(days=1))
        self.assertEqual(series.values, (72.0, 74.0))
        self.assertEqual(series.counts["measured"], 2)
        self.assertAlmostEqual(series.coverage, 120 / 86400.0, places=8)
        # Two edge gaps (before and after) - and they are *reported*, not smoothed away,
        # longest first, which is the order a report wants.
        self.assertEqual(len(series.gaps), 2)
        self.assertEqual([gap.seconds for gap in series.gaps], [53340.0, 33000.0])
        self.assertEqual(series.max_gap_seconds, 53340.0)

    def test_a_reimport_of_the_same_instant_is_one_row_not_two(self) -> None:
        # The store upserts by (dataset, instant), so a range re-imported twice cannot
        # hand this layer the same minute twice - and the sample count says so.
        self.put([(START, 60.0), (START, 61.0), (START + timedelta(minutes=1), 62.0)])
        series = self.series(START, START + timedelta(minutes=2))
        self.assertEqual(series.values, (61.0, 62.0))
        self.assertEqual(series.counts["samples"], 2)

    def test_as_dict_carries_the_counts_gaps_and_the_impossible_list(self) -> None:
        self.put_flat(60.0, 0.0, 61.0)
        payload = self.series(START, START + timedelta(minutes=3), impute_seconds=0.0).as_dict()
        self.assertEqual(payload["unit"], "beats per minute")
        self.assertEqual(payload["plausible_band_bpm"], [25.0, 220.0])
        self.assertEqual(payload["impossible"], [payload["impossible"][0]])
        self.assertEqual(len(payload["impossible"]), 1)
        self.assertEqual(payload["impossible"][0]["raw"], 0.0)
        self.assertEqual(payload["usable"], 2)
        self.assertEqual(payload["out_of_range"], 1)
        self.assertEqual(payload["gaps"], 0)
        self.assertEqual(payload["mean_bpm"], 60.5)

    def test_the_window_has_to_be_a_window(self) -> None:
        with self.assertRaises(ValueError):
            epoch_series(self.reader(), START, START)
        with self.assertRaises(ValueError):
            epoch_series(self.reader(), START + timedelta(days=1), START)

    def test_naive_instants_are_refused_rather_than_guessed(self) -> None:
        with self.assertRaises(ValueError):
            epoch_series(self.reader(), datetime(2026, 5, 1), datetime(2026, 5, 2))


class GapTest(HrStoreTestCase):
    """Where the series is broken, and how that is stated."""

    def test_an_interior_two_hour_hole_is_one_gap_of_the_right_length(self) -> None:
        points = plateau((0, 10, 62.0), (12, 24, 63.0), step_minutes=5)
        series = self.day_series(points, epoch_seconds=60)
        self.assertEqual(len(series.gaps), 1)
        gap = series.gaps[0]
        self.assertEqual(gap.start, START + timedelta(hours=9, minutes=55))
        self.assertEqual(gap.end, START + timedelta(hours=12))
        self.assertEqual(gap.seconds, 125 * 60)
        self.assertEqual(series.longest_gap, gap)
        self.assertEqual(gap.as_dict()["seconds"], 7500.0)

    def test_the_missing_night_is_a_gap_even_though_no_two_readings_straddle_it(self) -> None:
        # A store whose first reading of the day is at 11:00 has no *interior* hole at all;
        # the morning is only found missing by looking at the edges of the requested range.
        self.put(plateau((11, 24, 70.0), step_minutes=1))
        series = self.series(START, START + timedelta(days=1))
        self.assertEqual(len(series.gaps), 1)
        self.assertEqual(series.gaps[0].start, START)
        self.assertEqual(series.gaps[0].end, START + timedelta(hours=11))
        self.assertEqual(series.gaps[0].seconds, 11 * 3600)

    def test_a_short_break_is_not_a_gap_because_the_grain_tolerates_it(self) -> None:
        points = plateau((0, 10, 60.0), step_minutes=1)
        points = [pair for pair in points if pair[0] != START + timedelta(minutes=4)]
        series = self.day_series(points)
        self.assertEqual(series.gaps, ())
        self.assertEqual(series.counts["measured"], 599)

    def test_a_refused_reading_breaks_the_series_for_counting_but_not_for_grain(self) -> None:
        points = plateau((0, 1, 60.0), step_minutes=1)
        points[30] = (START + timedelta(minutes=30), 0.0)
        series = self.day_series(points, impute_seconds=0.0)
        self.assertEqual(series.counts["impossible"], 1)
        self.assertEqual(len(series.values), 59)
        self.assertEqual(series.gaps, (), "one refused minute is under the gap tolerance")

    def test_find_gaps_reports_edge_breaks_only_when_asked(self) -> None:
        instants = stamps(10, start=START + timedelta(hours=1))
        samples = epochs.build_samples([(ts, 60.0) for ts in instants], epoch_seconds=60.0)
        with_edges = find_gaps(samples, epoch_seconds=60.0, since=START, until=START + timedelta(hours=2))
        without = find_gaps(samples, epoch_seconds=60.0, since=START, until=START + timedelta(hours=2), edges=False)
        self.assertEqual(len(with_edges), 2)
        self.assertEqual(without, ())

    def test_grain_seconds_is_the_lower_quartile_of_spacings(self) -> None:
        instants = stamps(9) + [stamps(9)[-1] + timedelta(minutes=90)]
        samples = epochs.build_samples([(ts, 60.0) for ts in instants], epoch_seconds=60.0)
        self.assertEqual(grain_seconds(samples), 60.0)
        self.assertIsNone(grain_seconds(samples[:5]))


class ImputationTest(HrStoreTestCase):
    """Short holes may be filled, visibly, and only between two real readings."""

    def test_a_two_minute_hole_is_filled_with_a_flag_and_not_counted_as_measured(self) -> None:
        points = [(START, 60.0), (START + timedelta(minutes=2), 64.0)]
        # 60 ... gap ... 64 at one-minute spacing: one reading is invented at 62, linearly.
        series = hand_series(points, epoch_seconds=60.0, impute_seconds=120.0)
        self.assertEqual(series.values, (60.0, 62.0, 64.0))
        self.assertEqual(series.measured_values, (60.0, 64.0))
        self.assertEqual(series.counts["imputed"], 1)
        imputed = series.imputed_samples[0]
        self.assertEqual(imputed.flags, (FLAG_IMPUTED,))
        self.assertEqual(imputed.ts, START + timedelta(minutes=1))
        self.assertIn("interpolated", imputed.reason or "")

    def test_nothing_is_invented_across_a_refused_reading(self) -> None:
        points = [(START, 60.0), (START + timedelta(minutes=1), 0.0), (START + timedelta(minutes=2), 64.0)]
        series = hand_series(points, epoch_seconds=60.0, impute_seconds=60.0)
        self.assertEqual(series.counts["imputed"], 0)
        self.assertEqual(series.values, (60.0, 64.0))
        self.assertEqual(series.counts["impossible"], 1)

    def test_imputation_is_off_by_default_for_a_hand_series_and_limited_to_two_rows(self) -> None:
        points = [(START, 60.0), (START + timedelta(minutes=2), 64.0)]
        self.assertEqual(hand_series(points).counts["imputed"], 0)
        wide = [(START, 60.0), (START + timedelta(minutes=3), 66.0)]
        filled = hand_series(wide, impute_seconds=120.0)
        self.assertEqual(filled.counts["imputed"], 2, "at most two readings are ever invented")
        beyond = [(START, 60.0), (START + timedelta(minutes=5), 66.0)]
        self.assertEqual(hand_series(beyond, impute_seconds=120.0).counts["imputed"], 0)

    def test_the_store_backed_default_fills_one_row_at_the_data_s_own_spacing(self) -> None:
        # Ten readings every 3 minutes with one 6-minute hole. The grain the data has is
        # 180 s, so the default invents ONE reading - it does not retrofit the catalog's
        # 60-second grain into a corpus that never had it.
        instants = [START + timedelta(seconds=180 * i) for i in range(10)]
        self.put([(ts, 60.0 + i) for i, ts in enumerate(instants) if i != 4])
        series = self.series(START, instants[-1] + timedelta(seconds=180))
        self.assertEqual(series.observed_epoch_seconds, 180.0)
        self.assertEqual(series.effective_epoch_seconds, 180.0)
        self.assertEqual(series.impute_seconds, 360.0)
        self.assertEqual(series.counts["measured"], 9)
        self.assertEqual(series.counts["imputed"], 1)
        self.assertEqual(len(series.values), 10)
        self.assertEqual(series.gaps, (), "the hole was filled, and the fill is flagged")


class SliceTest(HrStoreTestCase):
    """A sub-range is a series too, with its own gaps."""

    def setUp(self) -> None:
        super().setUp()
        # A morning at rest, an hour of exercise 09:00-10:00, a hole until 14:00, evening.
        self.points = plateau((0, 9, 55.0), (9, 10, 120.0), (14, 24, 66.0), step_minutes=1)
        self.put(self.points)
        self.series_full = self.series(START, START + timedelta(days=1))

    def test_a_slice_keeps_only_its_own_readings(self) -> None:
        piece = self.series_full.slice(START + timedelta(hours=9), START + timedelta(hours=10))
        self.assertEqual(len(piece.values), 60)
        self.assertEqual(piece.mean_bpm, 120.0)
        self.assertEqual(
            piece.between(START + timedelta(hours=9, minutes=30), START + timedelta(hours=10)),
            (120.0,) * 30,
        )

    def test_a_slice_wholly_inside_a_hole_reports_the_hole(self) -> None:
        hole = self.series_full.slice(START + timedelta(hours=10), START + timedelta(hours=12))
        self.assertFalse(hole.has_data)
        self.assertEqual(len(hole.gaps), 1)
        self.assertEqual(hole.gaps[0].seconds, 2 * 3600)

    def test_a_slice_starting_before_a_hole_reports_the_hole_it_straddles(self) -> None:
        edge = self.series_full.slice(START + timedelta(hours=8, minutes=30), START + timedelta(hours=15))
        self.assertTrue(edge.has_data)
        self.assertEqual(edge.gaps[0].seconds, 4 * 3600 + 60)

    def test_slicing_preserves_the_band_and_the_grain(self) -> None:
        piece = self.series_full.slice(START, START + timedelta(hours=1))
        self.assertEqual(piece.min_plausible_bpm, 25.0)
        self.assertEqual(piece.effective_epoch_seconds, 60.0)
        self.assertEqual(piece.dataset, "heart_rate")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
