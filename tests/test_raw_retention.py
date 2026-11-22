"""Raw payload retention: the bytes the source sent are kept, once, and readable.

ADR-0003 makes the retained payload the record and the normalized rows a derived
convenience, so the properties tested here are the ones the rest of the program leans
on: identical bytes are stored once however many imports asked for them, the stored blob
round-trips exactly, the encoding is recorded rather than guessed, and the window the
payload was *asked* for travels with it.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from fitbit_monitor.ingest.pipeline import Ingest
from fitbit_monitor.store import raw as raw_store
from fitbit_monitor.store.reader import ReadOnlyStore
from fitbit_monitor.store.writer import SeriesStore

T0 = datetime(2026, 5, 1, tzinfo=UTC)
BIG = json.dumps({"heart": [{"dateTime": f"2026-05-01T{m:02d}:00", "heartRate": 60} for m in range(60 * 24)]})


class RawStoreTestCase(unittest.TestCase):
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


class EncodingTest(unittest.TestCase):
    def test_small_bodies_are_stored_as_they_came(self) -> None:
        blob, size, stored, encoding = raw_store.encode_body('{"x": 1}')
        self.assertEqual(encoding, raw_store.ENCODING_IDENTITY)
        self.assertEqual((size, stored), (8, 8))
        self.assertEqual(blob, b'{"x": 1}')

    def test_real_payloads_are_compressed_and_round_trip(self) -> None:
        blob, size, stored, encoding = raw_store.encode_body(BIG)
        self.assertEqual(encoding, raw_store.ENCODING_GZIP)
        self.assertEqual(size, len(BIG))
        self.assertLess(stored, size / 4, "a day of intraday JSON should compress hard")
        self.assertEqual(raw_store.decode_body(blob, encoding), BIG)

    def test_decode_refuses_an_unknown_encoding_instead_of_guessing(self) -> None:
        with self.assertRaises(ValueError):
            raw_store.decode_body(b"whatever", "brotli")

    def test_hash_is_over_the_text_as_received(self) -> None:
        self.assertEqual(raw_store.content_hash("abc"), raw_store.content_hash(b"abc"))
        self.assertNotEqual(raw_store.content_hash("abc"), raw_store.content_hash("abd"))


class PutRawTest(RawStoreTestCase):
    def test_identical_bytes_are_stored_once(self) -> None:
        self.assertTrue(self.store.put_raw("heart_rate", "path-a", T0, BIG))
        self.assertFalse(self.store.put_raw("heart_rate", "path-b", T0 + timedelta(days=1), BIG))
        kept = list(self.store.raw_payloads())
        self.assertEqual(len(kept), 1, "the second import of the same day costs nothing")
        self.assertEqual(kept[0].source_id, "path-a", "the first fetch keeps the row")

    def test_different_bytes_are_different_payloads(self) -> None:
        self.assertTrue(self.store.put_raw("heart_rate", "a", T0, '{"hr": 60}'))
        self.assertTrue(self.store.put_raw("heart_rate", "a", T0, '{"hr": 61}'))
        self.assertEqual(len(list(self.store.raw_payloads(with_body=False))), 2)

    def test_requested_range_and_source_are_recorded(self) -> None:
        self.store.put_raw(
            "heart_rate",
            "/1/user/-/heart/date/2026-05-01/1d/1min.json",
            T0,
            BIG,
            source="api",
            requested_start=date(2026, 5, 1),
            requested_end=date(2026, 5, 7),
        )
        payload = next(self.store.raw_payloads(with_body=False))
        self.assertEqual(payload.requested_start, "2026-05-01")
        self.assertEqual(payload.requested_end, "2026-05-07")
        self.assertEqual(payload.source, "api")
        self.assertEqual(payload.dataset, "heart_rate")
        self.assertEqual(payload.bytes, len(BIG))
        self.assertLess(payload.ratio, 1.0)

    def test_a_payload_registers_its_dataset(self) -> None:
        self.store.put_raw("brand_new", "p", T0, "{}")
        self.assertIn("brand_new", self.store.registry())

    def test_bytes_stored_reports_both_sizes(self) -> None:
        self.store.put_raw("heart_rate", "a", T0, BIG)
        self.assertEqual(self.store.raw_total_bytes(stored=False), len(BIG))
        self.assertLess(self.store.raw_total_bytes(stored=True), self.store.raw_total_bytes(stored=False))


class RawReadTest(RawStoreTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store.put_raw("heart_rate", "day-1", T0, '{"day": 1}', requested_start=date(2026, 5, 1))
        self.store.put_raw(
            "heart_rate", "day-2", T0 + timedelta(days=1), '{"day": 2}', requested_start=date(2026, 5, 2)
        )
        self.store.put_raw("hrv", "week-1", T0 + timedelta(days=2), '{"day": 3}', source="csv")

    def test_reader_gets_the_body_back_verbatim(self) -> None:
        payloads = list(self.reader().iter_raw(dataset="heart_rate"))
        self.assertEqual([p.body for p in payloads], ['{"day": 1}', '{"day": 2}'])
        self.assertEqual([p.requested_start for p in payloads], ["2026-05-01", "2026-05-02"])

    def test_filters_narrow_and_bodies_can_be_left_out(self) -> None:
        self.assertEqual([p.body for p in self.reader().iter_raw(source="csv")], ['{"day": 3}'])
        windowed = self.reader().iter_raw(since=T0 + timedelta(hours=12), until=T0 + timedelta(days=2))
        self.assertEqual([p.source_id for p in windowed], ["day-2"])
        metadata_only = list(self.reader().iter_raw(with_body=False))
        self.assertEqual(len(metadata_only), 3)
        self.assertEqual([p.body for p in metadata_only], [None, None, None])

    def test_lookup_by_hash_and_by_dataset_instant(self) -> None:
        reader = self.reader()
        digest = raw_store.content_hash('{"day": 2}')
        self.assertEqual(reader.raw_payload(digest).source_id, "day-2")
        self.assertIsNone(reader.raw_payload("nope"))
        self.assertEqual(reader.raw_for("heart_rate", T0).source_id, "day-1")
        self.assertEqual(reader.raw_for("heart_rate", T0 + timedelta(days=5)).source_id, "day-2")
        self.assertIsNone(reader.raw_payload(digest, with_body=False).body)

    def test_totals_cover_each_dataset_and_all(self) -> None:
        totals = self.reader().raw_totals()
        self.assertEqual(totals["heart_rate"]["payloads"], 2)
        self.assertEqual(totals["hrv"]["payloads"], 1)
        self.assertEqual(totals["all"]["payloads"], 3)
        self.assertEqual(
            totals["all"]["bytes"], totals["heart_rate"]["bytes"] + totals["hrv"]["bytes"]
        )
        self.assertEqual(self.reader().raw_totals("hrv")["hrv"]["payloads"], 1)

    def test_raw_rows_are_readable_from_a_plain_connection_too(self) -> None:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            self.assertEqual(len(list(raw_store.iter_payloads(conn))), 3)
            self.assertEqual(raw_store.totals(conn)["all"]["payloads"], 3)
        finally:
            conn.close()

    def test_as_dict_can_leave_the_body_out(self) -> None:
        payload = next(self.reader().iter_raw(dataset="hrv"))
        self.assertIn("body", payload.as_dict())
        self.assertNotIn("body", payload.as_dict(include_body=False))
        self.assertEqual(payload.as_dict()["dataset"], "hrv")


class IngestWiringTest(RawStoreTestCase):
    def test_ingest_records_the_chunk_window_and_the_source(self) -> None:
        from fitbit_monitor.ingest.pipeline import Chunk

        ingest = Ingest(self.store, source="csv")
        stats = ingest.payload(
            "heart_rate",
            "heart.csv",
            '{"heart": []}',
            rows=[(T0, {"hr": 60.0})],
            chunk=Chunk("heart_rate", days=(date(2026, 5, 1), date(2026, 5, 3)), label="heart.csv"),
        )
        self.assertEqual(stats.rows_inserted, 1)
        payload = next(self.store.raw_payloads(with_body=False))
        self.assertEqual((payload.requested_start, payload.requested_end), ("2026-05-01", "2026-05-03"))
        self.assertEqual(payload.source, "csv")

    def test_a_replayed_response_is_counted_as_a_duplicate_not_a_second_row(self) -> None:
        ingest = Ingest(self.store)
        body = '{"heart": [{"dateTime": "2026-05-01T00:00", "heartRate": 60}]}'
        rows = [(T0, {"hr": 60.0})]
        first = ingest.payload("heart_rate", "p", body, rows=rows)
        second = ingest.payload("heart_rate", "p", body, rows=rows)
        self.assertEqual((first.payloads, first.payloads_duplicate), (1, 0))
        self.assertEqual((second.payloads, second.payloads_duplicate), (0, 1))
        self.assertEqual(second.rows_duplicate, 1)
        self.assertEqual(len(list(self.store.raw_payloads(with_body=False))), 1)

    def test_delete_raw_before_removes_only_old_payloads(self) -> None:
        self.store.put_raw("heart_rate", "old", T0, '{"old": 1}')
        self.store.put_raw("heart_rate", "new", T0 + timedelta(days=10), '{"new": 1}')
        rows, freed = self.store.delete_raw_before(T0 + timedelta(days=5))
        self.assertEqual((rows, freed > 0), (1, True))
        self.assertEqual([p.source_id for p in self.store.raw_payloads(with_body=False)], ["new"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
