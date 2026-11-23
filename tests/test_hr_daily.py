"""Per-local-day assembly, and the day's clock being the wearer's rather than UTC's.

The pattern stored here repeats every local day - asleep 23:00 to 03:00, a quiet desk hour
at 09:00, a ten-minute run at 22:00 - so each day's expected windows can be stated exactly,
including the one that straddles midnight and the previous night that has to be excluded from
the resting baseline. The DST cases are what a UTC-based day silently gets wrong.

``FeatureOptions`` carries the zone, so a test that wants local days says so once, in the
options, rather than in the shape of every call.
"""

from __future__ import annotations

import unittest
from datetime import UTC, date, datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

from fitbit_monitor.hr import (
    DayFeatures,
    FeatureOptions,
    anchor_window,
    daily_features,
    day_features,
    day_window,
)
from fitbit_monitor.store.seed import seed_store
from hr_support import HrStoreTestCase, START, plateau

NY = ZoneInfo("America/New_York")
DAY = timedelta(days=1)
RUN_MINUTES = 10


def night_day(day: datetime, *, step_minutes: int = 1) -> list[tuple]:
    """One local day: night across midnight, a quiet hour at 09:00, a run at 22:00."""
    points = plateau(
        (0, 3, 52.0), (3, 7, 70.0), (7, 9, 82.0), (9, 10, 65.0),
        (10, 22, 82.0), (22, 23, 82.0), (23, 24, 52.0),
        day=day, step_minutes=step_minutes,
    )
    run_start = day + timedelta(hours=22)
    run_stop = run_start + timedelta(minutes=RUN_MINUTES)
    return [(stamp, 150.0 if run_start <= stamp < run_stop else value) for stamp, value in points]


class DayShapeTest(unittest.TestCase):
    """Local days, their length, and where the overnight anchor sits."""

    def test_a_local_day_is_half_open_utc_bounds(self) -> None:
        start, end = day_window(date(2026, 5, 1))
        self.assertEqual(start, datetime(2026, 5, 1, tzinfo=UTC))
        self.assertEqual(end, datetime(2026, 5, 2, tzinfo=UTC))

    def test_a_local_day_in_an_offset_zone_is_not_a_utc_day(self) -> None:
        start, end = day_window(date(2026, 5, 10), FeatureOptions(tz=NY))
        self.assertEqual(start, datetime(2026, 5, 10, 4, tzinfo=UTC))
        self.assertEqual(end, datetime(2026, 5, 11, 4, tzinfo=UTC))

    def test_spring_forward_day_is_twenty_three_hours_long(self) -> None:
        start, end = day_window(date(2026, 3, 8), FeatureOptions(tz=NY))
        self.assertEqual(end - start, timedelta(hours=23))

    def test_fall_back_day_is_twenty_five_hours_long(self) -> None:
        start, end = day_window(date(2026, 11, 1), FeatureOptions(tz=NY))
        self.assertEqual(end - start, timedelta(hours=25))

    def test_the_overnight_anchor_runs_evening_to_next_noon(self) -> None:
        start, end = anchor_window(date(2026, 5, 1))
        self.assertEqual(start, START + timedelta(hours=18))
        self.assertEqual(end, START + DAY + timedelta(hours=12))
        self.assertEqual(end - start, timedelta(hours=18))
        self.assertLess(end - start, timedelta(hours=30), "a night is not allowed to become a day")
        self.assertEqual(start.astimezone(UTC).date(), start.date())
        self.assertEqual(end.astimezone(UTC).date(), start.date() + timedelta(days=1), "it crosses midnight")

    def test_the_anchor_hours_are_the_caller_s_to_move(self) -> None:
        start, end = anchor_window(date(2026, 5, 1), FeatureOptions(evening_hour=21, morning_hour=9))
        self.assertEqual(start, START + timedelta(hours=21))
        self.assertEqual(end, START + DAY + timedelta(hours=9))

    def test_the_two_sustained_floors_are_different_numbers(self) -> None:
        # Eight minutes of elevation is exercise; thirty quiet minutes is a resting
        # baseline. One shared constant once made the day's exercise spans require half an
        # hour of effort, so the difference is pinned here as well as named apart.
        options = FeatureOptions()
        self.assertEqual(options.exercise_sustained_minutes, 8.0)
        self.assertEqual(options.rest_sustained_minutes, 30.0)
        self.assertEqual(options.exercise_cooldown_minutes, 10.0)
        self.assertEqual(options.min_intervals, 8)

    def test_the_previous_day_s_anchor_is_the_previous_day_s_evening(self) -> None:
        today = anchor_window(date(2026, 5, 1))
        previous = anchor_window(date(2026, 4, 30))
        self.assertEqual(previous[1], today[0] - timedelta(hours=6), "nights do not overlap; noon is the seam")
        self.assertLess(previous[0], today[0])

    def test_options_refuse_an_unknown_hrv_window(self) -> None:
        with self.assertRaises(ValueError):
            FeatureOptions(hrv_window="whenever")

    def test_options_serialise_their_names_for_the_record(self) -> None:
        payload = FeatureOptions(tz=NY).as_dict()
        self.assertEqual(payload["timezone"], "America/New_York")
        self.assertEqual(payload["min_intervals"], 8)
        self.assertEqual(payload["hrv_window"], "sleep_first")
        self.assertEqual(payload["plausible_band_bpm"], [25.0, 220.0])
        self.assertEqual(payload["overnight_anchor_local"], "18:00-12:00")
        self.assertEqual(payload["exercise_absolute_floor_bpm"], 110.0)


