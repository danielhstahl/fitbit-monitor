"""Tests for the import planner and importer - the engine behind ``fm import``.

They run entirely offline against the recorded fixtures in ``tests/fixtures/api``
(see ``scripts/dev/record_fixtures.py``), including the end-to-end assertion the epic
asks for: a two-week import of recorded responses lands exactly the rows the fixtures
contain, twice over.

The failure paths are the point as much as the happy path: a dataset the token cannot
read fails alone, a rate limit stops the run instead of burning the budget, Ctrl-C
leaves the store consistent, and re-running costs no duplicate rows.
"""

from __future__ import annotations

import unittest
from datetime import date

import import_support as fx
from fitbit_monitor.api.catalog import HEALTH_METRICS_SCOPE, SLEEP_SCOPE
from fitbit_monitor.api.http import Response
from fitbit_monitor.errors import UsageError
from fitbit_monitor.ingest.importer import ImportPlanner, Importer, day_window
from fitbit_monitor.ingest.report import (
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_PARTIAL_FAILURE,
    EXIT_TOTAL_FAILURE,
    EXIT_WITH_WARNINGS,
)

WINDOW = dict(since=fx.SINCE, until=fx.UNTIL)


class WrapTransport:
    """Replays the fixtures, with per-path overrides supplied by the test."""

    def __init__(self, override=None, *, interrupt_after: int | None = None) -> None:
        self.inner = fx.replay_transport()
        self.override = override or (lambda path: None)
        self.interrupt_after = interrupt_after
        self.calls = 0

    def request(self, method, url, headers=None, body=None):
        self.calls += 1
        if self.interrupt_after is not None and self.calls > self.interrupt_after:
            raise KeyboardInterrupt()
        path = url.split("?")[0]
        replacement = self.override(path)
        if replacement is not None:
            return replacement
        return self.inner.request(method, url, headers, body)


class DayWindowTest(unittest.TestCase):
    def test_plain_dates_are_inclusive(self) -> None:
        self.assertEqual(day_window("2026-05-01", "2026-05-14"), (date(2026, 5, 1), date(2026, 5, 14)))

    def test_a_bare_until_means_the_end_of_that_day(self) -> None:
        self.assertEqual(day_window("2026-05-01", "2026-05-03"), (date(2026, 5, 1), date(2026, 5, 3)))

    def test_a_timestamp_until_is_exclusive(self) -> None:
        self.assertEqual(day_window("2026-05-01", "2026-05-03T10:00:00"), (date(2026, 5, 1), date(2026, 5, 3)))

    def test_the_default_window_is_recent_days_back_from_today(self) -> None:
        start, stop = day_window(default_days=7)
        self.assertEqual((stop - start).days, 6)

    def test_an_inverted_range_is_refused(self) -> None:
        with self.assertRaises(UsageError) as caught:
            day_window("2026-05-14", "2026-05-01")
        self.assertIn("must not be before", str(caught.exception))
        self.assertEqual(caught.exception.exit_code, 2)


class PlannerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.env = fx.Environment()
        self.addCleanup(self.env.cleanup)
        self.planner = ImportPlanner(self.env.client, self.env.watermarks, self.env.settings)

    def test_the_default_selection_is_everything_the_account_allows(self) -> None:
        plan = self.planner.build(**WINDOW)
        self.assertEqual([d.dataset for d in plan.datasets], ["daily_summary", "heart_rate", "hrv", "sleep", "spo2"])
        self.assertEqual(plan.days, 14)
        self.assertEqual(plan.probe_requests, 1)

    def test_the_cost_of_the_two_week_pull_is_planned_before_it_is_spent(self) -> None:
        # Thirteen requests for two weeks of five datasets: one profile probe, four daily
        # rollups plus a resting-HR list, three heart-rate windows, one HRV list, two sleep
        # pages and one SpO2 list. The legacy per-day endpoints cost 73 for the same
        # window, which is the whole reason ADR-0002 §6 sizes windows instead of days.
        plan = self.planner.build(**WINDOW)
        self.assertEqual(plan.requests, 13)
        self.assertEqual(plan.dataset("daily_summary").requests, 5)
        self.assertEqual(plan.dataset("heart_rate").requests, 3)
        self.assertEqual(plan.dataset("hrv").requests, 1)
        self.assertEqual(plan.dataset("hrv").chunk_days, 14)
        self.assertEqual(len(plan.dataset("hrv").chunks), 1)
        self.assertIn("13", " ".join(plan.describe()))
        self.assertEqual(plan.as_dict()["requests_estimate"], 13)

    def test_scopes_decide_what_is_imported_and_the_reason_is_said(self) -> None:
        env = fx.Environment(scopes=(HEALTH_METRICS_SCOPE, SLEEP_SCOPE))
        self.addCleanup(env.cleanup)
        plan = ImportPlanner(env.client, env.watermarks).build(**WINDOW)
        self.assertEqual([d.dataset for d in plan.datasets], ["heart_rate", "hrv", "sleep", "spo2"])
        self.assertTrue(
            any("daily_summary" in note and "activity_and_fitness" in note for note in plan.notes),
            "the skip names the scope that was missing, on the dataset that needed it",
        )

    def test_an_explicit_dataset_list_bypasses_the_probe_but_not_validation(self) -> None:
        plan = self.planner.build(datasets=["heart_rate"], **WINDOW)
        self.assertEqual([d.dataset for d in plan.datasets], ["heart_rate"])
        with self.assertRaises(UsageError):
            self.planner.build(datasets=["glucose"], **WINDOW)

    def test_chunk_days_can_be_overridden_for_coarser_resume(self) -> None:
        from fitbit_monitor.config import ImportSettings

        env = fx.Environment(settings=ImportSettings(chunk_days={"heart_rate": 7}))
        self.addCleanup(env.cleanup)
        plan = ImportPlanner(env.client, env.watermarks, env.settings).build(**WINDOW)
        heart = plan.dataset("heart_rate")
        self.assertEqual(len(heart.chunks), 2)
        self.assertEqual(heart.chunks[0].label, "2026-05-01..2026-05-07")
        # A 7-day chunk is still two rollups, because the endpoint's page-safe window is
        # six days: the chunk is the unit of *durability*, the window is the unit of the
        # request, and the two do not have to agree.
        self.assertEqual(heart.requests, 4)

    def test_no_since_starts_at_the_recent_default_and_says_so(self) -> None:
        plan = self.planner.build(until=fx.UNTIL)
        heart = plan.dataset("heart_rate")
        self.assertEqual((heart.since - heart.until).days, -6)
        self.assertTrue(any("no watermark" in note for note in heart.notes))

    def test_a_watermark_is_where_the_next_import_starts(self) -> None:
        self.env.watermarks.advance("heart_rate", "api", last_day=date(2026, 5, 12), last_ts=None)
        plan = self.planner.build(until=fx.UNTIL)
        self.assertEqual(plan.dataset("heart_rate").since, date(2026, 5, 12), "the last fetched day is re-read once")

    def test_the_overlap_never_reaches_before_the_requested_window(self) -> None:
        self.env.watermarks.advance("heart_rate", "api", last_day=date(2026, 5, 7), last_ts=None)
        plan = self.planner.build(until=fx.UNTIL)
        self.assertEqual(plan.dataset("heart_rate").since, date(2026, 5, 8), "the window floor wins over the overlap")

    def test_resume_covers_the_whole_original_window_not_just_the_recent_default(self) -> None:
        """A half-finished pull is continued from where it started, even if that is
        months before the default window."""
        self.env.watermarks.advance("daily_summary", "api", last_day=date(2026, 5, 2), last_ts=None)
        plan = self.planner.build(until=fx.UNTIL, resume_from=date(2026, 5, 1))
        summary = plan.dataset("daily_summary")
        self.assertEqual(summary.since, date(2026, 5, 2), "one day of overlap, then everything onwards")
        self.assertEqual(len(summary.chunks), 1, "a 13-day tail is still inside one daily chunk")

    def test_the_overlap_window_re_reads_recent_days_to_catch_late_corrections(self) -> None:
        from fitbit_monitor.config import ImportSettings

        env = fx.Environment(settings=ImportSettings(overlap_days=3))
        self.addCleanup(env.cleanup)
        env.watermarks.advance("hrv", "api", last_day=date(2026, 5, 10), last_ts=None)
        plan = ImportPlanner(env.client, env.watermarks, env.settings).build(until=fx.UNTIL)
        hrv = plan.dataset("hrv")
        self.assertEqual(hrv.since, date(2026, 5, 8), "re-fetches the last three days")
        self.assertTrue(any("late corrections" in note for note in hrv.notes))

    def test_a_dataset_current_through_the_end_is_not_re_read_every_run(self) -> None:
        """The overlap applies when there is something new; a current dataset is free."""
        self.env.watermarks.advance("hrv", "api", last_day=date(2026, 5, 14), last_ts=None)
        plan = self.planner.build(until=fx.UNTIL)
        self.assertTrue(plan.dataset("hrv").skipped, "no requests spent re-reading a finished window")

    def test_overlap_days_zero_restarts_exactly_after_the_watermark(self) -> None:
        from fitbit_monitor.config import ImportSettings

        env = fx.Environment(settings=ImportSettings(overlap_days=0))
        self.addCleanup(env.cleanup)
        env.watermarks.advance("hrv", "api", last_day=date(2026, 5, 10), last_ts=None)
        plan = ImportPlanner(env.client, env.watermarks, env.settings).build(until=fx.UNTIL)
        self.assertEqual(plan.dataset("hrv").since, date(2026, 5, 11))

    def test_a_dataset_already_fetched_through_the_end_is_skipped_not_refetched(self) -> None:
        self.env.watermarks.advance("heart_rate", "api", last_day=date(2026, 5, 14), last_ts=None)
        plan = self.planner.build(until=fx.UNTIL)
        heart = plan.dataset("heart_rate")
        self.assertTrue(heart.skipped)
        self.assertIn("already fetched through 2026-05-14", heart.reason)
        self.assertEqual(plan.active, [d for d in plan.datasets if d.dataset != "heart_rate"])

    def test_full_history_starts_at_the_membership_date(self) -> None:
        plan = self.planner.build(until=fx.UNTIL, full_history=True)
        self.assertEqual(plan.dataset("heart_rate").since, date(2022, 11, 8))

    def test_the_plan_survives_json_round_trip(self) -> None:
        import json

        payload = json.loads(json.dumps(self.planner.build(**WINDOW).as_dict()))
        self.assertEqual(payload["datasets"][0]["dataset"], "daily_summary")


class ImporterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.env = fx.Environment()
        self.addCleanup(self.env.cleanup)
        self.progress = fx.CollectingProgress()

    def run_import(self, *, transport=None, plan_kwargs=None, **kwargs):
        """Build a plan and import it, keeping the environment under test in ``self.env``."""
        if transport is not None:
            self.env = fx.Environment(transport=transport)
            self.addCleanup(self.env.cleanup)
        planner = ImportPlanner(self.env.client, self.env.watermarks, self.env.settings)
        plan = planner.build(**(plan_kwargs or WINDOW))
        importer = Importer(
            client=self.env.client,
            ingest=self.env.ingest,
            watermarks=self.env.watermarks,
            runlog=self.env.runlog,
            settings=self.env.settings,
            progress=self.progress,
        )
        return importer.run(plan, **kwargs)

    # --- the headline end-to-end assertion ---------------------------------
    def test_two_weeks_of_recorded_fixtures_land_the_expected_rows(self) -> None:
        report = self.run_import()
        self.assertEqual(report.status, "ok")
        self.assertEqual(report.exit_code, EXIT_OK)
        for dataset, expected in fx.EXPECTED_ROWS.items():
            self.assertEqual(self.env.store.count(dataset), expected, dataset)
        self.assertEqual(self.env.client.requests, 13)
        self.assertEqual(self.env.store.count(), sum(fx.EXPECTED_ROWS.values()))

    def test_every_dataset_reports_its_range_chunks_and_watermark(self) -> None:
        report = self.run_import()
        for entry in report.datasets:
            with self.subTest(dataset=entry.dataset):
                self.assertEqual(entry.since, fx.SINCE)
                self.assertEqual(entry.until, fx.UNTIL)
                self.assertEqual(entry.status, "ok")
                self.assertEqual(entry.watermark, fx.UNTIL)
                self.assertGreater(entry.rows_inserted, 0)
                self.assertGreater(entry.chunks_fetched, 0)
                self.assertEqual(entry.chunks_fetched, entry.chunks_planned)
                self.assertGreater(entry.bytes_stored, 0)

    def test_a_day_with_no_data_lands_no_rows_and_no_warning(self) -> None:
        """v4 answers an empty period with an empty page, not a 404.

        So the old "day missing from the corpus" case is now "the window came back shorter
        than it was asked for": 13 of the 14 days have heart rate, and nothing about that
        is a warning, because there is nothing to warn about.
        """
        report = self.run_import()
        heart = report.dataset("heart_rate")
        self.assertEqual(heart.status, "ok")
        self.assertEqual(report.exit_code, EXIT_OK)
        self.assertEqual(heart.rows_inserted, 13 * 288, "every day except 2026-05-05")
        self.assertEqual(heart.notes, [])
        self.assertEqual(heart.info, [])

    def test_a_refusal_for_scope_is_a_reported_gap_not_a_failed_import(self):
        """Partial consent: one data type the token cannot see must not fail the dataset.

        Google's rule for a withheld scope is that the app keeps working on the streams it
        was given; a chunk that died over one refused data type would throw away the good
        half of the same response set.
        """
        refused = Response(
            403,
            b'{"error":{"code":403,"status":"PERMISSION_DENIED","message":"no scope","details":'
            b'[{"@type":"type.googleapis.com/google.rpc.ErrorInfo","reason":"MISSING_OAUTH_SCOPE"}]}}',
        )
        report = self.run_import(
            transport=WrapTransport(lambda path: refused if "daily-resting-heart-rate" in path else None)
        )
        summary = report.dataset("daily_summary")
        self.assertEqual(summary.status, "ok", "the four rollups that answered still land")
        self.assertGreater(summary.rows_inserted, 0)
        self.assertTrue(any("MISSING_OAUTH_SCOPE" in note for note in summary.info), summary.info)
        self.assertEqual(self.env.store.count("daily_summary"), fx.EXPECTED_ROWS["daily_summary"])

    def test_re_running_the_same_window_adds_nothing(self) -> None:
        self.run_import()
        before = self.env.counts()
        second = self.run_import()
        self.assertEqual(self.env.counts(), before)
        self.assertEqual(sum(d.rows_inserted for d in second.datasets), 0)
        self.assertTrue(all(d.rows_duplicate > 0 for d in second.datasets))
        self.assertEqual(second.exit_code, EXIT_OK)

    def test_the_second_pass_costs_etags_not_payloads(self) -> None:
        self.run_import()
        self.env.client.etags.save()
        client = fx.make_client(self.env.dir)
        plan = ImportPlanner(client, self.env.watermarks, self.env.settings).build(**WINDOW)
        report = Importer(client=client, ingest=self.env.ingest, watermarks=self.env.watermarks).run(plan)
        self.assertEqual(report.exit_code, EXIT_OK)
        self.assertGreater(client.not_modified, 0, "the recorded ETags turn refetches into 304s")

    def test_progress_is_reported_per_dataset_and_chunk(self) -> None:
        self.run_import()
        self.assertEqual(sorted(self.progress.started), ["daily_summary", "heart_rate", "hrv", "sleep", "spo2"])
        self.assertEqual(len(self.progress.chunks), 8)
        self.assertEqual(self.progress.chunks[0][1], 1)
        self.assertIsNone(self.progress.remaining, "v4 sends no rate-limit ceiling to surface")
        self.assertEqual(len(self.progress.finished), 5)

    def test_watermarks_are_per_dataset_and_move_only_over_fetched_days(self) -> None:
        self.run_import()
        marks = {m.dataset: m.last_day for m in self.env.watermarks.all("api")}
        self.assertEqual(marks["heart_rate"], date(2026, 5, 14))
        self.assertEqual(self.env.watermarks.next_since("heart_rate"), date(2026, 5, 15))

    def test_the_run_is_recorded_in_the_history_table(self) -> None:
        report = self.run_import()
        rows = self.env.runlog.for_run(report.run_id)
        self.assertEqual(len(rows), 5)
        self.assertEqual({r["source"] for r in rows}, {"api"})
        self.assertEqual(self.env.runlog.last_run_id(), report.run_id)

    # --- failure paths ------------------------------------------------------
    def test_a_dataset_the_token_cannot_read_fails_alone(self) -> None:
        forbidden = Response(
            403,
            b'{"error":{"code":403,"status":"PERMISSION_DENIED","message":"You do not have permission for hrv"}}',
        )
        report = self.run_import(
            transport=WrapTransport(lambda path: forbidden if "daily-heart-rate-variability" in path else None)
        )
        self.assertEqual(report.status, "partial")
        self.assertEqual(report.exit_code, EXIT_PARTIAL_FAILURE)
        hrv = report.dataset("hrv")
        self.assertEqual(hrv.status, "failed")
        self.assertIn("PERMISSION_DENIED", hrv.error)
        self.assertEqual(self.env.store.count("heart_rate"), fx.EXPECTED_ROWS["heart_rate"])

    def test_everything_forbidden_is_a_total_failure(self) -> None:
        forbidden = Response(403, b'{"errors":[{"message":"no scope"}]}')
        report = self.run_import(transport=WrapTransport(lambda path: None if "profile" in path else forbidden))
        self.assertEqual(report.status, "failed")
        self.assertEqual(report.exit_code, EXIT_TOTAL_FAILURE)
        self.assertEqual(self.env.store.count(), 0)

    def test_a_rate_limit_stops_the_run_and_says_re_run(self) -> None:
        soon = Response(429, b"", {"Retry-After": "1"})
        report = self.run_import(transport=WrapTransport(lambda path: soon if "heart-rate" in path else None))
        self.assertIn("rate limit", " ".join(report.notes).lower())
        self.assertIn(report.exit_code, (EXIT_PARTIAL_FAILURE, EXIT_TOTAL_FAILURE))
        self.assertEqual(self.env.store.count("heart_rate"), 0, "the aborted dataset stored nothing")

    def test_ctrl_c_leaves_the_completed_chunks_stored(self) -> None:
        # Call 7 of: profile probe (1) + all of daily_summary (5) + heart_rate's first
        # window (1), so the interrupt lands as heart_rate's second window is asked for.
        report = self.run_import(transport=WrapTransport(None, interrupt_after=7))
        self.assertEqual(report.status, "interrupted")
        self.assertEqual(report.exit_code, EXIT_INTERRUPTED)
        stored = self.env.counts()
        self.assertEqual(stored.get("daily_summary"), fx.EXPECTED_ROWS["daily_summary"], "finished datasets survive")
        # The first window is six days, and one of them (2026-05-05) has no data, so a
        # whole window is five days of buckets - not a torn sixth of one.
        self.assertEqual(self.env.store.count("heart_rate"), 5 * 288, "one whole window, no partial window")
        self.assertEqual(self.env.store.count("hrv"), 0, "never started")
        mark = self.env.watermarks.get("heart_rate", "api")
        self.assertEqual(mark.last_day, date(2026, 5, 6), "the watermark says where to resume")
        interrupted = report.dataset("heart_rate")
        self.assertEqual(interrupted.status, "interrupted")
        self.assertIn("interrupted during chunk", " ".join(interrupted.notes))
        self.assertIn("--resume", " ".join(interrupted.notes))

    def test_max_requests_bounds_the_run_and_notes_the_resume(self) -> None:
        report = self.run_import(max_requests=5)
        self.assertIn(report.exit_code, (EXIT_OK, EXIT_PARTIAL_FAILURE, EXIT_WITH_WARNINGS))
        self.assertLessEqual(self.env.client.requests, 6)
        notes = " ".join(" ".join(d.notes) for d in report.datasets)
        self.assertIn("--max-requests", notes)

    def test_a_payload_that_is_not_json_is_quarantined_and_warned(self) -> None:
        junk = Response(200, b"<html>rate limited page</html>")
        report = self.run_import(
            transport=WrapTransport(lambda path: junk if "daily-oxygen-saturation" in path else None)
        )
        spo2 = report.dataset("spo2")
        self.assertEqual(spo2.quarantined, 1, "the one spo2 request answered HTML")
        self.assertEqual(spo2.rows_inserted, 0)
        self.assertEqual(report.exit_code, EXIT_WITH_WARNINGS)
        self.assertIn("not JSON", " ".join(spo2.notes))
        self.assertEqual(self.env.ingest.quarantine_count("spo2"), 1)

    def test_a_normalizer_bug_does_not_lose_the_raw_payload(self) -> None:
        odd = Response(200, b'{"dataPoints": [{"dailyOxygenSaturation": {"date": "not-a-date"}}]}')
        report = self.run_import(
            transport=WrapTransport(lambda path: odd if "daily-oxygen-saturation" in path else None)
        )
        self.assertEqual(report.dataset("spo2").rows_inserted, 0)
        stored = self.env.store.conn.execute("SELECT COUNT(*) AS n FROM raw_payload WHERE dataset='spo2'").fetchone()["n"]
        self.assertEqual(stored, 1, "the identical body is kept once, and it is kept")

    def test_a_run_with_nothing_to_do_is_a_clean_zero(self) -> None:
        for dataset in ("daily_summary", "heart_rate", "hrv", "sleep", "spo2"):
            self.env.watermarks.advance(dataset, "api", last_day=date(2026, 5, 14), last_ts=None)
        report = self.run_import(plan_kwargs={"until": fx.UNTIL})
        self.assertEqual(report.exit_code, EXIT_OK)
        self.assertEqual(report.status, "skipped")
        self.assertTrue(all(entry.status == "skipped" for entry in report.datasets))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
