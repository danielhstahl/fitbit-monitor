"""Store maintenance: stats, verify, export, backup, prune (fitb-ei6.6 / ei6.7).

The premise of the whole store is that this file becomes the durable copy, so the
commands that prove it is intact and get it out are not optional extras. What is
pinned here:

* ``verify`` re-hashes the raw payloads - a deliberately corrupted blob is a failed
  check and a nonzero exit, not a silent pass;
* ``backup`` produces a consistent snapshot **of a live database**: committed rows are
  in it, uncommitted ones are not, and the copy passes ``integrity_check``;
* ``export`` writes the series and the raw payloads, in both formats;
* ``prune`` does **nothing** until told to, and reports what it would have done.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from fitbit_monitor.errors import StoreError
from fitbit_monitor.store import maintenance
from fitbit_monitor.store import schema
from fitbit_monitor.store.reader import ReadOnlyStore
from fitbit_monitor.store.writer import SeriesStore

T0 = datetime(2026, 5, 1, tzinfo=UTC)
BODY = json.dumps({"hrv": [{"daily": 1}] * 200})


class MaintenanceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "store.sqlite3"
        self.store = SeriesStore(self.path)
        self.addCleanup(self.store.close)
        self.store.put("heart_rate", T0, {"hr": 61.0})
        self.store.put("heart_rate", T0 + timedelta(days=40), {"hr": 62.0})
        self.store.put("hrv", T0, {"rmssd": 40.0})
        self.store.put_raw("heart_rate", "/api/heart", T0, BODY, requested_start=date(2026, 5, 1))
        self.store.quarantine("hrv", "bad.csv", "row was not numeric", "{}")
        self.store.commit()

    def corrupt_raw_body(self, body: bytes = b"not gzip at all, honestly") -> None:
        self.store.conn.execute("UPDATE raw_payload SET body = ?", (body,))
        self.store.conn.commit()


class StatsTest(MaintenanceTestCase):
    def test_stats_reports_counts_coverage_and_both_sizes(self) -> None:
        report = maintenance.stats(self.store.conn, path=self.path)
        self.assertEqual(report["schema_version"], schema.SCHEMA_VERSION)
        self.assertGreater(report["file_bytes"], 0)
        by_name = {entry["dataset"]: entry for entry in report["datasets"]}
        self.assertEqual(by_name["heart_rate"]["points"], 2)
        self.assertEqual(by_name["heart_rate"]["kind"], "intraday")
        self.assertEqual(by_name["heart_rate"]["days_present"], 2)
        self.assertGreater(by_name["heart_rate"]["normalized_bytes"], 0)
        totals = report["totals"]
        self.assertEqual(totals["points"], 3)
        self.assertEqual(totals["quarantined"], 1)
        self.assertEqual(totals["payloads"], 1)
        self.assertEqual(totals["raw_bytes"], len(BODY))
        self.assertLess(totals["raw_stored_bytes"], totals["raw_bytes"], "gzip should be earning its keep")
        self.assertLess(totals["compression_ratio"], 1.0)
        self.assertGreater(totals["normalized_bytes"], 0)

    def test_stats_counts_duplicates_that_imports_refused(self) -> None:
        from fitbit_monitor.ingest.runlog import DatasetRun, RunLog

        RunLog(self.store).record(
            DatasetRun(
                run_id="r1",
                dataset="heart_rate",
                source="api",
                since=None,
                until=None,
                status="ok",
                started_at="2026-05-01T00:00:00+00:00",
                rows_duplicate=1440,
                payloads_duplicate=1,
            )
        )
        self.store.commit()
        totals = maintenance.stats(self.store.conn, path=self.path)["totals"]
        self.assertEqual(totals["duplicate_rows_by_import"], 1440)
        self.assertEqual(totals["duplicate_payloads_by_import"], 1)

    def test_human_bytes(self) -> None:
        self.assertEqual(maintenance.human_bytes(512), "512 B")
        self.assertEqual(maintenance.human_bytes(2048), "2.0 KiB")
        self.assertEqual(maintenance.human_bytes(5 * 1024 * 1024), "5.0 MiB")


class VerifyTest(MaintenanceTestCase):
    def names(self, result: maintenance.Verification) -> dict[str, bool]:
        return {check.name: check.ok for check in result.checks}

    def test_a_healthy_store_passes_every_check(self) -> None:
        result = maintenance.verify(self.store.conn)
        self.assertTrue(result.ok, result.problems)
        self.assertEqual(
            self.names(result),
            {
                "schema_version": True,
                "integrity_check": True,
                "foreign_keys": True,
                "dataset_registry": True,
                "series_rows": True,
                "raw_payload_hashes": True,
            },
        )
        self.assertEqual(result.problem_count, 0)
        self.assertIn("checked_at", result.as_dict())

    def test_a_corrupted_raw_blob_is_detected(self) -> None:
        self.corrupt_raw_body()
        result = maintenance.verify(self.store.conn)
        self.assertFalse(result.ok)
        self.assertFalse(self.names(result)["raw_payload_hashes"])
        self.assertIn("cannot decompress", " ".join(result.problems))

    def test_a_payload_that_decodes_but_hashes_differently_is_detected(self) -> None:
        # The shape of silent corruption: readable bytes, wrong content.
        tampered = '{"hrv": [' + ",".join(str(i) for i in range(300)) + "]}"  # same shape, different bytes
        blob, size, stored, encoding = maintenance.raw_store.encode_body(tampered)
        self.assertEqual(encoding, "gzip", "the tampered body has to be big enough to compress")
        self.store.conn.execute("UPDATE raw_payload SET body = ?, bytes = ?, stored_bytes = ?", (blob, size, stored))
        self.store.conn.commit()
        result = maintenance.verify(self.store.conn)
        self.assertFalse(result.ok)
        self.assertIn("hash drift", " ".join(result.problems))

    def test_a_claimed_size_that_does_not_match_is_detected(self) -> None:
        self.store.conn.execute("UPDATE raw_payload SET bytes = bytes + 1")
        self.store.conn.commit()
        result = maintenance.verify(self.store.conn)
        self.assertFalse(self.names(result)["raw_payload_hashes"])
        self.assertIn("claimed size", " ".join(result.problems))

    def test_unparseable_series_payload_is_detected(self) -> None:
        self.store.conn.execute("UPDATE series SET payload = '{not json' WHERE dataset = 'hrv'")
        self.store.conn.commit()
        result = maintenance.verify(self.store.conn)
        self.assertFalse(self.names(result)["series_rows"])

    def test_a_row_for_an_unregistered_dataset_is_detected(self) -> None:
        self.store.conn.execute("PRAGMA foreign_keys=OFF")
        self.store.conn.execute(
            "INSERT INTO series VALUES ('phantom', ?, '{\"x\": 1}', NULL)", (T0.isoformat(),)
        )
        self.store.conn.execute("PRAGMA foreign_keys=ON")
        self.store.conn.commit()
        result = maintenance.verify(self.store.conn)
        self.assertFalse(self.names(result)["dataset_registry"])
        self.assertIn("phantom", " ".join(result.problems))

    def test_quick_skips_the_body_check_and_sample_bounds_it(self) -> None:
        self.assertNotIn(
            "raw_payload_hashes", self.names(maintenance.verify(self.store.conn, include_bodies=False))
        )
        self.store.put_raw("heart_rate", "/api/second", T0 + timedelta(days=1), BODY)
        self.store.commit()
        sampled = maintenance.verify(self.store.conn, sample=1)
        checked = next(check for check in sampled.checks if check.name == "raw_payload_hashes")
        self.assertEqual(checked.checked, 1)
        self.assertTrue(sampled.ok)

    def test_problems_are_capped_but_counted(self) -> None:
        self.store.conn.executemany(
            "INSERT INTO series VALUES ('hrv', ?, 'nope', NULL)",
            [((T0 + timedelta(days=day)).isoformat(),) for day in range(1, maintenance.MAX_REPORTED_PROBLEMS + 6)],
        )
        self.store.conn.commit()
        result = maintenance.verify(self.store.conn, include_bodies=False)
        check = next(check for check in result.checks if check.name == "series_rows")
        self.assertEqual(len(check.problems), maintenance.MAX_REPORTED_PROBLEMS + 1)
        self.assertIn("more", check.problems[-1])


class ExportTest(MaintenanceTestCase):
    def test_series_jsonl_keeps_the_nested_shape(self) -> None:
        out = io.StringIO()
        written = maintenance.export_series(self.store.conn, out, dataset="heart_rate")
        self.assertEqual(written, 2)
        records = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual(records[0]["dataset"], "heart_rate")
        self.assertEqual(records[0]["values"], {"hr": 61.0})
        self.assertEqual(records[0]["ts_utc"], T0.isoformat())

    def test_series_csv_is_wide_with_one_column_per_field(self) -> None:
        import csv

        out = io.StringIO()
        maintenance.export_series(self.store.conn, out, fmt="csv")
        rows = list(csv.DictReader(io.StringIO(out.getvalue())))
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["dataset"], "heart_rate")
        self.assertEqual(rows[0]["hr"], "61.0")
        self.assertEqual([row["rmssd"] for row in rows if row["dataset"] == "hrv"], ["40.0"])

    def test_a_missing_field_is_an_empty_cell_not_a_zero(self) -> None:
        import csv

        out = io.StringIO()
        maintenance.export_series(self.store.conn, out, fmt="csv")
        rows = list(csv.DictReader(io.StringIO(out.getvalue())))
        hrv = next(row for row in rows if row["dataset"] == "hrv")
        self.assertEqual(hrv["hr"], "", "absent is not zero")
        heart = next(row for row in rows if row["dataset"] == "heart_rate")
        self.assertEqual(heart["rmssd"], "")

    def test_raw_export_includes_the_body(self) -> None:
        out = io.StringIO()
        written = maintenance.export_raw(self.store.conn, out)
        self.assertEqual(written, 1)
        record = json.loads(out.getvalue())
        self.assertEqual(record["body"], BODY)
        self.assertEqual(record["source_id"], "/api/heart")
        self.assertEqual(record["requested_start"], "2026-05-01")

    def test_raw_export_as_csv_carries_the_body_too(self) -> None:
        import csv

        out = io.StringIO()
        maintenance.export_raw(self.store.conn, out, fmt="csv")
        rows = list(csv.DictReader(io.StringIO(out.getvalue())))
        self.assertEqual(rows[0]["body"], BODY)
        self.assertEqual(rows[0]["source_id"], "/api/heart")

    def test_window_filters_apply_to_both(self) -> None:
        out = io.StringIO()
        self.assertEqual(
            maintenance.export_series(self.store.conn, out, since=T0 + timedelta(days=1)), 1
        )
        self.assertEqual(
            maintenance.export_series(self.store.conn, io.StringIO(), until=T0 + timedelta(days=1)), 2
        )

    def test_an_unknown_format_is_refused(self) -> None:
        with self.assertRaises(StoreError):
            maintenance.export_series(self.store.conn, io.StringIO(), fmt="parquet")

    def test_export_can_go_to_a_file(self) -> None:
        target = Path(self.tmp.name) / "out" / "series.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8") as handle:
            written = maintenance.export_series(self.store.conn, handle)
        self.assertEqual(written, 3)
        self.assertEqual(len(target.read_text().splitlines()), 3)


class BackupTest(MaintenanceTestCase):
    def test_backup_of_a_live_database_is_consistent(self) -> None:
        second = schema.connect(self.path)  # a second writer mid-transaction
        second.execute("BEGIN IMMEDIATE")
        second.execute("INSERT INTO series VALUES ('ghost', ?, '{\"x\": 1}', NULL)", (T0.isoformat(),))
        try:
            snapshot = maintenance.backup(self.path, Path(self.tmp.name) / "backup.sqlite3")
            self.assertTrue(snapshot.ok)
            self.assertGreater(snapshot.bytes, 0)
            reader = ReadOnlyStore(snapshot.path)
            try:
                self.assertEqual(reader.count("heart_rate"), 2)
                self.assertEqual(sorted(reader.datasets()), ["heart_rate", "hrv"])
                self.assertFalse(reader.has_dataset("ghost"), "uncommitted work must not appear")
                self.assertEqual(list(reader.iter_raw())[0].body, BODY)
            finally:
                reader.close()
        finally:
            second.rollback()
            second.close()

    def test_backup_is_idempotent_and_checks_itself(self) -> None:
        destination = Path(self.tmp.name) / "again.sqlite3"
        first = maintenance.backup(self.path, destination)
        second = maintenance.backup(self.path, destination)
        self.assertTrue(second.ok)
        self.assertEqual(first.bytes, second.bytes)

    def test_backup_refuses_to_overwrite_the_store_itself(self) -> None:
        with self.assertRaises(StoreError):
            maintenance.backup(self.path, self.path)

    def test_backup_of_a_missing_store_is_a_clear_error(self) -> None:
        with self.assertRaises(StoreError):
            maintenance.backup(Path(self.tmp.name) / "nope.sqlite3", Path(self.tmp.name) / "b.sqlite3")


class PruneTest(MaintenanceTestCase):
    #: The clock the prune calls are told to believe. With --days 30 the effective
    #: cutoff is 2026-07-02, which is older than every row setUp wrote.
    NOW = datetime(2026, 8, 1, tzinfo=UTC)

    def test_dry_run_is_the_default_and_deletes_nothing(self) -> None:
        before = (self.store.count(), len(list(self.store.raw_payloads(with_body=False))))
        result = maintenance.prune(self.store, days=30, what=("all",), now=self.NOW)
        self.assertTrue(result.dry_run)
        self.assertEqual(result.removed["series"], 3)
        self.assertEqual(result.removed["raw_payload"], 1)
        self.assertEqual(result.removed["quarantine"], 0)
        self.assertGreater(result.bytes_freed, 0)
        self.assertEqual(self.store.count(), before[0])
        self.assertEqual(len(list(self.store.raw_payloads(with_body=False))), before[1])

    def test_execute_deletes_only_what_is_older_than_the_horizon(self) -> None:
        result = maintenance.prune(self.store, days=30, what=("all",), execute=True, now=self.NOW)
        self.assertFalse(result.dry_run)
        self.assertEqual(result.removed["series"], 3)
        self.assertEqual(self.store.count(), 0)
        self.assertEqual(self.store.count("heart_rate"), 0)
        self.assertEqual(list(self.store.raw_payloads(with_body=False)), [])
        # quarantined today is not "older than 30 days", whatever the horizon says
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) AS n FROM quarantine").fetchone()["n"], 1)

    def test_what_defaults_to_raw_and_can_be_named(self) -> None:
        result = maintenance.prune(
            self.store, days=30, what=("series",), execute=True, now=self.NOW
        )
        self.assertEqual(result.removed, {"series": 3})
        self.assertEqual(len(list(self.store.raw_payloads(with_body=False))), 1, "raw was not asked for")

    def test_all_covers_quarantine_too(self) -> None:
        self.store.conn.execute(
            "INSERT INTO quarantine (dataset, source_id, reason, body, quarantined_at) VALUES (?,?,?,?,?)",
            ("hrv", "old.csv", "ancient", "{}", (T0 - timedelta(days=1)).isoformat()),
        )
        self.store.commit()
        result = maintenance.prune(self.store, days=30, what=("all",), execute=True, now=self.NOW)
        self.assertEqual(result.removed["quarantine"], 1)
        self.assertEqual(result.removed["raw_payload"], 1)

    def test_nothing_to_prune_when_the_horizon_is_generous(self) -> None:
        result = maintenance.prune(self.store, days=100000, execute=True, now=self.NOW)
        self.assertEqual(result.rows, 0)

    def test_bad_arguments_are_refused_before_anything_is_deleted(self) -> None:
        with self.assertRaises(StoreError):
            maintenance.prune(self.store, days=0)
        with self.assertRaises(StoreError):
            maintenance.prune(self.store, days=30, what=("everything",), now=self.NOW)

    def test_vacuum_shrinks_the_file(self) -> None:
        import random
        import string

        alphabet = string.ascii_letters + string.digits
        noisy = "".join(random.Random(7).choices(alphabet, k=200_000))
        for day in range(6):
            # distinct bodies, or content-addressing would store one payload for all six
            self.store.put_raw("heart_rate", f"/api/{day}", T0 - timedelta(days=day), noisy + str(day))
        self.store.commit()

        def footprint() -> int:
            # WAL means the data may be in either file; the pair is the store.
            return sum(path.stat().st_size for path in self.path.parent.glob("store.sqlite3*"))

        before = footprint()
        result = maintenance.prune(
            self.store, days=30, what=("raw",), execute=True, vacuum=True, now=self.NOW
        )
        self.assertTrue(result.vacuumed)
        self.assertLess(footprint(), before)


class StoreCommandTest(MaintenanceTestCase):
    def setUp(self) -> None:
        import os

        super().setUp()
        self.home = Path(self.tmp.name)
        for name, value in (("XDG_DATA_HOME", self.home / "data"), ("XDG_CONFIG_HOME", self.home / "config")):
            original = os.environ.get(name)
            os.environ[name] = str(value)
            self.addCleanup(
                lambda: os.environ.__setitem__(name, original) if original is not None else os.environ.pop(name, None)
            )
        self.db = self.home / "data" / "fitbit-monitor" / "store.sqlite3"
        self.store.close()  # the CLI opens its own handle on the same file
        self.store = SeriesStore(self.db)
        self.addCleanup(self.store.close)
        self.store.put("heart_rate", T0, {"hr": 61.0})
        self.store.put_raw("heart_rate", "p", T0, BODY)
        self.store.commit()
        self.store.close()

    def cli(self, *argv):
        from fitbit_monitor.cli import main

        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue() + err.getvalue()

    def test_stats_and_verify_end_to_end(self) -> None:
        code, text = self.cli("store", "stats", "--json")
        self.assertEqual(code, 0, text)
        payload = json.loads(text)
        self.assertEqual(payload["totals"]["payloads"], 1)

        code, text = self.cli("store", "stats")
        self.assertEqual(code, 0, text)
        self.assertIn("heart_rate", text)

        code, text = self.cli("store", "verify", "--json")
        self.assertEqual(code, 0, text)
        self.assertTrue(json.loads(text)["ok"])

    def test_verify_exits_nonzero_when_a_blob_is_corrupt(self) -> None:
        conn = schema.connect(self.db)
        conn.execute("UPDATE raw_payload SET body = 'tampered'")
        conn.commit()
        conn.close()
        code, text = self.cli("store", "verify")
        self.assertEqual(code, 1, text)
        self.assertIn("FAIL", text)
        code, text = self.cli("store", "verify", "--json")
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(text)["ok"])

    def test_backup_command_writes_and_reports_a_snapshot(self) -> None:
        target = self.home / "snapshots" / "store.sqlite3"
        code, text = self.cli("store", "backup", "--output", str(target))
        self.assertEqual(code, 0, text)
        self.assertTrue(target.exists())
        code, text = self.cli("store", "backup", "--json")
        self.assertEqual(code, 0, text)
        self.assertTrue(json.loads(text)["ok"])

    def test_prune_defaults_to_a_dry_run_from_the_command_line(self) -> None:
        before = self.row_count()
        code, text = self.cli("store", "prune", "--days", "1")
        self.assertEqual(code, 0, text)
        self.assertIn("dry run", text)
        self.assertEqual(self.row_count(), before, "nothing may be deleted without --execute")

        code, text = self.cli("store", "prune", "--days", "1", "--json")
        self.assertEqual(code, 0, text)
        self.assertEqual(json.loads(text)["removed"]["raw_payload"], 1)

        # The default target is raw only: the series plugins read is never the default.
        code, text = self.cli("store", "prune", "--days", "1", "--execute")
        self.assertEqual(code, 0, text)
        self.assertEqual(self.row_count(), before)
        self.assertEqual(self.raw_count(), 0)

        code, text = self.cli("store", "prune", "--days", "1", "--what", "series", "--execute")
        self.assertEqual(code, 0, text)
        self.assertEqual(self.row_count(), 0)

    def test_export_command_writes_a_file_and_summarises(self) -> None:
        target = self.home / "out" / "raw.jsonl"
        code, text = self.cli("store", "export", "--what", "raw", "--format", "jsonl", "--output", str(target), "--json")
        self.assertEqual(code, 0, text)
        summary = json.loads(text)
        self.assertEqual(summary["records"], 1)
        self.assertEqual(json.loads(target.read_text())["body"], BODY)

        code, text = self.cli("store", "export", "--format", "csv", "--dataset", "heart_rate")
        self.assertEqual(code, 0, text)
        self.assertIn("dataset,ts_utc,source_id,hr", text)

    def test_commands_report_a_missing_store(self) -> None:
        self.db.unlink()
        for argv in (("db", "status"), ("store", "stats"), ("store", "verify"), ("store", "backup")):
            code, _ = self.cli(*argv)
            self.assertEqual(code, 4, argv)

    def row_count(self) -> int:
        return self._count("series")

    def raw_count(self) -> int:
        return self._count("raw_payload")

    def _count(self, table: str) -> int:
        conn = schema.connect(self.db)
        try:
            return int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()[0])
        finally:
            conn.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
