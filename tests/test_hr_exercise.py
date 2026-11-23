"""The exercise exclusion mask: rates that were movement are not rates at rest.

Every assertion here is a rate the rule had to decide about, at a minute the test chose:
a 31-minute run that must be excluded (plus its cooldown), a one-minute spike that must
not be, a threshold that follows the wearer's own resting level rather than a textbook
constant, and the counts a caller needs in order to notice that a day is mostly gym.
"""

from __future__ import annotations

import unittest
from datetime import timedelta

from fitbit_monitor.hr import build_exercise_mask, threshold_bpm
from fitbit_monitor.hr.exercise import (
    DEFAULT_ABSOLUTE_FLOOR_BPM,
    DEFAULT_COOLDOWN_MINUTES,
    DEFAULT_RESTING_MULTIPLE,
    EXERCISE_SUSTAINED_MINUTES,
    ExerciseMask,
)
from fitbit_monitor.hr.util import EXERCISE_HEURISTIC
from hr_support import HrStoreTestCase, START, hand_series, plateau


def day_at_rest(*, value: float = 60.0, minutes: int = 24 * 60) -> list[tuple]:
    return plateau((0, minutes / 60, value))


def with_run(value: float, start_hour: float, length_minutes: int, base: float = 60.0) -> list[tuple]:
    """A resting day with one block of ``value`` readings, minute by minute."""
    points = day_at_rest(value=base)
    start = START + timedelta(hours=start_hour)
    stop = start + timedelta(minutes=length_minutes)
    return [(stamp, value if start <= stamp < stop else reading) for stamp, reading in points]


class ThresholdTest(unittest.TestCase):
    """The two halves of the bar: a personal multiple and an absolute floor."""

    def test_the_defaults_are_the_documented_ones(self) -> None:
        self.assertEqual(DEFAULT_RESTING_MULTIPLE, 1.35)
        self.assertEqual(DEFAULT_ABSOLUTE_FLOOR_BPM, 110.0)
        self.assertEqual(EXERCISE_SUSTAINED_MINUTES, 8.0)
        self.assertEqual(DEFAULT_COOLDOWN_MINUTES, 10.0)

    def test_without_a_resting_level_only_the_floor_applies(self) -> None:
        self.assertEqual(threshold_bpm(None), 110.0)
        self.assertEqual(threshold_bpm(0), 110.0)
        self.assertEqual(threshold_bpm(40.0), 110.0, "1.35 x 40 = 54, and the floor is 110")

    def test_a_higher_resting_rate_raises_the_bar_to_its_own_multiple(self) -> None:
        self.assertEqual(threshold_bpm(95.0), 129.0, "95 x 1.35 = 128.25, rounded up")
        self.assertEqual(threshold_bpm(90.0), 122.0, "90 x 1.35 = 121.5 -> 122")
        self.assertEqual(threshold_bpm(100.0), 135.0)

    def test_the_threshold_is_a_whole_number_of_bpm(self) -> None:
        # A threshold printed as 128.25 claims a precision the resting estimate does not
        # have, and invites someone to compare against it exactly.
        self.assertEqual(threshold_bpm(96.0), 130.0)
        self.assertEqual(float(threshold_bpm(96.0)).is_integer(), True)

    def test_the_parameters_are_parameters(self) -> None:
        self.assertEqual(threshold_bpm(80.0, resting_multiple=1.5), 120.0)
        self.assertEqual(threshold_bpm(80.0, absolute_floor_bpm=150.0), 150.0)


