"""Tests for ingest: dedup, quarantine, watermarks, run log, and the report contract.

These are the guarantees that make "just run it again" the correct answer, so they are
tested directly rather than through the CLI:

* an identical payload is kept once and counted as a duplicate;
* a row that is already there is an upsert, and an update to a *different field* of the
  same instant merges instead of overwriting;
* anything unusable is quarantined with a reason and counted;
* a chunk commits whole, so a rollback drops exactly the unfinished chunk;
* watermarks only move forward, and per source;
* the exit code distinguishes success, success-with-warnings, partial and total failure.
"""

from __future__ import annotations

import json
import random
import sqlite3
import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from fitbit_monitor.errors import StoreError
from fitbit_monitor.ingest.pipeline import Ingest, IngestStats
from fitbit_monitor.ingest.report import (
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_PARTIAL_FAILURE,
    EXIT_TOTAL_FAILURE,
    EXIT_WITH_WARNINGS,
    DatasetReport,
    ImportReport,
    new_run_id,
    summarise_runs,
)
from fitbit_monitor.ingest.runlog import DatasetRun, RunLog
from fitbit_monitor.store import schema
from fitbit_monitor.store.watermarks import Watermarks
from fitbit_monitor.store.writer import SeriesStore, write_lock

T0 = datetime(2026, 5, 1, tzinfo=UTC)
REPO = Path(__file__).resolve().parents[1]


class IngestTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SeriesStore(Path(self.tmp.name) / "store.sqlite3")
        self.addCleanup(self.store.close)
        self.ingest = Ingest(self.store)

    def payload(self, body="{}", rows=None, dataset="hrv", source_id="src", **kwargs):
        return self.ingest.payload(dataset, source_id, body, rows, **kwargs)


class RawRetentionTest(IngestTestCase):
    def test_identical_payload_is_stored_once(self) -> None:
        first = self.payload('{"rmssd": 40}', [(T0, {"rmssd": 40.0})])
        second = self.payload('{"rmssd": 40}', [(T0, {"rmssd": 40.0})])
        self.assertEqual((first.payloads, first.bytes_stored > 0), (1, True))
        self.assertEqual((second.payloads, second.payloads_duplicate), (0, 1))
        self.assertEqual(second.bytes_stored, 0)
        rows = self.store.conn.execute("SELECT COUNT(*) AS n FROM raw_payload").fetchone()["n"]
        self.assertEqual(rows, 1)

    def test_different_payloads_are_both_kept(self) -> None:
        self.payload('{"a":1}', [])
        self.payload('{"a":2}', [])
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) AS n FROM raw_payload").fetchone()["n"], 2)

    def test_unparseable_body_is_quarantined_and_reported(self) -> None:
        stats = self.payload("<html>not json</html>", None)
        self.assertEqual((stats.payloads, stats.payloads_quarantined), (1, 1))
        self.assertEqual(self.ingest.quarantine_count("hrv"), 1)
        self.assertIn("not valid JSON", self.ingest.recent_quarantined()[0]["reason"])

    def test_quarantine_keeps_the_body_for_a_later_fix(self) -> None:
        self.payload("{oops", None, source_id="/1/user/-/hrv/date/2026-05-01/7d.json")
        entry = self.ingest.recent_quarantined()[0]
        self.assertEqual(entry["source_id"], "/1/user/-/hrv/date/2026-05-01/7d.json")


class WriteLockTest(unittest.TestCase):
    def test_a_second_writer_is_refused_instead_of_stalling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "store.sqlite3"
            SeriesStore(path).close()
            with write_lock(path, what="an fm import"):
                with self.assertRaises(StoreError) as caught:
                    with write_lock(path, what="an fm import"):
                        self.fail("the second writer should not have got in")
                self.assertIn("already writing", str(caught.exception))
                self.assertIn("--db", str(caught.exception.hint))
            with write_lock(path):
                pass  # released with the block, so the next import is fine

    def test_the_lock_dies_with_the_process(self) -> None:
        """A crashed import must not leave the store locked (which is why this is no
        pid file and no cleanup step)."""
        import subprocess
        import sys

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "store.sqlite3"
            SeriesStore(path).close()
            script = (
                "import sys; sys.path.insert(0, 'src');"
                "from fitbit_monitor.store.writer import write_lock;"
                "write_lock(sys.argv[1]).__enter__(); sys.exit(0)"
            )
            subprocess.run([sys.executable, "-c", script, str(path)], check=True, cwd=REPO)
            with write_lock(path):
                pass