class DayFeaturesTest(HrStoreTestCase):
    """One row per local day, with the numbers a report asks for."""

    #: Smoothing is a denoiser, not an edge-finder: a centred mean cannot reach the plateau
    #: value until it is fully inside the plateau, so with the default 15-minute width every
    #: detected edge sits about six minutes inside the true one. The exact-edge assertions
    #: below switch it off; :meth:`default_smoothing_sits_the_edges_inside_the_plateau` keeps
    #: it on, because that inset is real behaviour and worth pinning.
    EXACT = FeatureOptions(sleep_smoothing_minutes=0.0, rest_smoothing_minutes=0.0)

    def setUp(self) -> None:
        super().setUp()
        for day in (START - DAY, START, START + DAY):
            self.put(night_day(day))
        self.read = self.reader()

    def features(self, day: date = date(2026, 5, 1), **kwargs) -> DayFeatures:
        kwargs.setdefault("options", self.EXACT)
        return day_features(self.read, day, **kwargs)

    def test_the_day_reports_the_sleep_that_straddles_midnight(self) -> None:
        features = self.features()
        self.assertEqual(features.day, date(2026, 5, 1))
        self.assertTrue(features.has_data)
        self.assertEqual(features.series.span_seconds, 1440 * 60)
        self.assertWindow(features.sleep, START + timedelta(hours=23), START + DAY + timedelta(hours=3))
        self.assertEqual(features.sleep.minutes, 240.0)
        self.assertEqual(features.sleep.mean_bpm, 52.0)
        self.assertEqual(features.sleep.kind, "sleep")
        self.assertEqual(features.sleep.detail["anchored"][1].startswith("2026-05-02T12"), True)

    def test_default_smoothing_sits_the_edges_inside_the_plateau(self) -> None:
        smooth = day_features(self.read, date(2026, 5, 1))
        exact = self.features()
        self.assertGreater(smooth.sleep.start, exact.sleep.start)
        self.assertLess(smooth.sleep.end, exact.sleep.end)
        self.assertLess((smooth.sleep.start - exact.sleep.start).total_seconds(), 15 * 60)
        self.assertEqual(smooth.rest.mean_bpm, exact.rest.mean_bpm, "the level is not the artefact")

    def test_the_previous_night_is_reported_separately_and_excluded_from_rest(self) -> None:
        features = self.features()
        self.assertWindow(
            features.previous_sleep, START - DAY + timedelta(hours=23), START + timedelta(hours=3)
        )
        self.assertEqual(features.rest.start, START + timedelta(hours=9))
        self.assertEqual(features.rest.end, START + timedelta(hours=10))
        self.assertEqual(features.rest.mean_bpm, 65.0)
        self.assertEqual(features.resting_bpm, 65.0)
        self.assertEqual(features.resting_samples, 60)
        self.assertEqual(features.resting_samples_after_exclusion, 60)
        self.assertFalse(features.rest.overlaps(features.previous_sleep.start, features.previous_sleep.end))
        self.assertNotEqual(features.previous_sleep.start, features.sleep.start)

    def test_the_run_is_counted_out_of_the_resting_numbers(self) -> None:
        features = self.features()
        self.assertEqual(len(features.exercise.spans), 1)
        self.assertEqual(features.exercise.threshold, 110.0)
        # Ten minutes of running + ten minutes of cooldown; the span ends one epoch after
        # the last running reading, which is already counted inside those ten minutes.
        self.assertEqual(features.exercise.minutes_excluded, 20.0)
        self.assertEqual(features.resting_samples_after_exclusion, 60)
        self.assertTrue(features.exercise.covers(START + timedelta(hours=22, minutes=5)))
        self.assertFalse(features.rest.covers(START + timedelta(hours=22, minutes=5)))
        self.assertEqual(features.exercise.peak_bpm, 150.0)
        self.assertTrue(features.usable)

    def test_hrv_defaults_to_the_sleep_window_and_says_which_window_it_used(self) -> None:
        features = self.features()
        self.assertEqual(features.hrv_window, "sleep")
        self.assertEqual(features.hrv.sample_count, 240)
        self.assertEqual(features.hrv.interval_count, 239)
        self.assertTrue(features.hrv.emitted, features.hrv.not_emitted_reason)
        self.assertEqual(features.hrv.sdnn_ms, 0.0, "a flat night has no interval variance")
        self.assertEqual(features.hrv.rmssd_ms, 0.0)
        self.assertAlmostEqual(features.hrv.mean_interval_ms, 60_000.0 / 52.0, places=6)

        whole_day = day_features(
            self.read, date(2026, 5, 1),
            options=FeatureOptions(sleep_smoothing_minutes=0.0, rest_smoothing_minutes=0.0, hrv_window="day"),
        )
        self.assertEqual(whole_day.hrv_window, "day")
        self.assertEqual(whole_day.hrv.sample_count, 1440 - 20, "the run and its cooldown are not resting HRV")
        self.assertTrue(whole_day.hrv.emitted)
        self.assertGreater(whole_day.hrv.sdnn_ms, 0.0)

    def test_hrv_falls_back_to_the_resting_window_when_there_is_no_night(self) -> None:
        options = FeatureOptions(sleep_smoothing_minutes=0.0, rest_smoothing_minutes=0.0, hrv_window="rest")
        features = self.features(options=options)
        self.assertEqual(features.hrv_window, "rest")
        self.assertEqual(features.hrv.sample_count, 60)
        self.assertEqual(features.hrv.interval_count, 59)

    def test_a_day_with_no_readings_is_still_a_row_that_says_so(self) -> None:
        features = self.features(date(2026, 7, 1))
        self.assertFalse(features.has_data)
        self.assertFalse(features.usable)
        self.assertIsNone(features.resting_bpm)
        self.assertNotWindow(features.sleep, mentions="no readings overnight")
        self.assertNotWindow(features.previous_sleep, mentions="no readings overnight")
        self.assertEqual(features.hrv.sample_count, 0)
        self.assertFalse(features.hrv.emitted)

    def test_a_day_with_two_readings_is_recorded_as_unusable_rather_than_guessed(self) -> None:
        quiet = START + timedelta(days=21)
        self.put([(quiet, 61.0), (quiet + timedelta(minutes=1), 62.0)])
        features = self.features(quiet.date())
        self.assertTrue(features.has_data)
        self.assertFalse(features.usable)
        self.assertNotWindow(features.rest, mentions="eligible readings")
        self.assertFalse(features.hrv.emitted)
        self.assertIn("need 8", features.hrv.not_emitted_reason or "")
        self.assertEqual(features.hrv_window, "sleep_first:none")

    def test_the_row_serialises_with_units_heuristics_and_reasons(self) -> None:
        payload = self.features().as_dict()
        self.assertEqual(payload["day"], "2026-05-01")
        self.assertEqual(payload["resting_bpm"], 65.0)
        self.assertEqual(payload["span_minutes"], 1440.0)
        self.assertEqual(payload["sleep"]["heuristic"], "lowest-load-overnight-block")
        self.assertEqual(payload["previous_sleep"]["kind"], "sleep")
        self.assertEqual(payload["rest"]["heuristic"], "quiet-hour-sustained-minimum")
        self.assertEqual(payload["exercise"]["spans"][0]["minutes"], 20.0)
        self.assertEqual(payload["hrv"]["min_intervals"], 8)
        self.assertEqual(payload["hrv_window"], "sleep")

    def test_the_same_day_in_a_zone_is_a_different_row(self) -> None:
        utc_row = day_features(self.read, date(2026, 5, 1))
        ny_row = day_features(self.read, date(2026, 5, 1), options=FeatureOptions(tz=NY))
        self.assertEqual(ny_row.timezone, "America/New_York")
        self.assertNotEqual(utc_row.series.since, ny_row.series.since)
        self.assertEqual(ny_row.series.since, datetime(2026, 5, 1, 4, tzinfo=UTC))

    def test_daily_features_walks_the_days_it_is_given(self) -> None:
        rows = daily_features(
            self.read, START - DAY, START + 2 * DAY,
            days=[date(2026, 4, 30), date(2026, 5, 1), date(2026, 5, 2)],
        )
        self.assertEqual([row.day for row in rows], [date(2026, 4, 30), date(2026, 5, 1), date(2026, 5, 2)])
        self.assertTrue(all(row.has_data for row in rows))
        self.assertTrue(all(row.usable for row in rows))

    def test_daily_features_without_days_reports_every_local_day_touched(self) -> None:
        rows = daily_features(self.read, START - DAY, START + 2 * DAY)
        self.assertEqual([row.day for row in rows], [date(2026, 4, 30), date(2026, 5, 1), date(2026, 5, 2)])

    def test_daily_features_honours_the_zone_in_the_options(self) -> None:
        rows = daily_features(self.read, START - DAY, START + 2 * DAY, options=FeatureOptions(tz=NY))
        self.assertEqual(rows[0].day, date(2026, 4, 29), "23:00 UTC on the 29th is already the 30th locally")
        self.assertEqual({row.timezone for row in rows}, {"America/New_York"})

    def test_daily_features_reads_the_store_once_for_the_whole_range(self) -> None:
        with mock.patch.object(self.read, "query", wraps=self.read.query) as reads:
            rows = daily_features(self.read, START - DAY, START + 2 * DAY)
        self.assertEqual(len(rows), 3)
        self.assertEqual(reads.call_count, 1, "a 90-day scan must not be 90 queries")


