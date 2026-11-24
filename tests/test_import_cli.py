"""CLI tests for ``fm import`` and its reporting subcommands.

They drive the real command line in-process (``main``) under a scratch ``$HOME``, with
``--fixtures`` replaying the recorded responses, so argument parsing, planning, the
store, watermarks, report files and exit codes are all covered without a network or a
browser.

Exit codes are part of the interface - they are printed in ``fm import --help`` and are
what a scheduler keys on - so every one the fixtures can produce is asserted here.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from test_cli import CliTestCase

from import_support import EXPECTED_ROWS, FIXTURES, write_credentials
from fitbit_monitor.api.catalog import HEALTH_METRICS_SCOPE, SLEEP_SCOPE
from fitbit_monitor.cli.common import EXIT_USAGE
from fitbit_monitor.ingest.report import (
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_PARTIAL_FAILURE,
    EXIT_TOTAL_FAILURE,
    EXIT_WITH_WARNINGS,
)

SINCE, UNTIL = "2026-05-01", "2026-05-14"
TOTAL_ROWS = sum(EXPECTED_ROWS.values())


class ImportCliTest(CliTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.credentials_path = self.home / "data" / "fitbit-monitor" / "credentials.json"
        self.credentials_path.parent.mkdir(parents=True, exist_ok=True)
        write_credentials(self.credentials_path)
        self.reports_dir = self.home / "data" / "fitbit-monitor" / "imports"

    def import_window(self, *extra: str, expect: int | None = None) -> tuple[int, str]:
        return self.fm(
            "import", "--fixtures", str(FIXTURES), "--since", SINCE, "--until", UNTIL, *extra, expect=expect
        )

    # --- planning -----------------------------------------------------------
    def test_health_and_archive_are_the_two_sources_under_other_names(self) -> None:
        """ADR-0002 §9 renamed the sources; the flag accepts both spellings.

        What a script must be able to rely on is that the *answer* is the same object,
        and that the store still says "api" - watermarks are keyed by the stored name.
        """
        plain, text = self.import_window("--dry-run", "--source", "health", expect=EXIT_OK)
        named, api_text = self.import_window("--dry-run", expect=EXIT_OK)
        self.assertEqual(text, api_text)
        self.assertEqual(plain, named)
        export = self.home / "export"
        export.mkdir()
        (export / "hrv.csv").write_text("date,hrvValue,sdnn\n2026-05-01,44.0,30\n")
        _code, text = self.fm("import", "--source", "archive", "--path", str(export), "--json", expect=EXIT_OK)
        self.assertEqual(json.loads(text)["source"], "csv", "the answer names the source it ran, canonically")

    def test_dry_run_costs_nothing(self) -> None:
        code, text = self.import_window("--dry-run", expect=EXIT_OK)
        self.assertIn("heart_rate", text)
        self.assertIn("13", text, "the plan states its request cost")
        self.assertFalse(self.store_path.exists(), "--dry-run must not create or touch the store")

    def test_dry_run_json_is_the_plan(self) -> None:
        _, text = self.import_window("--dry-run", "--json", expect=EXIT_OK)
        plan = json.loads(text)
        self.assertEqual([d["dataset"] for d in plan["datasets"]], sorted(EXPECTED_ROWS))
        self.assertEqual(plan["requests_estimate"], 13)

    def test_unknown_dataset_is_a_usage_error(self) -> None:
        code, text = self.import_window("--dataset", "glucose")
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("glucose", text)

    # --- a real run ---------------------------------------------------------
    def test_a_two_week_import_lands_every_dataset_and_exits_zero(self) -> None:
        code, text = self.import_window(expect=EXIT_OK)
        for dataset in EXPECTED_ROWS:
            self.assertIn(dataset, text)
        self.assertIn(str(TOTAL_ROWS), text, "the summary line totals the new rows")
        self.assertIn("report:", text, "the run says where its report went")

    def test_json_report_matches_the_store(self) -> None:
        _, text = self.import_window("--json", expect=EXIT_OK)
        report = json.loads(text)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["since"], SINCE)
        self.assertEqual(report["totals"]["rows_inserted"], TOTAL_ROWS)
        self.assertEqual({d["dataset"] for d in report["datasets"]}, set(EXPECTED_ROWS))
        self.assertTrue(Path(report["report_path"]).exists())

    def test_running_twice_is_idempotent_and_still_zero(self) -> None:
        self.import_window(expect=EXIT_OK)
        _, text = self.import_window(expect=EXIT_OK)
        self.assertIn("0 new rows", text)

    def test_max_requests_bounds_the_run(self) -> None:
        _, text = self.import_window("--max-requests", "5", "--json")
        report = json.loads(text)
        self.assertLessEqual(report["totals"]["requests"], 6)
        self.assertIn("max-requests", json.dumps(report))

    def test_resume_after_a_partial_run_finishes_the_job(self) -> None:
        self.import_window("--max-requests", "5", expect=None)
        code, text = self.fm("import", "--fixtures", str(FIXTURES), "--until", UNTIL, "--resume", expect=EXIT_OK)
        self.assertIn("--resume", text)
        counts = self.counts()
        for dataset, expected in EXPECTED_ROWS.items():
            self.assertEqual(counts.get(dataset, 0), expected, dataset)

    def test_an_unauthenticated_run_says_what_to_run(self) -> None:
        self.credentials_path.unlink()
        code, text = self.fm("import", "--fixtures", str(FIXTURES))
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("fm auth login", text)
        self.assertFalse(self.store_path.exists())

    def test_the_table_forms_of_the_reporting_commands(self) -> None:
        self.import_window(expect=EXIT_OK)
        _code, text = self.fm("import", "report", expect=EXIT_OK)
        self.assertIn("report:", text)
        self.assertIn("watermark", text)
        for dataset in EXPECTED_ROWS:
            self.assertIn(dataset, text)
        _code, text = self.fm("import", "history", expect=EXIT_OK)
        self.assertIn("DATASET", text)
        self.assertEqual(sum(1 for line in text.splitlines() if " api " in line), 5, "one line per dataset")
        _code, text = self.fm("import", "watermarks", expect=EXIT_OK)
        self.assertIn("LAST DAY", text)
        self.assertIn(UNTIL, text)
        self.assertIn("LAST STATUS", text)
        self.assertEqual(text.count(" ok "), 5, "every dataset's last outcome is on the same line")

    def test_watermarks_show_the_last_status_and_error_per_dataset(self) -> None:
        """A dataset that failed should still be findable after the fact, with its
        reason - 'how far did it get' and 'how did it end' are one question."""
        forbidden = Path(self.home / "forbidden")
        forbidden.mkdir()
        for path in sorted(FIXTURES.glob("*.json")):
            payload = json.loads(path.read_text())
            if "daily-heart-rate-variability" in str(payload.get("request", {}).get("path", "")):
                payload["response"] = {
                    "status": 403,
                    "body": {
                        "error": {
                            "code": 403,
                            "status": "PERMISSION_DENIED",
                            "message": "no permission for hrv",
                        }
                    },
                    "headers": {},
                }
            (forbidden / path.name).write_text(json.dumps(payload))
        self.fm("import", "--fixtures", str(forbidden), "--since", SINCE, "--until", UNTIL, expect=EXIT_PARTIAL_FAILURE)

        _code, text = self.fm("import", "watermarks", "--json", expect=EXIT_OK)
        marks = {m["dataset"]: m for m in json.loads(text)["watermarks"]}
        self.assertEqual(marks["sleep"]["status"], "ok")
        self.assertNotIn("hrv", marks, "a dataset that stored nothing has no watermark - it is in the history")
        _code, text = self.fm("import", "history", "--dataset", "hrv", expect=EXIT_OK)
        self.assertIn("failed", text)
        self.assertIn("HTTP 403", text, "the reason is on the same line as the failure")

    def test_a_big_pull_is_confirmed_or_said_to_be_unattended(self) -> None:
        config = self.home / "config" / "fitbit-monitor" / "config.toml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text("[import]\nconfirm_above_requests = 8\n")
        # No TTY under the test runner: the run proceeds and says it is unattended.
        _code, text = self.import_window()
        self.assertIn("running unattended", text)
        _code, text = self.import_window("--yes")
        self.assertNotIn("running unattended", text)

    # --- afterwards ---------------------------------------------------------
    def test_history_watermarks_and_report(self) -> None:
        self.import_window(expect=EXIT_OK)

        _, text = self.fm("import", "history", "--json", expect=EXIT_OK)
        history = json.loads(text)
        self.assertEqual(history["count"], 5, "one row per dataset")
        self.assertEqual({r["dataset"] for r in history["runs"]}, set(EXPECTED_ROWS))
        self.assertEqual({r["source"] for r in history["runs"]}, {"api"})

        _, text = self.fm("import", "watermarks", "--json", expect=EXIT_OK)
        marks = {mark["dataset"]: mark for mark in json.loads(text)["watermarks"]}
        self.assertEqual(set(marks), set(EXPECTED_ROWS))
        self.assertEqual(marks["hrv"]["last_day"], UNTIL)

        _, text = self.fm("import", "report", "--json", expect=EXIT_OK)
        self.assertEqual(json.loads(text)["totals"]["rows_inserted"], TOTAL_ROWS)

    def test_history_before_any_store_is_said_not_crashed(self) -> None:
        code, text = self.fm("import", "history")
        self.assertIn("no store", text.lower())
        self.assertNotEqual(code, 0)

    # --- permissions and sources -------------------------------------------
    def test_a_scope_the_token_lacks_is_a_skip_with_a_reason(self) -> None:
        write_credentials(self.credentials_path, scopes=(HEALTH_METRICS_SCOPE, SLEEP_SCOPE))
        _, text = self.import_window("--json", expect=EXIT_OK)
        report = json.loads(text)
        self.assertEqual(
            {d["dataset"] for d in report["datasets"]},
            {"heart_rate", "hrv", "sleep", "spo2"},
            "everything the two granted scopes covers, and nothing that needs activity_and_fitness",
        )
        self.assertTrue(
            any("daily_summary" in note and "activity_and_fitness" in note for note in report["notes"]),
            "the reason is printed, not swallowed",
        )

    def test_csv_source_from_a_directory(self) -> None:
        export = self.home / "export"
        export.mkdir()
        (export / "hrv.csv").write_text("date,hrvValue,sdnn\n2026-05-01,44.0,30\n2026-05-02,51.0,33\n2026-05-03,oops,x\n")
        code, text = self.fm("import", "--source", "csv", "--path", str(export), "--json")
        report = json.loads(text)
        self.assertEqual(report["source"], "csv")
        self.assertEqual(report["totals"]["rows_inserted"], 2)
        self.assertEqual(code, EXIT_WITH_WARNINGS, "an unusable row is a warning, not silence")
        _, text = self.fm("import", "watermarks", "--source", "csv", "--json", expect=EXIT_OK)
        self.assertEqual(json.loads(text)["watermarks"][0]["dataset"], "hrv")

    def test_csv_filters(self) -> None:
        export = self.home / "export"
        export.mkdir()
        (export / "hrv.csv").write_text("date,hrvValue\n2026-05-01,44.0\n")
        (export / "sleep.csv").write_text(
            "startTime,minutesAsleep\n2026-05-01T23:05:00.000,425\n"
        )
        code, _ = self.fm("import", "--source", "csv", "--path", str(export), "--file", "hrv.csv", "--json")
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(self.counts(), {"hrv": 1})

    def test_csv_dry_run_lists_files_without_writing(self) -> None:
        export = self.home / "export"
        export.mkdir()
        (export / "HeartRate.csv").write_text("time,heartRate,timestamp\n08:00:00,61,2026-05-01T08:00:00.000\n")
        (export / "StrideLength.csv").write_text("time,timestamp,strideLength\n08:00:00,2026-05-01T08:00:00.000,0.7\n")
        _, text = self.fm("import", "--source", "csv", "--path", str(export), "--dry-run", expect=EXIT_OK)
        self.assertIn("HeartRate.csv", text)
        self.assertIn("heart_rate", text, "a flat file is recognised from its header")
        self.assertIn("StrideLength.csv", text)
        self.assertIn("no parser", text, "a file nobody claimed is said, not swallowed")
        self.assertFalse(self.store_path.exists())

    def test_missing_export_is_a_usage_error(self) -> None:
        self.fm("import", "--source", "csv", "--path", str(self.home / "nope.zip"), expect=EXIT_USAGE)

    # --- documentation of the contract ------------------------------------
    def test_the_help_text_states_the_exit_codes_and_flags(self) -> None:
        import argparse

        from fitbit_monitor.cli import build_parser

        parser = build_parser()
        subparsers = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
        text = subparsers.choices["import"].format_help()
        for marker in ("0 ", "1 ", "3 ", "5 ", "130 "):
            self.assertIn(marker, text, marker)
        for flag in ("--resume", "--max-requests", "--dry-run", "--full-history", "--dataset", "--source"):
            self.assertIn(flag, text, flag)
        self.assertEqual((EXIT_INTERRUPTED, EXIT_TOTAL_FAILURE, EXIT_PARTIAL_FAILURE), (130, 1, 3))

    # --- helpers ------------------------------------------------------------
    def counts(self) -> dict[str, int]:
        from fitbit_monitor.store.writer import SeriesStore

        store = SeriesStore(self.store_path)
        try:
            return {dataset: store.count(dataset) for dataset in store.datasets()}
        finally:
            store.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