class RowUpsertTest(IngestTestCase):
    def test_rows_are_counted_as_insert_then_duplicate(self) -> None:
        rows = [(T0, {"rmssd": 40.0}), (T0 + timedelta(days=1), {"rmssd": 42.0})]
        self.assertEqual(self.ingest.rows("hrv", rows).rows_inserted, 2)
        second = self.ingest.rows("hrv", rows)
        self.assertEqual((second.rows_inserted, second.rows_duplicate), (0, 2))
        self.assertEqual(self.store.count("hrv"), 2)

    def test_a_second_writer_adds_fields_instead_of_erasing_them(self) -> None:
        self.ingest.rows("daily_summary", [(T0, {"steps": 9000.0})])
        self.ingest.rows("daily_summary", [(T0, {"resting_hr": 55.0})])
        row = self.store.conn.execute("SELECT payload FROM series WHERE dataset='daily_summary'").fetchone()
        self.assertEqual(json.loads(row["payload"]), {"steps": 9000.0, "resting_hr": 55.0})

    def test_the_same_field_twice_is_last_write_wins(self) -> None:
        self.ingest.rows("hrv", [(T0, {"rmssd": 40.0})])
        self.ingest.rows("hrv", [(T0, {"rmssd": 41.0})])
        values = json.loads(self.store.conn.execute("SELECT payload FROM series").fetchone()["payload"])
        self.assertEqual(values["rmssd"], 41.0)

    def test_bad_rows_are_quarantined_not_silently_dropped(self) -> None:
        stats = self.ingest.rows(
            "hrv",
            [
                (T0, {"rmssd": 40.0}),
                (T0.replace(tzinfo=None), {"rmssd": 39.0}),  # naive: a bug, not a guess
                (T0 + timedelta(days=2), {"rmssd": "wide"}),
                (T0 + timedelta(days=3), {}),
                ("nope", {"rmssd": 1.0}),
            ],
        )
        self.assertEqual(stats.rows_inserted, 1)
        self.assertEqual(stats.payloads_quarantined, 4)
        self.assertEqual(len(self.ingest.recent_quarantined()), 4)
        reasons = " ".join(entry["reason"] for entry in self.ingest.recent_quarantined())
        self.assertIn("timezone-aware", reasons)
        self.assertIn("not numeric", reasons)
        self.assertIn("no values", reasons)


class OverlappingRangeTest(IngestTestCase):
    """The idempotency property, stated the way the storage ticket asks for it:
    importing a set of overlapping ranges must leave exactly what one clean import of
    the whole span leaves - regardless of order, repetition or overlap."""

    def _write(self, start_day: int, days: int) -> None:
        base = date(2026, 4, 1)
        rows = []
        for offset in range(days):
            index = start_day + offset
            day = datetime.combine(base + timedelta(days=index), datetime.min.time(), tzinfo=UTC)
            rows.append((day, {"rmssd": 40.0, "sdnn": 30.0 + index}))
        self.ingest.rows("hrv", rows, source_id=f"chunk-{start_day}")

    def test_random_overlapping_imports_equal_a_single_clean_import(self) -> None:
        """Random *covering*: chunks are random in size and overlap each other, but
        together they cover the span, so the only question left is whether the store
        ends up exactly as if the span had been imported once."""
        rng = random.Random(20260514)
        span = 40
        covered = 0
        plan = []
        while covered < span:
            start = rng.randint(max(0, covered - 6), covered)
            length = min(rng.randint(3, 12), span - start)
            plan.append((start, length))
            covered = max(covered, start + length)
        self.assertGreater(len(plan), 3, "the plan should actually overlap itself")
        for start, length in plan:
            self._write(start, length)

        with tempfile.TemporaryDirectory() as tmp:
            clean = SeriesStore(Path(tmp) / "clean.sqlite3")
            Ingest(clean).rows("hrv", self._clean_rows())
            clean.commit()
            expected = {r["ts_utc"]: json.loads(r["payload"]) for r in clean.conn.execute("SELECT ts_utc, payload FROM series")}
            clean.close()

        actual = {r["ts_utc"]: json.loads(r["payload"]) for r in self.store.conn.execute("SELECT ts_utc, payload FROM series")}
        self.assertEqual(set(actual), set(expected), "the same instants, no more and no fewer")
        self.assertEqual(actual, expected, "and the same values a single import would have written")

    def test_a_second_report_of_the_same_instants_counts_duplicates_not_rows(self) -> None:
        self._write(0, 10)
        again = self._write(5, 10)
        self.assertIsNone(again)
        stored = self.store.conn.execute("SELECT COUNT(*) AS n FROM series").fetchone()["n"]
        self.assertEqual(stored, 15)

    def _clean_rows(self):
        base = date(2026, 4, 1)
        out = []
        for offset in range(40):
            day = datetime.combine(base + timedelta(days=offset), datetime.min.time(), tzinfo=UTC)
            out.append((day, {"rmssd": 40.0, "sdnn": 30.0 + offset}))
        return out


