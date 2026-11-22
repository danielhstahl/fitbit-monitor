"""The built-in ``resting_trend`` plugin, held to the shared contract suite.

This is the file that keeps the docs honest: CI runs the same
``PluginContractTestCase`` against the shipped plugin that a third-party author
inherits from ``fitbit_monitor.plugins.testing``, then checks the specific numbers
the plugin promises - weekly means, deltas, and completeness honesty.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from fitbit_monitor.plugins.builtin.resting_trend import RestingTrendPlugin, week_start
from fitbit_monitor.plugins.testing import PluginContractTestCase, fixture_window, run_fixture_plugin
from fitbit_monitor.store.seed import seed_store
from fitbit_monitor.store.writer import SeriesStore
from fitbit_monitor.timeutil import DateRange

END = date(2026, 5, 30)  # a Saturday, so the last ISO week is complete


def seeded_store(directory: str, *, days: int = 60, seed: int = 99, drop: bool = False) -> Path:
    path = Path(directory) / "store.sqlite3"
    with SeriesStore(path) as store:
        seed_store(store, days=days, seed=seed, end=END, drop_missing_days=drop)
    return path


def full_window(days: int = 60) -> DateRange:
    since = datetime(END.year, END.month, END.day, tzinfo=UTC) + timedelta(days=1)
    return DateRange(since=since - timedelta(days=days), until=since)


class RestingTrendContractTest(PluginContractTestCase):
    """The shared suite - the same one published to third-party plugin authors."""

    plugin_cls = RestingTrendPlugin
    config_values = {"lookback_weeks": 6}

    def test_weeks_are_reported_with_both_measures(self) -> None:
        outcome = self.run_fixture_plugin()
        keys = [m.key for m in outcome.metrics]
        self.assertIn("resting_hr_weekly", keys)
        self.assertIn("hrv_weekly", keys)
        weeks = {m.labels["iso_week"] for m in outcome.metrics}
        self.assertLessEqual(len(weeks), 6)
        self.assertGreaterEqual(len(weeks), 4)

    def test_units_and_measures_are_stated(self) -> None:
        outcome = self.run_fixture_plugin()
        by_key = {m.key: m for m in outcome.metrics}
        self.assertEqual(by_key["resting_hr_weekly"].unit, "bpm")
        self.assertEqual(by_key["hrv_weekly"].unit, "ms")
        self.assertEqual(by_key["hrv_weekly"].details["hrv_field"], "rmssd")

    def test_hrv_field_option_switches_measure(self) -> None:
        outcome = self.run_fixture_plugin(config_values={"hrv_field": "sdnn"})
        metric = next(m for m in outcome.metrics if m.key == "hrv_weekly")
        self.assertEqual(metric.details["hrv_field"], "sdnn")
        sdnn = next(m for m in outcome.metrics if m.key == "hrv_weekly").value
        rmssd = next(
            m for m in self.run_fixture_plugin(config_values={"hrv_field": "rmssd"}).metrics
            if m.key == "hrv_weekly"
        ).value
        self.assertGreater(sdnn, rmssd, "SDNN is normally above RMSSD; the fixture should agree")

    def test_rejects_an_unknown_hrv_field(self) -> None:
        outcome = self.run_fixture_plugin(config_values={"hrv_field": "p95"})
        self.assertEqual(outcome.status, "config_error")
        self.assertIn("hrv_field", outcome.error)

    def test_rejects_an_unknown_resting_source(self) -> None:
        outcome = self.run_fixture_plugin(config_values={"resting_source": "vibes"})
        self.assertEqual(outcome.status, "config_error")

    def test_deltas_are_present_and_correct(self) -> None:
        outcome = self.run_fixture_plugin(config_values={"lookback_weeks": 8})
        weekly = [m for m in outcome.metrics if m.key == "resting_hr_weekly"]
        self.assertGreater(len(weekly), 2)
        for previous, current in zip(weekly, weekly[1:]):
            self.assertAlmostEqual(
                current.details["wow_delta"],
                round(current.value - previous.value, 2),
                places=2,
                msg=f"{previous.labels['iso_week']} -> {current.labels['iso_week']}",
            )
        self.assertTrue(
            [m for m in weekly if m.details.get("delta_4w") is not None],
            "a 4-week delta should exist once enough weeks are available",
        )
        self.assertIsNone(weekly[0].details["wow_delta"], "the oldest week has no previous week")

    def test_incomplete_weeks_are_reported_as_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = seeded_store(tmp, days=30, drop=True)
            outcome = run_fixture_plugin(
                RestingTrendPlugin,
                store_path=path,
                window=full_window(30),
                config_values={"lookback_weeks": 4, "min_days_per_week": 7},
            )
        self.assertEqual(outcome.status, "ok")
        incomplete = [m for m in outcome.metrics if m.labels["complete"] == "no"]
        self.assertTrue(incomplete, "dropping whole days should leave at least one incomplete week")
        for metric in incomplete:
            self.assertLess(metric.details["days_present"], metric.details["days_expected"])
            self.assertLess(metric.confidence, 1.0)
            self.assertTrue(metric.details["missing_days"])

    def test_confidence_never_exceeds_one(self) -> None:
        outcome = self.run_fixture_plugin()
        for metric in outcome.metrics:
            self.assertGreaterEqual(metric.confidence, 0.0)
            self.assertLessEqual(metric.confidence, 1.0)

    def test_resting_hr_falls_back_to_the_overnight_window(self) -> None:
        """With no daily_summary field, resting HR is computed from intraday heart_rate."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "store.sqlite3"
            with SeriesStore(path) as store:
                day = datetime(2026, 5, 20, tzinfo=UTC)
                for minute in range(0, 360, 5):
                    store.put("heart_rate", day + timedelta(minutes=minute), {"hr": 55.0})
                store.put("hrv", day, {"rmssd": 60.0})
            outcome = run_fixture_plugin(
                RestingTrendPlugin,
                store_path=path,
                window=DateRange(since=day - timedelta(days=7), until=day + timedelta(days=1)),
                config_values={"lookback_weeks": 1, "resting_source": "auto"},
            )
        self.assertEqual(outcome.status, "ok")
        hr = next(m for m in outcome.metrics if m.key == "resting_hr_weekly")
        self.assertAlmostEqual(hr.value, 55.0, places=1)
        self.assertEqual(hr.details["days_present"], 1)

    def test_explicit_daily_summary_source_is_not_replaced(self) -> None:
        outcome = self.run_fixture_plugin(config_values={"resting_source": "daily_summary"})
        self.assertEqual(outcome.status, "ok")
        self.assertTrue(any(m.key == "resting_hr_weekly" for m in outcome.metrics))

    def test_lookback_smaller_than_history_reports_only_that_many_weeks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = seeded_store(tmp, days=60)
            outcome = run_fixture_plugin(
                RestingTrendPlugin, store_path=path, window=full_window(60), config_values={"lookback_weeks": 3}
            )
        weeks = {m.labels["iso_week"] for m in outcome.metrics}
        self.assertEqual(len(weeks), 3)
        self.assertEqual(max(weeks), _iso_week(END))

    def test_reads_only_inside_the_requested_window(self) -> None:
        """A narrow window must not quietly analyse the whole store."""
        with tempfile.TemporaryDirectory() as tmp:
            path = seeded_store(tmp, days=90)
            narrow = DateRange(
                since=datetime(END.year, END.month, END.day, tzinfo=UTC) - timedelta(days=6),
                until=datetime(END.year, END.month, END.day, tzinfo=UTC) + timedelta(days=1),
            )
            outcome = run_fixture_plugin(
                RestingTrendPlugin,
                store_path=path,
                window=narrow,
                config_values={"lookback_weeks": 1, "min_days_per_week": 4},
            )
        self.assertEqual(outcome.status, "ok")
        for metric in outcome.metrics:
            self.assertLessEqual(metric.details["days_present"], 7)


class WeekMathTest(unittest.TestCase):
    def test_week_start_is_the_monday(self) -> None:
        self.assertEqual(week_start(date(2026, 5, 3)), date(2026, 4, 27))
        self.assertEqual(week_start(date(2026, 4, 27)), date(2026, 4, 27))

    def test_fixture_window_matches_the_fixture(self) -> None:
        for days in (1, 14, 45):
            window = fixture_window(days=days)
            self.assertEqual((window.until - window.since).days, days)


def _iso_week(day: date) -> str:
    year, week, _ = week_start(day).isocalendar()
    return f"{year}-W{week:02d}"


def _week_before(label: str) -> str:
    year, _, rest = label.partition("-W")
    start = datetime.fromisocalendar(int(year), int(rest), 1).date()
    return _iso_week(start - timedelta(days=7))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
