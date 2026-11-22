"""The read API: the surface plugins and reports are built on (fitb-ei6.4).

The promises this file exists to keep, in the order the ticket lists them: iterate a
dataset over a range as typed points with units, fetch a single day, list what is stored
and how complete it is, read the retained raw payload, downsample with the aggregation
named, and flag gaps rather than skipping them. Plus the one that makes all the others
safe to hand to third-party code: a read-only handle cannot write.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from fitbit_monitor.errors import StoreError
from fitbit_monitor.store import downsample, stored_datasets
from fitbit_monitor.store.downsample import bucket_key, downsample as bucket_points
from fitbit_monitor.store.reader import Completeness, DatasetInfo, Gap, ReadOnlyStore
from fitbit_monitor.store.writer import SeriesStore

T0 = datetime(2026, 5, 1, tzinfo=UTC)


class ReadApiTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "store.sqlite3"
        self.store = SeriesStore(self.path)
        self.addCleanup(self.store.close)

    def reader(self) -> ReadOnlyStore:
        store = ReadOnlyStore(self.path)
        self.addCleanup(store.close)
        return store

    def put_hr(self, *stamps: datetime, hr: float = 61.0) -> None:
        for stamp in stamps:
            self.store.put("heart_rate", stamp, {"hr": hr})
        self.store.commit()


class TypedPointsTest(ReadApiTestCase):
    def test_points_carry_units_from_the_catalog(self) -> None:
        self.put_hr(T0)
        point = next(self.reader().query("heart_rate"))
        self.assertEqual(point.dataset, "heart_rate")
        self.assertEqual(point.unit("hr"), "beats per minute")
        self.assertEqual(point.items_with_units(), (("hr", 61.0, "beats per minute"),))
        self.assertEqual(point.unit("unknown_field"), None)

    def test_units_and_describe_come_from_one_catalog(self) -> None:
        reader = self.reader()
        self.assertEqual(reader.units("hrv"), {"rmssd": "milliseconds", "sdnn": "milliseconds", "hrv_n": "samples"})
        self.assertEqual(reader.describe("heart_rate").kind, "intraday")
        self.assertEqual(reader.grain_seconds("heart_rate"), 60)
        self.assertEqual(reader.describe("mystery_dataset").kind, "daily")
        self.assertIsNone(reader.grain_seconds("mystery_dataset"))

    def test_every_point_is_utc_and_aware(self) -> None:
        self.put_hr(T0 + timedelta(minutes=5))
        for point in self.reader().query("heart_rate"):
            self.assertIsNotNone(point.ts.tzinfo)
            self.assertEqual(point.ts.utcoffset(), timedelta(0))

    def test_a_daily_point_sits_at_utc_midnight_of_its_day(self) -> None:
        self.store.put("daily_summary", T0, {"steps": 9000.0})
        self.store.commit()
        point = next(self.reader().query("daily_summary"))
        self.assertEqual(point.ts, datetime(2026, 5, 1, tzinfo=UTC))
        self.assertEqual(point.ts.date(), date(2026, 5, 1))
        self.assertEqual(point.unit("steps"), "steps")


class DayLookupTest(ReadApiTestCase):
    def test_day_returns_the_calendar_day_only(self) -> None:
        self.put_hr(
            T0,
            T0 + timedelta(hours=23),
            T0 + timedelta(days=1),
            T0 + timedelta(days=1, minutes=5),
        )
        reader = self.reader()
        self.assertEqual(len(reader.day("heart_rate", date(2026, 5, 1))), 2)
        self.assertEqual(len(reader.day("heart_rate", "2026-05-01")), 2)
        self.assertEqual(len(reader.day("heart_rate", T0 + timedelta(hours=13))), 2)
        self.assertEqual([p.ts for p in reader.day("heart_rate", "2026-05-02")], [T0 + timedelta(days=1), T0 + timedelta(days=1, minutes=5)])
        self.assertEqual(reader.day("heart_rate", "2026-06-01"), [])

    def test_day_of_a_daily_dataset_is_the_one_row_lookup(self) -> None:
        self.store.put("hrv", T0, {"rmssd": 40.0})
        self.store.commit()
        points = self.reader().day("hrv", "2026-05-01")
        self.assertEqual([p["rmssd"] for p in points], [40.0])


class GapAndCoverageTest(ReadApiTestCase):
    def test_gaps_are_flagged_against_the_series_grain(self) -> None:
        stamps = [T0 + timedelta(minutes=5 * i) for i in range(6)]
        stamps += [T0 + timedelta(minutes=5 * i) for i in range(200, 210)]
        self.put_hr(*stamps)
        gaps = self.reader().gaps("heart_rate", T0, T0 + timedelta(days=1))
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0].start, stamps[5])
        self.assertEqual(gaps[0].end, stamps[6])
        self.assertAlmostEqual(gaps[0].hours, (200 - 5) * 5 / 60)
        self.assertEqual(gaps[0].as_dict()["hours"], round((195 * 5) / 60, 2))

    def test_an_explicit_threshold_beats_the_default_grain(self) -> None:
        stamps = [T0 + timedelta(minutes=5 * i) for i in range(12)]  # an hour of samples
        stamps += [T0 + timedelta(minutes=5 * i) for i in range(36, 48)]  # two hours later
        self.put_hr(*stamps)
        reader = self.reader()
        self.assertEqual(len(reader.gaps("heart_rate", T0, T0 + timedelta(days=1))), 1)
        self.assertEqual(
            reader.gaps("heart_rate", T0, T0 + timedelta(days=1), max_gap_seconds=5 * 3600), ()
        )

    def test_completeness_names_missing_days_and_gaps(self) -> None:
        self.store.put("daily_summary", T0, {"steps": 1.0})
        self.store.put("daily_summary", T0 + timedelta(days=4), {"steps": 1.0})
        self.store.commit()
        report = self.reader().completeness("daily_summary", T0, T0 + timedelta(days=5))
        self.assertIsInstance(report, Completeness)
        self.assertFalse(report.complete)
        self.assertEqual(
            report.coverage.missing_days, (date(2026, 5, 2), date(2026, 5, 3), date(2026, 5, 4))
        )
        self.assertEqual(len(report.gaps), 1)
        self.assertEqual(report.longest_gap.start, T0)
        self.assertEqual(report.longest_gap.end, T0 + timedelta(days=4))
        payload = report.as_dict()
        self.assertIn("missing_days", payload)
        self.assertEqual(len(payload["gaps"]), 1)

    def test_a_complete_window_is_reported_as_complete(self) -> None:
        for day in range(4):
            self.store.put("daily_summary", T0 + timedelta(days=day), {"steps": 1.0})
        self.store.commit()
        report = self.reader().completeness("daily_summary", T0, T0 + timedelta(days=4))
        self.assertTrue(report.complete, report.as_dict())
        self.assertIsNone(report.longest_gap)

    def test_gap_type_is_a_documented_value(self) -> None:
        gap = Gap(start=T0, end=T0 + timedelta(hours=1), seconds=3600.0)
        self.assertEqual(gap.hours, 1.0)
        self.assertEqual(gap.as_dict()["seconds"], 3600.0)


class StoredDatasetTest(ReadApiTestCase):
    def test_stored_datasets_lists_extent_and_bytes(self) -> None:
        self.put_hr(T0, T0 + timedelta(days=1))
        self.store.put("hrv", T0, {"rmssd": 40.0})
        self.store.put_raw("heart_rate", "p", T0, '{"big": "' + "x" * 400 + '"}')
        self.store.commit()
        infos = {info.name: info for info in self.reader().stored_datasets()}
        self.assertEqual(set(infos), {"heart_rate", "hrv"})
        heart = infos["heart_rate"]
        self.assertEqual(heart.kind, "intraday")
        self.assertEqual(heart.points, 2)
        self.assertEqual(heart.days_present, 2)
        self.assertEqual((heart.first_ts, heart.last_ts), (T0, T0 + timedelta(days=1)))
        self.assertEqual(heart.payload_count, 1)
        self.assertGreater(heart.payload_bytes, 0)
        self.assertEqual(heart.units, {"hr": "beats per minute"})
        self.assertEqual(heart.fields, ("hr",))
        self.assertIsInstance(heart, DatasetInfo)
        self.assertEqual(stored_datasets(self.store.conn)[0].name, "heart_rate")

    def test_a_dataset_with_only_payloads_still_shows_up(self) -> None:
        self.store.put_raw("spo2", "p", T0, "{}")
        infos = {info.name: info for info in self.reader().stored_datasets()}
        self.assertEqual(infos["spo2"].points, 0)
        self.assertEqual(infos["spo2"].payload_count, 1)


class RawAccessTest(ReadApiTestCase):
    def test_read_the_stored_response_for_a_dataset(self) -> None:
        body = '{"heart": [{"heartRate": 61}]}'
        self.store.put_raw("heart_rate", "/api/path", T0, body)
        reader = self.reader()
        payload = reader.raw_for("heart_rate", T0)
        self.assertEqual(payload.body, body)
        self.assertEqual(payload.source_id, "/api/path")
        digest = payload.content_hash
        self.assertEqual(reader.raw_payload(digest).body, body)
        self.assertEqual(list(reader.iter_raw(dataset="heart_rate"))[0].body, body)


class DownsampleTest(ReadApiTestCase):
    def test_per_minute_and_per_hour_buckets_name_their_method(self) -> None:
        for minute in range(3):
            self.store.put("heart_rate", T0 + timedelta(minutes=minute), {"hr": 60.0 + minute})
        self.store.commit()
        reader = self.reader()
        per_minute = list(bucket_points(reader.query("heart_rate"), "minute", how="max"))
        self.assertEqual([p["hr"] for p in per_minute], [60.0, 61.0, 62.0])
        hourly = list(bucket_points(reader.query("heart_rate"), "hour", how="sum"))
        self.assertEqual([p["hr"] for p in hourly], [183.0])
        self.assertEqual(bucket_key(T0 + timedelta(minutes=7, seconds=30), "minute"), T0 + timedelta(minutes=7))

    def test_downsample_is_importable_from_the_package_root(self) -> None:
        self.store.put("heart_rate", T0, {"hr": 60.0})
        self.store.commit()
        buckets = list(downsample(self.reader().query("heart_rate"), "day", how="mean"))
        self.assertEqual(buckets[0].ts, T0)


class ReadOnlyProofTest(ReadApiTestCase):
    def test_the_read_api_leaves_the_store_exactly_as_it_found_it(self) -> None:
        self.put_hr(T0, T0 + timedelta(days=1))
        self.store.put("hrv", T0, {"rmssd": 40.0})
        self.store.put_raw("heart_rate", "p", T0, "{}")
        self.store.commit()

        def fingerprint() -> dict[str, bytes]:
            # -shm is SQLite's shared WAL index: opening the database at all touches it,
            # and it holds no data. Anything else changing is a real write.
            return {
                path.name: path.read_bytes()
                for path in sorted(self.path.parent.glob("store.sqlite3*"))
                if not path.name.endswith("-shm")
            }

        before = fingerprint()
        reader = ReadOnlyStore(self.path)
        try:
            reader.datasets()
            reader.stored_datasets()
            list(reader.query("heart_rate", T0, T0 + timedelta(days=9)))
            reader.day("hrv", "2026-05-01")
            reader.coverage("heart_rate", T0, T0 + timedelta(days=9))
            reader.completeness("heart_rate", T0, T0 + timedelta(days=9))
            reader.gaps("hrv", T0, T0 + timedelta(days=9))
            list(reader.iter_raw())
            reader.raw_totals()
            reader.assert_read_only()
        finally:
            reader.close()
        self.assertEqual(fingerprint(), before, "a read-only handle must not touch the files")

    def test_assert_read_only_shouts_when_the_handle_can_write(self) -> None:
        with self.assertRaises(StoreError):
            ReadOnlyStore(self.path, connection=self.store.conn).assert_read_only()

    def test_sql_writes_are_refused_through_the_connection_too(self) -> None:
        self.put_hr(T0)
        reader = self.reader()
        for statement in ("INSERT INTO series VALUES ('x','y','{}',NULL)", "DELETE FROM series"):
            with self.assertRaises(Exception):
                reader.conn.execute(statement)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