class ChunkTransactionTest(IngestTestCase):
    def test_rollback_drops_the_unfinished_chunk_only(self) -> None:
        self.ingest.payload("hrv", "day-1", "{}", [(T0, {"rmssd": 40.0})], commit=False)
        self.ingest.commit()
        self.ingest.payload("hrv", "day-2", "{}", [(T0 + timedelta(days=1), {"rmssd": 41.0})], commit=False)
        self.ingest.rollback()
        self.assertEqual(self.store.count("hrv"), 1)

    def test_stats_add_up_across_chunks(self) -> None:
        total = IngestStats()
        for offset in range(3):
            total = total + self.payload(
                '{"n": %d}' % offset, [(T0 + timedelta(days=offset), {"rmssd": 40.0 + offset})]
            )
        self.assertEqual(total.rows_inserted, 3)
        self.assertEqual(total.payloads, 3)
        self.assertGreater(total.bytes_stored, 0)
        self.assertEqual(total.as_dict()["rows_inserted"], 3)


class WatermarkTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SeriesStore(Path(self.tmp.name) / "store.sqlite3")
        self.addCleanup(self.store.close)
        self.marks = Watermarks(self.store)

    def test_no_watermark_means_no_suggestion(self) -> None:
        self.assertIsNone(self.marks.get("hrv"))
        self.assertIsNone(self.marks.next_since("hrv"))
        self.assertEqual(self.marks.all(), ())

    def test_a_watermark_advances_and_suggests_the_next_start(self) -> None:
        self.marks.advance("hrv", "api", last_day=date(2026, 5, 10), last_ts=T0 + timedelta(days=10))
        self.assertEqual(self.marks.next_since("hrv"), date(2026, 5, 11))
        mark = self.marks.get("hrv")
        self.assertEqual(mark.last_day, date(2026, 5, 10))
        self.assertEqual(mark.as_dict()["last_day"], "2026-05-10")

    def test_re_importing_an_old_window_does_not_move_it_backwards(self) -> None:
        self.marks.advance("hrv", "api", last_day=date(2026, 5, 20), last_ts=T0 + timedelta(days=20))
        self.marks.advance("hrv", "api", last_day=date(2026, 5, 2), last_ts=T0 + timedelta(days=2))
        self.assertEqual(self.marks.get("hrv").last_day, date(2026, 5, 20))

    def test_sources_do_not_share_watermarks(self) -> None:
        self.marks.advance("heart_rate", "csv", last_day=date(2026, 1, 1), last_ts=None)
        self.assertEqual(self.marks.get("heart_rate", "csv").last_day, date(2026, 1, 1))
        self.assertIsNone(self.marks.get("heart_rate", "api"))
        self.assertEqual(len(self.marks.all()), 1)
        self.assertEqual(self.marks.all("api"), ())

    def test_clearing(self) -> None:
        self.marks.advance("hrv", "api", last_day=date(2026, 5, 1), last_ts=None)
        self.marks.advance("sleep", "api", last_day=date(2026, 5, 1), last_ts=None)
        self.assertEqual(self.marks.clear("hrv"), 1)
        self.assertEqual(self.marks.clear(), 1)


class RunLogTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SeriesStore(Path(self.tmp.name) / "store.sqlite3")
        self.addCleanup(self.store.close)
        self.log = RunLog(self.store)

    def record(self, run_id="run-1", dataset="hrv", status="ok", rows=10):
        self.log.record(
            DatasetRun(
                run_id=run_id,
                dataset=dataset,
                source="api",
                since="2026-05-01",
                until="2026-05-14",
                status=status,
                rows_inserted=rows,
                started_at="2026-06-01T00:00:00+00:00",
                finished_at="2026-06-01T00:00:05+00:00",
            )
        )
        self.store.commit()

    def test_history_is_newest_first_and_filterable(self) -> None:
        self.record(run_id="run-1")
        self.record(run_id="run-2", dataset="sleep", status="failed", rows=0)
        self.assertEqual([r["run_id"] for r in self.log.recent()], ["run-2", "run-1"])
        self.assertEqual([r["run_id"] for r in self.log.recent(dataset="hrv")], ["run-1"])
        self.assertEqual(self.log.for_run("run-1")[0]["dataset"], "hrv")
        self.assertEqual(self.log.last_run_id(), "run-2")

    def test_retry_list_names_the_failed_datasets(self) -> None:
        self.record(run_id="run-1", dataset="sleep", status="failed")
        self.record(run_id="run-1", dataset="hrv")
        self.assertEqual(self.log.datasets_needing_retry(), ("sleep",))
        self.record(run_id="run-2")
        self.assertEqual(self.log.datasets_needing_retry(), ())

    def test_record_report_writes_one_row_per_dataset(self) -> None:
        report = ImportReport(run_id="run-9", since="2026-05-01", until="2026-05-14")
        report.add(DatasetReport(dataset="hrv", rows_inserted=5, watermark="2026-05-14"))
        rows = self.log.record_report(report, started_at="2026-06-01T00:00:00+00:00", finished_at="2026-06-01T00:00:01+00:00")
        self.assertEqual(rows[0].dataset, "hrv")
        self.assertEqual(self.log.recent()[0]["watermark"], "2026-05-14")

    def test_empty_log_is_not_an_error(self) -> None:
        self.assertEqual(self.log.recent(), [])
        self.assertIsNone(self.log.last_run_id())
        self.assertEqual(self.log.datasets_needing_retry(), ())


class ReportTest(unittest.TestCase):
    def report(self, statuses, **kwargs):
        report = ImportReport(run_id="run-1", since="2026-05-01", until="2026-05-14", **kwargs)
        for status in statuses:
            name = f"ds{len(report.datasets)}"
            report.add(DatasetReport(dataset=name, status=status))
        return report

    def test_exit_codes_are_distinct_per_outcome(self) -> None:
        self.assertEqual(self.report(["ok"]).exit_code, EXIT_OK)
        self.assertEqual(self.report(["ok", "failed"]).exit_code, EXIT_PARTIAL_FAILURE)
        self.assertEqual(self.report(["failed", "failed"]).exit_code, EXIT_TOTAL_FAILURE)
        self.assertEqual(self.report(["interrupted"]).exit_code, EXIT_INTERRUPTED)
        self.assertEqual(self.report([], status="interrupted").exit_code, EXIT_INTERRUPTED)

    def test_skipped_datasets_are_not_a_failure(self) -> None:
        report = ImportReport(run_id="run-1")
        report.add(DatasetReport(dataset="hrv", status="skipped", info=["already fetched through 2026-05-14"]))
        self.assertEqual(report.exit_code, EXIT_OK)

    def test_warnings_are_success_with_a_signal(self) -> None:
        report = ImportReport(run_id="run-1")
        report.add(DatasetReport(dataset="hrv", status="ok", quarantined=2))
        self.assertEqual(report.exit_code, EXIT_WITH_WARNINGS)
        self.assertIn("quarantined", " ".join(report.warnings))
        other = ImportReport(run_id="run-2")
        other.add(DatasetReport(dataset="hrv", status="ok", notes=["one file had bad rows"]))
        self.assertEqual(other.exit_code, EXIT_WITH_WARNINGS)

    def test_informational_notes_are_not_warnings(self) -> None:
        report = ImportReport(run_id="run-1")
        report.add(DatasetReport(dataset="hrv", info=["/1/user/-/x: no data (HTTP 404)"]))
        self.assertEqual(report.exit_code, EXIT_OK)
        self.assertEqual(report.warnings, [])

    def test_totals_and_duration(self) -> None:
        report = ImportReport(
            run_id="run-1",
            started_at=datetime(2026, 6, 1, tzinfo=UTC),
            finished_at=datetime(2026, 6, 1, 0, 0, 7, tzinfo=UTC),
        )
        report.add(DatasetReport(dataset="a", rows_inserted=3, requests=2))
        report.add(DatasetReport(dataset="b", rows_duplicate=9, requests=4))
        self.assertEqual(report.totals["rows_inserted"], 3)
        self.assertEqual(report.totals["requests"], 6)
        self.assertEqual(report.duration_ms, 7000)
        self.assertEqual(report.totals["duration_ms"], 7000)

    def test_merging_a_retry_into_the_same_line(self) -> None:
        report = ImportReport(run_id="run-1")
        report.add(DatasetReport(dataset="hrv", status="failed", error="HTTP 500", rows_inserted=1))
        report.add(DatasetReport(dataset="hrv", status="ok", rows_inserted=4, watermark="2026-05-07"))
        merged = report.dataset("hrv")
        self.assertEqual(merged.status, "ok")
        self.assertIsNone(merged.error)
        self.assertEqual(merged.rows_inserted, 5)
        self.assertEqual(merged.watermark, "2026-05-07")
        self.assertEqual(report.exit_code, EXIT_OK)

    def test_the_report_is_written_and_can_be_read_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = ImportReport(run_id="import-api-1", since="2026-05-01", until="2026-05-14")
            report.add(DatasetReport(dataset="hrv", rows_inserted=5, watermark="2026-05-14"))
            path = report.write(Path(tmp))
            self.assertTrue(path.exists())
            payload = json.loads(path.read_text())
            self.assertEqual(payload["datasets"][0]["dataset"], "hrv")
            self.assertEqual(payload["report_path"], str(path))
            self.assertEqual(ImportReport.latest(Path(tmp)), path)
            self.assertEqual(ImportReport.read(path)["run_id"], "import-api-1")

    def test_old_reports_are_pruned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            for index in range(5):
                ImportReport(run_id=f"import-0{index}").write(directory, keep=3)
            self.assertEqual(len(list(directory.glob("*.json"))), 3)
            self.assertFalse((directory / "import-00.json").exists())
            self.assertIsNotNone(ImportReport.latest(directory))
            self.assertEqual(
                ImportReport.prune(directory, 1),
                [directory / "import-03.json", directory / "import-02.json"],
            )

    def test_latest_of_nothing_is_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(ImportReport.latest(Path(tmp)))

    def test_run_ids_sort_and_are_unique(self) -> None:
        first, second = new_run_id("import"), new_run_id("import")
        self.assertTrue(first.startswith("import-"))
        self.assertNotEqual(first, second)

    def test_summarise_collapses_per_dataset_rows(self) -> None:
        runs = [
            {"run_id": "a", "dataset": "hrv", "status": "ok", "source": "api", "started_at": "1", "rows_inserted": 2, "requests": 1, "quarantined": 0},
            {"run_id": "a", "dataset": "sleep", "status": "failed", "source": "api", "started_at": "1", "rows_inserted": 0, "requests": 1, "quarantined": 0},
            {"run_id": "b", "dataset": "hrv", "status": "ok", "source": "csv", "started_at": "2", "rows_inserted": 5, "requests": 0, "quarantined": 0},
        ]
        summary = summarise_runs(runs)
        self.assertEqual([entry["run_id"] for entry in summary], ["b", "a"])
        self.assertEqual(summary[1]["status"], "partial")
        self.assertEqual(summary[0]["status"], "ok")
        self.assertEqual(summary[1]["rows_inserted"], 2)

    def test_table_lines_render(self) -> None:
        report = ImportReport(run_id="run-1")
        report.add(DatasetReport(dataset="hrv", status="ok", chunks_planned=2, chunks_fetched=2, watermark="2026-05-07"))
        self.assertIn("hrv", report.table_lines()[0])


