"""Tests for the store: writes, the read API, coverage, downsampling, seeding.

The read API is what plugins hold, so its two promises get explicit tests: the
window is half-open and UTC, and nothing about a plugin's handle can write.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from fitbit_monitor.errors import StoreError, StoreUnavailableError
from fitbit_monitor.store.downsample import aggregate, bucket_key, daily_values
from fitbit_monitor.store.downsample import downsample as bucket_points
from fitbit_monitor.store.reader import ReadOnlyStore, SeriesPoint
from fitbit_monitor.store.seed import DATASETS, seed_store
from fitbit_monitor.store.writer import SeriesStore
from fitbit_monitor.timeutil import DateRange

T0 = datetime(2026, 5, 1, tzinfo=UTC)


class StoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "store.sqlite3"
        self.store = SeriesStore(self.path)
        self.addCleanup(self.store.close)

    def write(self, dataset: str, **values) -> None:
        self.store.put(dataset, T0, values)
        self.store.commit()

    def reader(self) -> ReadOnlyStore:
        store = ReadOnlyStore(self.path)
        self.addCleanup(store.close)
        return store


class WriterTest(StoreTestCase):
    def test_schema_is_created_on_first_use(self) -> None:
        self.assertTrue(self.path.exists())
        tables = {
            r["name"]
            for r in self.store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        self.assertLessEqual({"series", "alerts", "raw_payload", "fm_meta"}, tables)

    def test_put_is_idempotent_per_instant(self) -> None:
        self.write("daily_summary", resting_hr=60.0)
        self.write("daily_summary", resting_hr=59.0)
        self.assertEqual(self.store.count("daily_summary"), 1)
        with ReadOnlyStore(self.path) as ro:
            point = next(ro.query("daily_summary"))
        self.assertEqual(point["resting_hr"], 59.0)

    def test_raw_payload_dedupes_by_content(self) -> None:
        now = datetime(2026, 5, 1, tzinfo=UTC)
        self.assertTrue(self.store.put_raw("heart_rate", "src", now, '{"bpm":[60]}'))
        self.assertFalse(self.store.put_raw("heart_rate", "src", now, '{"bpm":[60]}'))
        self.assertTrue(self.store.put_raw("heart_rate", "src", now, '{"bpm":[61]}'))

    def test_delete_range(self) -> None:
        for offset in range(5):
            self.store.put("hrv", T0 + timedelta(days=offset), {"rmssd": 40.0})
        self.store.commit()
        removed = self.store.delete_range("hrv", T0 + timedelta(days=1), T0 + timedelta(days=3))
        self.assertEqual(removed, 2)
        self.assertEqual(self.store.count("hrv"), 3)


class ReaderTest(StoreTestCase):
    def reader(self) -> ReadOnlyStore:
        store = ReadOnlyStore(self.path)
        self.addCleanup(store.close)
        return store

    def test_missing_store_names_the_fix(self) -> None:
        with self.assertRaises(StoreUnavailableError) as ctx:
            ReadOnlyStore(Path(self.tmp.name) / "nope.sqlite3")
        self.assertIn("fm dev seed", str(ctx.exception.hint))

    def test_query_window_is_half_open_and_utc(self) -> None:
        for offset in range(4):
            self.store.put("hrv", T0 + timedelta(days=offset), {"rmssd": 40.0 + offset})
        self.store.commit()
        points = list(self.reader().query("hrv", T0, T0 + timedelta(days=2)))
        self.assertEqual([p["rmssd"] for p in points], [40.0, 41.0])
        self.assertTrue(all(p.ts.tzinfo is not None for p in points))

    def test_query_field_filter_drops_incomplete_points(self) -> None:
        self.store.put("daily_summary", T0, {"steps": 9000.0})
        self.store.put("daily_summary", T0 + timedelta(days=1), {"steps": 8000.0, "resting_hr": 55.0})
        self.store.commit()
        got = list(self.reader().query("daily_summary", fields=["resting_hr"]))
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].ts, T0 + timedelta(days=1))

    def test_latest_first_ts_and_count(self) -> None:
        for offset in range(3):
            self.store.put("hrv", T0 + timedelta(days=offset), {"rmssd": 40.0})
        self.store.commit()
        ro = self.reader()
        self.assertEqual(ro.first_ts("hrv"), T0)
        self.assertEqual(ro.last_ts("hrv"), T0 + timedelta(days=2))
        self.assertEqual(ro.count("hrv"), 3)
        self.assertEqual(next(ro.query("hrv", order="desc")).ts, T0 + timedelta(days=2))
        self.assertIsNone(ro.first_ts("absent"))
        self.assertIsNone(ro.latest("absent"))

    def test_inverted_window_is_refused(self) -> None:
        with self.assertRaises(StoreError):
            list(self.reader().query("hrv", T0 + timedelta(days=5), T0))

    def test_coverage_reports_missing_days(self) -> None:
        for offset in (0, 1, 4):
            self.store.put("hrv", T0 + timedelta(days=offset), {"rmssd": 40.0})
        self.store.commit()
        cov = self.reader().coverage("hrv", T0, T0 + timedelta(days=7))
        self.assertEqual(cov.points, 3)
        self.assertEqual(cov.days_expected, 7)
        self.assertEqual(cov.days_present, 3)
        self.assertEqual(
            cov.missing_days,
            (date(2026, 5, 3), date(2026, 5, 4), date(2026, 5, 6), date(2026, 5, 7)),
        )
        self.assertAlmostEqual(cov.fraction, 3 / 7)
        self.assertTrue(cov.has_data)
        self.assertEqual(cov.as_dict()["days_present"], 3)

    def test_has_any_data(self) -> None:
        self.write("hrv", rmssd=40.0)
        window = DateRange(since=T0, until=T0 + timedelta(days=1))
        self.assertTrue(self.reader().has_any_data(["hrv"], window))
        self.assertFalse(self.reader().has_any_data(["absent"], window))
        later = DateRange(since=T0 + timedelta(days=10), until=T0 + timedelta(days=11))
        self.assertFalse(self.reader().has_any_data(["hrv"], later))

    def test_reader_cannot_write(self) -> None:
        self.write("hrv", rmssd=40.0)
        ro = self.reader()
        for forbidden in ("put", "put_many", "commit", "clear", "delete_range"):
            self.assertFalse(hasattr(ro, forbidden))
        with self.assertRaises(sqlite3.OperationalError):
            ro.conn.execute("INSERT INTO series VALUES ('x','y','{}',NULL)")
        with self.assertRaises(sqlite3.OperationalError):
            ro.conn.execute("DELETE FROM series")

    def test_reader_refuses_a_foreign_database(self) -> None:
        other = Path(self.tmp.name) / "foreign.sqlite3"
        conn = sqlite3.connect(other)
        conn.execute("CREATE TABLE series (dataset TEXT, ts_utc TEXT, payload TEXT)")
        conn.commit()
        conn.close()
        with self.assertRaises(StoreError):
            ReadOnlyStore(other)

    def test_closed_store_reports_instead_of_crashing_weirdly(self) -> None:
        ro = ReadOnlyStore(self.path)
        ro.close()
        with self.assertRaises(StoreError):
            ro.datasets()


class DownsampleTest(StoreTestCase):
    def test_aggregates(self) -> None:
        self.assertEqual(aggregate([1, 2, 3], "mean"), 2.0)
        self.assertEqual(aggregate([1, 2, 3], "median"), 2.0)
        self.assertEqual(aggregate([1, 2, 3], "min"), 1.0)
        self.assertEqual(aggregate([1, 2, 3], "max"), 3.0)
        self.assertEqual(aggregate([1, 2, 3], "sum"), 6.0)
        self.assertEqual(aggregate([1, 2, 3], "first"), 1.0)
        self.assertEqual(aggregate([1, 2, 3], "last"), 3.0)
        self.assertEqual(aggregate([1, 2, 3], "count"), 3.0)
        self.assertIsNone(aggregate([], "mean"))
        with self.assertRaises(Exception):
            aggregate([1], "p95")

    def test_bucket_keys(self) -> None:
        ts = datetime(2026, 5, 3, 13, 45, 20, tzinfo=UTC)
        self.assertEqual(bucket_key(ts, "hour"), datetime(2026, 5, 3, 13, tzinfo=UTC))
        self.assertEqual(bucket_key(ts, "day"), datetime(2026, 5, 3, tzinfo=UTC))
        self.assertEqual(bucket_key(ts, "week"), datetime(2026, 4, 27, tzinfo=UTC))  # the Monday before
        self.assertEqual(bucket_key(ts, "month"), datetime(2026, 5, 1, tzinfo=UTC))
        with self.assertRaises(Exception):
            bucket_key(ts, "fortnight")

    def test_downsample_buckets_fields_independently(self) -> None:
        for offset in range(3):
            self.store.put("heart_rate", T0 + timedelta(hours=offset), {"hr": 60.0 + offset})
        self.store.put("heart_rate", T0 + timedelta(hours=10), {"hr": 100.0})
        self.store.commit()
        buckets = list(bucket_points(self.reader().query("heart_rate"), "day"))
        self.assertEqual(len(buckets), 1)
        self.assertAlmostEqual(buckets[0]["hr"], (60 + 61 + 62 + 100) / 4)
        hourly = list(bucket_points(self.reader().query("heart_rate"), "hour", how="max"))
        self.assertEqual([b["hr"] for b in hourly], [60.0, 61.0, 62.0, 100.0])

    def test_downsample_can_select_fields(self) -> None:
        self.store.put("heart_rate", T0, {"hr": 60.0, "other": 5.0})
        self.store.commit()
        bucket = next(iter(bucket_points(self.reader().query("heart_rate"), "day", fields=["hr"])))
        self.assertEqual(set(bucket.values), {"hr"})

    def test_daily_values_maps_dates(self) -> None:
        self.store.put("hrv", T0, {"rmssd": 40.0})
        self.store.put("hrv", T0 + timedelta(days=1), {"rmssd": 50.0})
        self.store.commit()
        self.assertEqual(daily_values(self.reader().query("hrv"), "rmssd"), {T0.date(): 40.0, (T0 + timedelta(days=1)).date(): 50.0})
        self.assertEqual(daily_values(self.reader().query("hrv"), "absent"), {})

    def test_seriespoint_helpers(self) -> None:
        point = SeriesPoint(ts=T0, values={"hr": 60.0})
        self.assertEqual(point["hr"], 60.0)
        self.assertEqual(point.get("absent", 1.0), 1.0)
        self.assertIn("hr", point)
        self.assertEqual(point.as_dict(), {"hr": 60.0})


class SeedTest(StoreTestCase):
    def test_seed_is_deterministic(self) -> None:
        plan_a = seed_store(self.store, days=10, seed=7, end=date(2026, 5, 31))
        rows_a = [tuple(r) for r in self.store.conn.execute("SELECT * FROM series ORDER BY dataset, ts_utc")]
        second = SeriesStore(Path(self.tmp.name) / "second.sqlite3")
        self.addCleanup(second.close)
        plan_b = seed_store(second, days=10, seed=7, end=date(2026, 5, 31))
        rows_b = [tuple(r) for r in second.conn.execute("SELECT * FROM series ORDER BY dataset, ts_utc")]
        self.assertEqual(rows_a, rows_b)
        self.assertEqual(plan_a.as_dict(), plan_b.as_dict())

    def test_seed_writes_every_declared_dataset(self) -> None:
        seed_store(self.store, days=5, seed=1, end=date(2026, 5, 31), drop_missing_days=False)
        for dataset in DATASETS:
            self.assertGreater(self.store.count(dataset), 0, dataset)

    def test_seed_can_be_gapless(self) -> None:
        plan = seed_store(self.store, days=6, seed=1, end=date(2026, 5, 31), drop_missing_days=False)
        self.assertEqual(plan.dropped_days, ())
        self.assertEqual(len(self.store.days_with_data()), 6)

    def test_seed_rejects_zero_days(self) -> None:
        with self.assertRaises(ValueError):
            seed_store(self.store, days=0)

    def test_intraday_range_is_plausible(self) -> None:
        seed_store(self.store, days=3, seed=1, end=date(2026, 5, 31), drop_missing_days=False)
        values = [
            row["payload"]
            for row in self.store.conn.execute("SELECT payload FROM series WHERE dataset='heart_rate'")
        ]
        self.assertTrue(values)
        import json

        hr_values = [json.loads(v)["hr"] for v in values]
        self.assertTrue(all(35 <= v <= 192 for v in hr_values))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