class SeededCorpusTest(HrStoreTestCase):
    """The library has to run end to end on the synthetic corpus without inventing anything."""

    def setUp(self) -> None:
        super().setUp()
        self.plan = seed_store(self.writer, days=45, seed=11, end=date(2026, 5, 31))
        self.rows = daily_features(
            self.reader(),
            datetime.combine(self.plan.start, datetime.min.time(), tzinfo=UTC),
            datetime.combine(self.plan.end + timedelta(days=1), datetime.min.time(), tzinfo=UTC),
        )

    def test_every_local_day_of_the_corpus_gets_exactly_one_row(self) -> None:
        self.assertEqual(len(self.rows), self.plan.days)
        self.assertEqual([row.day for row in self.rows], sorted(row.day for row in self.rows))
        self.assertEqual(len({row.day for row in self.rows}), len(self.rows))

    def test_most_days_measure_enough_to_report_and_the_others_say_why_they_do_not(self) -> None:
        usable = [row for row in self.rows if row.usable]
        self.assertGreaterEqual(len(usable), 35, "the generator drops a few whole days")
        for row in self.rows:
            if row.resting_bpm is not None:
                # The generator's nightly baseline is built around 57-65 bpm; a resting
                # window far outside that band means the rule stopped finding the quiet
                # hour, not that the wearer changed.
                self.assertTrue(45.0 <= row.resting_bpm <= 90.0, f"{row.day}: {row.resting_bpm}")
                self.assertGreaterEqual(row.resting_samples_after_exclusion, 2)
            else:
                self.assertTrue(row.rest.not_detected_reason, row.day)

    def test_windows_are_bounded_and_rest_is_never_sleep(self) -> None:
        for row in self.rows:
            if row.sleep.detected:
                self.assertGreaterEqual(row.sleep.minutes, 45.0, row.day)
                self.assertLessEqual(row.sleep.minutes, 10 * 60.0, row.day)
            if row.rest.detected:
                self.assertGreaterEqual(row.rest.minutes, 15.0, row.day)
                self.assertLessEqual(row.rest.minutes, row.series.span_seconds / 60.0, row.day)
                self.assertFalse(row.rest.overlaps(row.sleep.start, row.sleep.end), row.day)
                self.assertFalse(row.rest.overlaps(row.previous_sleep.start, row.previous_sleep.end), row.day)

    def test_exercise_exclusion_is_a_fraction_and_not_the_whole_day(self) -> None:
        fractions = [row.exercise.fraction_excluded for row in self.rows]
        self.assertTrue(all(0.0 <= fraction <= 1.0 for fraction in fractions))
        self.assertGreater(sum(fractions), 0.0, "the generator puts runs in some days")

    def test_hrv_is_emitted_where_there_are_intervals_and_refuses_where_there_are_not(self) -> None:
        emitted = [row for row in self.rows if row.hrv.emitted]
        self.assertGreaterEqual(len(emitted), 35)
        for row in self.rows:
            features = row.hrv
            if features.emitted:
                self.assertGreaterEqual(features.interval_count, 8)
                self.assertTrue(5.0 < features.sdnn_ms < 200.0, f"{row.day}: {features.sdnn_ms}")
                self.assertTrue(5.0 <= features.rmssd_ms < 200.0, f"{row.day}: {features.rmssd_ms}")
                self.assertTrue(0.0 <= features.pnn50 <= 1.0, row.day)
                self.assertGreaterEqual(features.irregularity_index, 0.0)
                self.assertAlmostEqual(features.mean_bpm, 60_000.0 / features.mean_interval_ms, places=6)
            else:
                self.assertTrue(features.not_emitted_reason, row.day)
                self.assertIsNone(features.sdnn_ms)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