class ExerciseMaskTest(HrStoreTestCase):
    def test_a_sustained_run_is_excluded_with_a_cooldown_after_it(self) -> None:
        series = hand_series(with_run(150.0, 14.0, 31))
        mask = build_exercise_mask(series)
        self.assertEqual(mask.heuristic, EXERCISE_HEURISTIC)
        self.assertEqual(mask.threshold, 110.0)
        self.assertEqual(len(mask.spans), 1, "the single 09:00 spike is too short to count")
        span = mask.spans[0]
        self.assertEqual(span.start, START + timedelta(hours=14))
        # 31 candidate minutes plus the epoch of the last one plus ten minutes of recovery.
        self.assertEqual(span.end, START + timedelta(hours=14, minutes=41))
        self.assertEqual(span.minutes, 41.0)
        self.assertEqual(span.samples, 31)
        self.assertEqual(span.mean_bpm, 150.0)
        self.assertEqual(span.peak_bpm, 150.0)
        self.assertEqual(span.excess_bpm, 40.0)
        self.assertTrue(mask.covers(START + timedelta(hours=14, minutes=20)))
        self.assertTrue(mask.excludes(START + timedelta(hours=14, minutes=40)), "recovery is part of the span")
        self.assertFalse(mask.covers(START + timedelta(hours=14, minutes=41)), "the end is exclusive")
        self.assertFalse(mask.excludes(START + timedelta(hours=9)))

    def test_a_short_burst_of_stairs_is_not_excluded(self) -> None:
        series = hand_series(with_run(150.0, 14.0, 5))
        mask = build_exercise_mask(series)
        self.assertEqual(mask.spans, ())
        self.assertFalse(mask.detected)
        self.assertEqual(mask.minutes_excluded, 0)
        self.assertEqual(mask.fraction_excluded, 0.0)

    def test_the_sustained_floor_is_measured_in_the_data_s_own_epochs(self) -> None:
        # Five-minute data: six readings is 30 minutes of effort, and must not be judged
        # as though it were six minutes.
        points = plateau((0, 24, 60.0), step_minutes=5)
        start = START + timedelta(hours=10)
        spiked = [
            (stamp, 150.0 if start <= stamp < start + timedelta(minutes=30) else 60.0)
            for stamp, _ in points
        ]
        series = hand_series(spiked, epoch_seconds=300.0)
        mask = build_exercise_mask(series)
        self.assertEqual(len(mask.spans), 1)
        # 10:00-10:25 of candidates + the last epoch + ten minutes of cooldown.
        self.assertEqual(mask.spans[0].minutes, 40.0)

    def test_a_run_split_by_a_data_drop_is_still_one_span(self) -> None:
        points = with_run(150.0, 14.0, 31)
        gap = START + timedelta(hours=14, minutes=10)
        points = [pair for pair in points if pair[0] != gap]
        series = hand_series(points)
        mask = build_exercise_mask(series)
        self.assertEqual(len(mask.spans), 1)
        self.assertEqual(mask.spans[0].samples, 30)

    def test_two_runs_far_apart_are_two_spans(self) -> None:
        points = day_at_rest()
        for hour, length in ((9.0, 15), (17.0, 20)):
            start = START + timedelta(hours=hour)
            stop = start + timedelta(minutes=length)
            points = [(stamp, 150.0 if start <= stamp < stop else value) for stamp, value in points]
        series = hand_series(points)
        mask = build_exercise_mask(series)
        self.assertEqual(len(mask.spans), 2)
        self.assertEqual(mask.spans[0].minutes, 25.0)
        self.assertEqual(mask.spans[1].minutes, 30.0)
        self.assertEqual(mask.minutes_excluded, 55.0)
        self.assertEqual(mask.peak_bpm, 150.0)

    def test_two_runs_close_together_merge_through_the_cooldown(self) -> None:
        points = day_at_rest()
        for minute_from, minute_to in ((840, 851), (860, 871)):  # 14:00-14:10, 14:20-14:30
            start = START + timedelta(minutes=minute_from)
            stop = START + timedelta(minutes=minute_to)
            points = [(stamp, 150.0 if start <= stamp < stop else value) for stamp, value in points]
        series = hand_series(points)
        merged = build_exercise_mask(series)
        self.assertEqual(len(merged.spans), 1)
        self.assertEqual(merged.spans[0].start, START + timedelta(hours=14))
        self.assertEqual(merged.spans[0].end, START + timedelta(hours=14, minutes=41))

    def test_a_personal_threshold_excludes_only_rates_above_it(self) -> None:
        # A wearer whose resting rate is 100 has a bar at 135: their 120 bpm walk is not
        # "exercise" to this rule, and with the floor-only bar it would be.
        series = hand_series(with_run(120.0, 14.0, 31))
        personal = build_exercise_mask(series, resting_bpm=100.0)
        floor_only = build_exercise_mask(series)
        self.assertEqual(personal.threshold, 135.0)
        self.assertEqual(personal.spans, ())
        self.assertEqual(len(floor_only.spans), 1)
        self.assertEqual(floor_only.threshold, 110.0, "the floor alone lets a 120 bpm walk through")

    def test_the_absolute_floor_can_be_raised_for_a_wearer_who_lives_at_higher_rates(self) -> None:
        series = hand_series(with_run(120.0, 14.0, 31))
        raised = build_exercise_mask(series, absolute_floor_bpm=140.0)
        self.assertEqual(raised.threshold, 140.0)
        self.assertEqual(raised.spans, ())

    def test_the_mask_counts_what_it_removed_so_a_report_can_say_so(self) -> None:
        series = hand_series(with_run(150.0, 14.0, 31))
        mask = build_exercise_mask(series)
        self.assertEqual(mask.measured_samples, 24 * 60)
        # 31 candidate minutes + the last epoch + ten minutes of cooldown: what the mask
        # removes is the effort *and* its recovery, and the count says so.
        self.assertEqual(mask.excluded_samples, 41)
        self.assertAlmostEqual(mask.fraction_excluded, 41 / 1440, places=6)
        self.assertEqual(len(mask.values(series)), 41)
        self.assertEqual(max(mask.values(series)), 150.0)
        self.assertEqual(len(mask.hrv_samples(series)), 1440 - 41)
        self.assertEqual(len(mask.excluded(series.measured_samples)), 41)

    def test_imputed_readings_are_never_evidence_of_effort(self) -> None:
        # A dropout inside the run is filled with an interpolated value; the fill must not
        # be counted as an excluded reading, though the run around it still counts.
        points = with_run(150.0, 14.0, 31)
        hole = START + timedelta(hours=14, minutes=5)
        points = [pair for pair in points if pair[0] != hole]
        series = hand_series(points, impute_seconds=120.0)
        self.assertEqual(series.counts["imputed"], 1)
        mask = build_exercise_mask(series)
        self.assertEqual(mask.excluded_samples, 40)
        self.assertEqual(mask.measured_samples, 1439)

    def test_an_empty_range_masks_nothing_and_divides_by_nothing(self) -> None:
        mask = ExerciseMask(since=START, until=START + timedelta(hours=1), threshold=110.0, epoch_seconds=60.0)
        self.assertFalse(mask.detected)
        self.assertEqual(mask.minutes_excluded, 0)
        self.assertEqual(mask.fraction_excluded, 0.0)
        self.assertIsNone(mask.peak_bpm)
        self.assertFalse(mask.covers(START))

    def test_as_dict_names_the_rule_and_every_number_behind_it(self) -> None:
        series = hand_series(with_run(150.0, 14.0, 31))
        payload = build_exercise_mask(series, resting_bpm=70.0).as_dict()
        self.assertEqual(payload["heuristic"], EXERCISE_HEURISTIC)
        self.assertEqual(payload["unit"], "beats per minute")
        self.assertEqual(payload["reference_bpm"], 70.0)
        self.assertEqual(payload["threshold_bpm"], 110.0)
        self.assertEqual(payload["resting_multiple"], 1.35)
        self.assertEqual(payload["cooldown_minutes"], 10.0)
        self.assertEqual(len(payload["spans"]), 1)
        self.assertEqual(payload["samples_excluded"], 41)
        self.assertEqual(payload["peak_bpm"], 150.0)

    def test_a_resting_rate_is_carried_onto_the_mask_for_the_record(self) -> None:
        series = hand_series(day_at_rest())
        mask = build_exercise_mask(series, resting_bpm=64.0)
        self.assertEqual(mask.reference_bpm, 64.0)
        self.assertIsNone(build_exercise_mask(series).reference_bpm)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