class StoreMigrationTest(unittest.TestCase):
    def test_schema_v2_tables_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SeriesStore(Path(tmp) / "store.sqlite3")
            tables = {r["name"] for r in store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            store.close()
        self.assertLessEqual(
            {"dataset_watermark", "dataset_run", "quarantine", "series", "raw_payload", "alerts"}, tables
        )

    def test_a_v1_store_is_migrated_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.sqlite3"
            conn = sqlite3.connect(path)
            schema.migrate_store(conn, target=1)
            conn.execute(
                "INSERT INTO series VALUES ('hrv', '2026-05-01T00:00:00+00:00', '{\"rmssd\":40.0}', NULL)"
            )
            conn.execute(
                "INSERT INTO raw_payload (dataset, source_id, fetched_at, content_hash, body) "
                "VALUES ('hrv', 'legacy', '2026-05-01T00:00:00+00:00', 'deadbeef', '{\"hrv\":[]}')"
            )
            conn.commit()
            conn.close()
            store = SeriesStore(path)
            version = store.conn.execute("SELECT value FROM fm_meta WHERE key='schema_version'").fetchone()[0]
            marks = Watermarks(store)
            marks.advance("hrv", "api", last_day=date(2026, 5, 1), last_ts=T0)
            survived = store.count("hrv")
            # The legacy payload survived too, and still decompresses: 0003 rewrites the
            # table, not the bytes.
            kept = list(store.raw_payloads())
            store.close()
            self.assertEqual(version, str(schema.SCHEMA_VERSION))
            self.assertEqual(survived, 1, "existing rows survive the migration")
            self.assertEqual([p.body for p in kept], ['{"hrv":[]}'])
            self.assertEqual([p.encoding for p in kept], ["identity"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
