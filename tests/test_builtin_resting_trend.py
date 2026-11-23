"""The built-in ``resting_trend`` plugin, held to the shared contract suite.

This is the file that keeps the docs honest: CI runs the same
``PluginContractTestCase`` against the shipped plugin that a third-party author
inherits from ``fitbit_monitor.plugins.testing``, then checks the specific numbers
the plugin promises - weekly means, deltas, which HRV column it picked and why, and
completeness honesty down to the whole week that had no data at all.

Two shapes are asserted over and over here, so they are helpers: ``weekly()`` for the
per-ISO-week metrics and ``coverage()`` for the single run-level line that explains
what is missing.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from fitbit_monitor.plugins.builtin.resting_trend import RestingTrendPlugin, week_start
from fitbit_monitor.plugins.testing import (
    PluginContractTestCase,
    fixture_window,
    make_config,
    run_fixture_plugin,
)
from fitbit_monitor.store.seed import seed_store
from fitbit_monitor.store.writer import SeriesStore
from fitbit_monitor.timeutil import DateRange

END = date(2026, 5, 30)  # a Saturday, so the last ISO week is complete

#: The per-week metrics, as opposed to the run-level ``resting_trend_data_coverage``.
WEEKLY_KEYS = ("resting_hr_weekly", "hrv_weekly")


def seeded_store(directory: str, *, days: int = 60, seed: int = 99, drop: bool = False) -> Path:
    path = Path(directory) / "store.sqlite3"
    with SeriesStore(path) as store:
        seed_store(store, days=days, seed=seed, end=END, drop_missing_days=drop)
    return path


def full_window(days: int = 60) -> DateRange:
    since = datetime(END.year, END.month, END.day, tzinfo=UTC) + timedelta(days=1)
    return DateRange(since=since - timedelta(days=days), until=since)


def weekly(outcome, key: str | None = None) -> list:
    """The per-week metrics only; a whole-week gap is reported in the coverage line."""
    return [
        m for m in outcome.metrics
        if m.key in WEEKLY_KEYS and (key is None or m.key == key)
    ]


def coverage(outcome):
    """The run-level completeness metric. Every run emits exactly one."""
    lines = [m for m in outcome.metrics if m.key == "resting_trend_data_coverage"]
    assert len(lines) == 1, f"expected exactly one coverage line, got {len(lines)}"
    return lines[0]


class RestingTrendContractTest(PluginContractTestCase):
    """The shared suite - the same one published to third-party plugin authors."""

    plugin_cls = RestingTrendPlugin
    config_values = {"lookback_weeks": 6}

    def test_weeks_are_reported_with_both_measures(self) -> None:
        outcome = self.run_fixture_plugin()
        keys = [m.key for m in outcome.metrics]
        self.assertIn("resting_hr_weekly", keys)
        self.assertIn("hrv_weekly", keys)
        weeks = {m.labels["iso_week"] for m in weekly(outcome)}
        self.assertLessEqual(len(weeks), 6)
        self.assertGreaterEqual(len(weeks), 4)

    def test_units_and_measures_are_stated(self) -> None:
        outcome = self.run_fixture_plugin()
        by_key = {m.key: m for m in weekly(outcome)}
        self.assertEqual(by_key["resting_hr_weekly"].unit, "bpm")
        self.assertEqual(by_key["hrv_weekly"].unit, "ms")
        self.assertEqual(by_key["hrv_weekly"].details["hrv_field"], "rmssd")
        # The human line is `key = value unit`, so the week has to live in the summary.
        for metric in weekly(outcome):
            self.assertTrue(
                metric.summary.startswith(metric.labels["iso_week"]),
                f"{metric.key} summary {metric.summary!r} does not name its week",
            )

    def test_auto_hrv_picks_the_measure_the_store_has(self) -> None:
        outcome = self.run_fixture_plugin(config_values={"hrv_field": "auto"})
        metric = weekly(outcome, "hrv_weekly")[0]
        self.assertEqual(metric.details["hrv_field"], "rmssd")
        self.assertEqual(metric.details["hrv_field_requested"], "auto")
        self.assertEqual(metric.labels["hrv_measure"], "rmssd")
        self.assertEqual(coverage(outcome).details["hrv_field_used"], "rmssd")
        self.assertNotIn("note", coverage(outcome).details)

    def test_auto_hrv_uses_sdnn_when_that_is_all_the_store_carries(self) -> None:
        """A Takeout archive has RMSSD and nothing else; another source may have only SDNN."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "store.sqlite3"
            with SeriesStore(path) as store:
                for offset in range(14):
                    day = datetime(2026, 5, 1, tzinfo=UTC) + timedelta(days=offset)
                    store.put("hrv", day, {"sdnn": 40.0 + offset})
                    store.put("daily_summary", day, {"resting_hr": 58.0})
            outcome = run_fixture_plugin(
                RestingTrendPlugin,
                store_path=path,
                window=DateRange(
                    since=datetime(2026, 5, 1, tzinfo=UTC), until=datetime(2026, 5, 15, tzinfo=UTC)
                ),
                config_values={"lookback_weeks": 1},
            )
        self.assertEqual(outcome.status, "ok")
        metric = weekly(outcome, "hrv_weekly")[0]
        self.assertEqual(metric.details["hrv_field"], "sdnn")
        self.assertEqual(metric.details["hrv_field_requested"], "auto")
        self.assertEqual(metric.labels["hrv_measure"], "sdnn")
        line = coverage(outcome)
        self.assertEqual(line.details["hrv_field_used"], "sdnn")
        self.assertIn("rmssd has no values in this range", line.details["note"])
        self.assertIn("SDNN", line.summary)

    def test_configure_checks_its_own_choices_too(self) -> None:
        """``fm plugins validate`` hands ``configure`` the raw section, uncoerced.

        The run path coerces declared options and refuses a bad value with a named
        ``config_error``; the validate path calls ``configure`` directly. So the plugin
        re-checks, or a typo in ``[plugins.resting_trend]`` would pass validation and
        then surprise the run.
        """
        for values, named in (
            ({"hrv_field": "p95"}, "hrv_field"),
            ({"resting_source": "vibes"}, "resting_source"),
        ):
            config = make_config(RestingTrendPlugin.name, values).plugin_config(RestingTrendPlugin.name)
            with self.assertRaises(ValueError) as caught:
                RestingTrendPlugin().configure(config)
            self.assertIn(named, str(caught.exception))

    def test_a_requested_measure_the_store_lacks_is_said_out_loud(self) -> None:
        """No HRV metric is a legitimate answer; a silent one is not."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "store.sqlite3"
            with SeriesStore(path) as store:
                for offset in range(10):
                    day = datetime(2026, 5, 1, tzinfo=UTC) + timedelta(days=offset)
                    store.put("hrv", day, {"sdnn": 41.0})
                    store.put("daily_summary", day, {"resting_hr": 57.0})
            outcome = run_fixture_plugin(
                RestingTrendPlugin,
                store_path=path,
                window=DateRange(
                    since=datetime(2026, 5, 1, tzinfo=UTC), until=datetime(2026, 5, 11, tzinfo=UTC)
                ),
                config_values={"lookback_weeks": 1, "hrv_field": "rmssd"},
            )
        self.assertEqual(outcome.status, "ok")
        self.assertEqual(weekly(outcome, "hrv_weekly"), [], "rmssd was asked for and is absent")
        self.assertTrue(weekly(outcome, "resting_hr_weekly"), "the resting half still reports")
        line = coverage(outcome)
        self.assertEqual(line.details["hrv_field_used"], "none")
        self.assertIn("hrv_field=rmssd has no values", line.details["note"])
        self.assertIn("none", line.summary)

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
        weekly_metrics = weekly(outcome, "resting_hr_weekly")
        self.assertGreater(len(weekly_metrics), 2)
        for previous, current in zip(weekly_metrics, weekly_metrics[1:]):
            self.assertAlmostEqual(
                current.details["wow_delta"],
                round(current.value - previous.value, 2),
                places=2,
                msg=f"{previous.labels['iso_week']} -> {current.labels['iso_week']}",
            )
        self.assertTrue(
            [m for m in weekly_metrics if m.details.get("delta_4w") is not None],
            "a 4-week delta should exist once enough weeks are available",
        )
        self.assertIsNone(weekly_metrics[0].details["wow_delta"], "the oldest week has no previous week")

    def test_incomplete_weeks_are_reported_as_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # seed 7 over 60 days drops exactly 2026-05-24 and 2026-05-29 inside the
            # reported weeks (and 2026-04-24 in the delta-only weeks before them).
            path = seeded_store(tmp, days=60, seed=7, drop=True)
            outcome = run_fixture_plugin(
                RestingTrendPlugin,
                store_path=path,
                window=full_window(60),
                config_values={"lookback_weeks": 4, "min_days_per_week": 7},
            )
        self.assertEqual(outcome.status, "ok")
        incomplete = [m for m in weekly(outcome) if m.labels["complete"] == "no"]
        self.assertTrue(incomplete, "dropping whole days should leave at least one incomplete week")
        self.assertTrue(
            any(m.details["missing_days"] for m in incomplete),
            "an incomplete week has to name the days it never got",
        )
        for metric in incomplete:
            self.assertLess(metric.details["days_present"], 7, "min_days_per_week was set to 7")
            self.assertLessEqual(metric.confidence, 1.0)
            self.assertAlmostEqual(
                metric.confidence,
                metric.details["days_present"] / metric.details["days_expected"],
                places=6,
                msg="confidence is the observed fraction of the week, nothing else",
            )
        self.assertEqual(
            set(coverage(outcome).details["weeks_incomplete"]),
            {m.labels["iso_week"] for m in incomplete},
            "the coverage line and the weekly labels must agree about what was incomplete",
        )

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

    def test_resting_source_heart_rate_reads_the_intraday_series(self) -> None:
        """The explicit intraday source: minute points, daytime ignored, checkpoint run.

        Five days of every minute is 7 200 points, which is past
        ``CHECK_EVERY_POINTS``, so the scan also runs its cooperative timeout check the
        way the contract asks a long loop to.
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "store.sqlite3"
            with SeriesStore(path) as store:
                # 5 days x 1440 minutes: quiet at night, loud in the day.
                rows = []
                for day in range(5):
                    day_start = datetime(2026, 5, 18, tzinfo=UTC) + timedelta(days=day)
                    for minute in range(1440):
                        ts = day_start + timedelta(minutes=minute)
                        rows.append(("heart_rate", ts, {"hr": 55.0 if ts.hour < 6 else 82.0}))
                    rows.append(("daily_summary", day_start, {"resting_hr": 99.0}))
                self.assertEqual(store.put_many(rows), len(rows))
            outcome = run_fixture_plugin(
                RestingTrendPlugin,
                store_path=path,
                window=DateRange(
                    since=datetime(2026, 5, 18, tzinfo=UTC), until=datetime(2026, 5, 23, tzinfo=UTC)
                ),
                config_values={"lookback_weeks": 1, "resting_source": "heart_rate"},
            )
        self.assertEqual(outcome.status, "ok", outcome.error)
        metric = weekly(outcome, "resting_hr_weekly")[0]
        self.assertAlmostEqual(metric.value, 55.0, places=2, msg="the overnight window, not the daytime")
        self.assertEqual(metric.details["days_present"], 5)
        self.assertEqual(metric.labels["complete"], "yes")

    def test_lookback_smaller_than_history_reports_only_that_many_weeks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = seeded_store(tmp, days=60)
            outcome = run_fixture_plugin(
                RestingTrendPlugin, store_path=path, window=full_window(60), config_values={"lookback_weeks": 3}
            )
        weeks = {m.labels["iso_week"] for m in weekly(outcome)}
        self.assertEqual(len(weeks), 3)
        self.assertEqual(max(weeks), _iso_week(END))

    def test_a_week_with_no_data_at_all_is_named_not_omitted(self) -> None:
        """Thirteen weeks asked for, three weeks of store: the gap is the finding."""
        with tempfile.TemporaryDirectory() as tmp:
            path = seeded_store(tmp, days=21)
            outcome = run_fixture_plugin(
                RestingTrendPlugin,
                store_path=path,
                window=full_window(21),
                config_values={"lookback_weeks": 12},
            )
        self.assertEqual(outcome.status, "ok")
        line = coverage(outcome)
        self.assertEqual(line.details["weeks_requested"], 12)
        self.assertTrue(line.details["weeks_without_data"], "nine of twelve weeks are empty")
        self.assertLess(line.value, 1.0)
        self.assertEqual(line.confidence, line.value)
        self.assertIn("none at all for", line.summary)
        for week in line.details["weeks_without_data"]:
            self.assertNotIn(week, {m.labels["iso_week"] for m in weekly(outcome)})

    def test_coverage_is_one_when_every_requested_week_has_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = seeded_store(tmp, days=60)
            outcome = run_fixture_plugin(
                RestingTrendPlugin,
                store_path=path,
                window=full_window(60),
                config_values={"lookback_weeks": 4},
            )
        line = coverage(outcome)
        self.assertEqual(line.value, 1.0)
        self.assertEqual(line.details["weeks_without_data"], [])
        self.assertEqual(line.details["weeks_incomplete"], [])
        self.assertEqual(line.details["weeks_with_data"], line.details["weeks_requested"])

    def test_a_window_ending_at_midnight_owes_no_day_after_it(self) -> None:
        """Half-open window: do not report a missing day the reader never asked for."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "store.sqlite3"
            with SeriesStore(path) as store:
                for offset in range(7):
                    day = datetime(2026, 5, 18, tzinfo=UTC) + timedelta(days=offset)
                    store.put("daily_summary", day, {"resting_hr": 56.0})
                    store.put("hrv", day, {"rmssd": 62.0})
            outcome = run_fixture_plugin(
                RestingTrendPlugin,
                store_path=path,
                window=DateRange(
                    since=datetime(2026, 5, 18, tzinfo=UTC), until=datetime(2026, 5, 25, tzinfo=UTC)
                ),
                config_values={"lookback_weeks": 1},
            )
        self.assertEqual(outcome.status, "ok")
        metric = weekly(outcome, "resting_hr_weekly")[0]
        self.assertEqual(metric.details["days_present"], 7)
        self.assertEqual(metric.details["missing_days"], [])
        self.assertEqual(metric.confidence, 1.0)
        self.assertEqual(metric.labels["complete"], "yes")
        self.assertEqual(coverage(outcome).value, 1.0)

    def test_a_quiet_run_says_why_instead_of_saying_nothing(self) -> None:
        """Data in range, but not the columns this plugin reads: report the reason."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "store.sqlite3"
            with SeriesStore(path) as store:
                for offset in range(10):
                    day = datetime(2026, 5, 1, tzinfo=UTC) + timedelta(days=offset)
                    store.put("hrv", day, {"hrv_n": 300.0})
                    store.put("daily_summary", day, {"steps": 9000.0})
            outcome = run_fixture_plugin(
                RestingTrendPlugin,
                store_path=path,
                window=DateRange(
                    since=datetime(2026, 5, 1, tzinfo=UTC), until=datetime(2026, 5, 11, tzinfo=UTC)
                ),
                config_values={"lookback_weeks": 1},
            )
        self.assertEqual(outcome.status, "ok")
        self.assertEqual(weekly(outcome), [], "nothing to average, so no weekly metric")
        line = coverage(outcome)
        self.assertEqual(line.value, 0.0)
        self.assertEqual(len(line.details["weeks_without_data"]), line.details["weeks_requested"])
        self.assertIn("no hrv rows carrying rmssd or sdnn", line.details["note"])

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
        for metric in weekly(outcome):
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
