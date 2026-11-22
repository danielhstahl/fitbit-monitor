"""Tests for the CSV / Takeout export source (``fm import --source csv``).

The archive on disk under ``tests/fixtures/export/`` is the same shape as a real
Google Takeout: a directory (or ZIP) with ``activities/``, ``hrv/``, ``sleep/`` and
friends inside, comma-separated, no timezone anywhere. Tests cover the per-file parsers,
the *reasons* files get skipped, the window filter, idempotence, and the whole path
through the ingest layer.
"""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path

from fitbit_monitor.ingest.csv_source import (
    PARSERS,
    CsvImport,
    find_parser,
    iter_archive,
    parse_daily_summary,
    parse_export,
    parse_hrv,
    parse_heart,
    parse_member,
    parse_sleep,
    summarise_files,
)
from fitbit_monitor.ingest.pipeline import Ingest
from fitbit_monitor.store.watermarks import Watermarks
from fitbit_monitor.store.writer import SeriesStore

REPO = Path(__file__).resolve().parents[1]
EXPORT = REPO / "tests" / "fixtures" / "export"
SAMPLE = EXPORT / "takeout-sample"
MESSY = EXPORT / "messy"


def make_zip(source_dir: Path, target_dir: Path, name: str = "export.zip") -> Path:
    """Zip a fixture directory the way Fitbit ships it (``Takeout/...`` prefix)."""
    path = target_dir / name
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for member in sorted(source_dir.rglob("*.csv")):
            archive.write(member, f"Takeout/{member.relative_to(source_dir).as_posix()}")
    return path


class ParserTest(unittest.TestCase):
    def test_heart_rate_csv_is_minutes_per_row_in_bpm(self) -> None:
        text = (SAMPLE / "activities" / "HeartRate_2026-05-01.csv").read_text()
        rows, bad = parse_heart(text, "UTC", "activities/HeartRate_2026-05-01.csv")
        self.assertEqual(bad, 0)
        self.assertEqual([ts for ts, _ in rows][0], datetime(2026, 5, 1, 0, 0, tzinfo=UTC))
        self.assertEqual(rows[3][1], {"hr": 121.0})

    def test_heart_rate_accepts_an_unsplit_datetime_column(self) -> None:
        text = "Min,DateTime\n58,2026-05-01T07:30:00\n"
        rows, bad = parse_heart(text, "UTC", "heart/HeartRate_2026-05-01.csv")
        self.assertEqual((bad, len(rows)), (0, 1))
        self.assertEqual(rows[0][0], datetime(2026, 5, 1, 7, 30, tzinfo=UTC))

    def test_heart_rate_rejects_impossible_values(self) -> None:
        rows, bad = parse_heart("Min,DateTime\n0,2026-05-01,00:00:00\n400,2026-05-01,00:01:00\n", "UTC", "x")
        self.assertEqual((rows, bad), ([], 2))

    def test_hrv_value_is_rmssd_milliseconds_on_a_local_date(self) -> None:
        text = (SAMPLE / "hrv" / "HRV_2026-05-01.csv").read_text()
        rows, bad = parse_hrv(text, "UTC", "hrv/HRV_2026-05-01.csv")
        self.assertEqual(bad, 0)
        self.assertEqual(rows[0], (datetime(2026, 5, 1, tzinfo=UTC), {"rmssd": 45.5}))

    def test_hrv_falls_back_to_the_file_name_date(self) -> None:
        rows, bad = parse_hrv("Hrv Value\n51.0\n", "UTC", "hrv/HRV_2026-05-04.csv")
        self.assertEqual((bad, rows[0][0].date()), (0, date(2026, 5, 4)))

    def test_sleep_vertical_field_value_format(self) -> None:
        text = (SAMPLE / "sleep" / "Sleep_2026-05-01.csv").read_text()
        rows, bad = parse_sleep(text, "UTC", "sleep/Sleep_2026-05-01.csv")
        self.assertEqual(bad, 0)
        ts, values = rows[0]
        self.assertEqual(ts, datetime(2026, 5, 1, 23, 5, tzinfo=UTC))
        self.assertEqual(values["sleep_minutes"], 425.0)
        self.assertEqual(values["deep_minutes"], 84.0)

    def test_sleep_horizontal_log_format(self) -> None:
        text = (SAMPLE / "sleep" / "Sleep-log.csv").read_text()
        rows, bad = parse_sleep(text, "UTC", "sleep/Sleep-log.csv")
        self.assertEqual((bad, len(rows)), (0, 1))
        self.assertEqual(rows[0][1]["awake_minutes"], 26.0)

    def test_daily_summary_columns_are_mapped(self) -> None:
        text = (SAMPLE / "activities" / "Daily_Summary.csv").read_text()
        rows, bad = parse_daily_summary(text, "UTC", "activities/Daily_Summary.csv")
        self.assertEqual(bad, 0)
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0][1]["steps"], 8452.0)
        self.assertEqual(rows[0][1]["resting_hr"], 56.0)

    def test_timezone_setting_moves_the_wall_clock(self) -> None:
        text = "Min,DateTime\n52,2026/05/01,00:00:00\n"
        utc = parse_heart(text, "UTC", "x")[0][0][0]
        berlin = parse_heart(text, "Europe/Berlin", "x")[0][0][0]
        self.assertEqual(utc, datetime(2026, 5, 1, tzinfo=UTC))
        self.assertEqual(berlin, datetime(2026, 4, 30, 22, tzinfo=UTC))

    def test_every_documented_parser_declares_units_and_timezone(self) -> None:
        for parser in PARSERS:
            self.assertTrue(parser.units, parser.kind)
            self.assertIn("local", parser.timezone_note, parser.kind)


class FileSelectionTest(unittest.TestCase):
    def test_known_files_match_their_parser(self) -> None:
        for name, kind in (
            ("Takeout/activities/HeartRate_2026-05-01.csv", "heart_rate"),
            ("Takeout/hrv/HRV_2026-05-01.csv", "hrv"),
            ("Takeout/sleep/Sleep-log.csv", "sleep"),
            ("Takeout/activities/Daily_Summary.csv", "daily_summary"),
        ):
            parser = find_parser(name)
            self.assertIsNotNone(parser, name)
            self.assertEqual(parser.kind, kind)  # type: ignore[union-attr]

    def test_unknown_and_declined_files_get_a_named_reason(self) -> None:
        parsed = [parse_member(name, "a,b\n1,2\n") for name in ("user/user_profile.csv", "body/Weight.csv", "x.csv")]
        reasons = " ".join(p.skipped or "" for p in parsed)
        self.assertIn("no parser", reasons)
        self.assertIn("body weight", reasons)

    def test_a_flat_file_is_recognised_from_its_header(self) -> None:
        """People unpack the archive and rename things; the header says what a file is."""
        cases = (
            ("hrv.csv", "Date Time,Hrv Value,Hrv Length\n2026-05-01,45.5,05:12:00\n", "hrv"),
            (
                "Sleep-log.csv",
                "Date of Sleep,Fall Asleep,Wake Up,Time in Bed,Time Asleep\n2026/05/02,2026/05/02 22:48:00,2026/05/03 06:20:00,452,412\n",
                "sleep",
            ),
            ("Daily_Summary.csv", "Date,Steps,Calories\n2026/05/01,8452,2480\n", "daily_summary"),
            ("HeartRate_2026-05-01.csv", "Min,DateTime\n52,2026/05/01,00:00:00\n", "heart_rate"),
        )
        for name, text, kind in cases:
            with self.subTest(file=name):
                self.assertEqual(find_parser(name, text).kind, kind)  # type: ignore[union-attr]
                self.assertEqual(find_parser(name), None, "the name alone says nothing")
                self.assertEqual(len(parse_member(name, text).rows), 1)

    def test_a_declined_file_stays_declined_however_its_header_looks(self) -> None:
        """The intraday activity log has a Steps column; importing it as a daily
        summary would count the same steps twice."""
        parsed = parse_member("activities/activities_2026-05-01.csv", "Date,Duration,Steps\n2026-05-01,600000,1500\n")
        self.assertIsNone(parsed.kind)
        self.assertIn("intraday activity log", parsed.skipped or "")

    def test_file_filters_select_members(self) -> None:
        names = {f.name for f in parse_export(SAMPLE, only=("hrv/*.csv",))}
        self.assertTrue(names)
        self.assertTrue(all(name.startswith("hrv/") or "/hrv/" in name for name in names))
        excluded = {f.name for f in parse_export(SAMPLE, exclude=("sleep/*",))}
        self.assertFalse(any(name.startswith("sleep/") for name in excluded))
        self.assertLess(len(excluded), len(parse_export(SAMPLE)))

    def test_export_directory_is_walked(self) -> None:
        files = parse_export(SAMPLE)
        names = {f.name for f in files}
        self.assertIn("activities/Daily_Summary.csv", names)
        self.assertEqual(len(files), len(list(iter_archive(SAMPLE))))

    def test_zip_archive_is_read_the_same_way(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = make_zip(SAMPLE, Path(tmp))
            from_dir = {f.name: len(f.rows) for f in parse_export(SAMPLE)}
            from_zip = {f.name.replace("Takeout/", ""): len(f.rows) for f in parse_export(archive)}
        self.assertEqual(from_dir["activities/Daily_Summary.csv"], from_zip["activities/Daily_Summary.csv"])

    def test_unselected_datasets_are_skipped_with_a_reason(self) -> None:
        files = parse_export(SAMPLE, datasets=["sleep"])
        self.assertTrue(all(f.skipped for f in files if f.kind and f.kind != "sleep"))

    def test_window_drops_rows_outside_the_range(self) -> None:
        files = parse_export(SAMPLE, window=(date(2026, 5, 1), date(2026, 5, 2)))
        summary = next(f for f in files if f.name.endswith("Daily_Summary.csv"))
        self.assertEqual(len(summary.rows), 2)
        self.assertEqual(summary.rows_out_of_range, 2)


class CsvIngestTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = SeriesStore(self.root / "store.sqlite3")
        self.addCleanup(self.store.close)
        self.ingest = Ingest(self.store, source="csv")
        self.watermarks = Watermarks(self.store)

    def run_csv(self, path: Path, **kwargs):
        return CsvImport(ingest=self.ingest, watermarks=self.watermarks, timezone="UTC").run(path, **kwargs)

    def test_sample_archive_lands_rows_in_every_dataset(self) -> None:
        report = self.run_csv(SAMPLE)
        self.assertEqual(report.status, "ok", report.warnings)
        counts = {d.dataset: d.rows_inserted for d in report.datasets}
        self.assertEqual(counts["heart_rate"], 7)
        self.assertEqual(counts["daily_summary"], 4)
        self.assertEqual(counts["hrv"], 2)
        self.assertEqual(counts["sleep"], 2)
        self.assertEqual(self.store.count("heart_rate"), 7)

    def test_importing_the_same_archive_twice_changes_nothing(self) -> None:
        first = self.run_csv(SAMPLE)
        total_after_first = self.store.count()
        rows_after_first = {d.dataset: d.rows_inserted for d in first.datasets}
        second = self.run_csv(SAMPLE)
        self.assertEqual(self.store.count(), total_after_first, "a re-import must not add rows")
        self.assertEqual(sum(d.rows_inserted for d in second.datasets), 0)
        self.assertTrue(all(d.rows_duplicate > 0 for d in second.datasets))
        self.assertTrue(sum(d.payloads_duplicate for d in second.datasets) > 0, "raw payloads dedupe too")
        self.assertEqual(rows_after_first["heart_rate"], 7)

    def test_zip_and_directory_give_the_same_result(self) -> None:
        from_directory = self.run_csv(SAMPLE)
        archive = make_zip(SAMPLE, self.root)
        # a fresh store, so "the same result" means the same *new* rows, not dedup
        other = SeriesStore(self.root / "zip-store.sqlite3")
        self.addCleanup(other.close)
        from_zip = CsvImport(ingest=Ingest(other, source="csv"), timezone="UTC").run(archive)
        self.assertEqual(
            {d.dataset: d.rows_inserted for d in from_directory.datasets},
            {d.dataset: d.rows_inserted for d in from_zip.datasets},
        )

    def test_skip_reasons_are_reported_not_swallowed(self) -> None:
        report = self.run_csv(SAMPLE)
        notes = " ".join(report.notes)
        self.assertIn("body weight", notes)
        self.assertIn("intraday activity log", notes)
        self.assertIn("no parser", notes)

    def test_bad_rows_are_counted_and_named(self) -> None:
        report = self.run_csv(MESSY)
        heart = report.dataset("heart_rate")
        self.assertIsNotNone(heart)
        self.assertEqual(heart.rows_inserted, 1)
        self.assertEqual(heart.quarantined, 3)
        self.assertTrue(heart.notes, "the file with bad rows must be named")

    def test_watermarks_record_the_last_day_ingested(self) -> None:
        self.run_csv(SAMPLE)
        mark = self.watermarks.get("daily_summary", "csv")
        self.assertEqual(mark.last_day, date(2026, 5, 11))
        self.assertIsNone(self.watermarks.get("daily_summary", "api"), "sources keep separate watermarks")

    def test_since_and_until_filter_the_archive(self) -> None:
        report = self.run_csv(SAMPLE, since=date(2026, 5, 1), until=date(2026, 5, 2))
        self.assertEqual(report.dataset("daily_summary").rows_inserted, 2)
        self.assertEqual(report.dataset("heart_rate").rows_inserted, 7)

    def test_per_file_row_counts_are_in_the_report(self) -> None:
        report = self.run_csv(SAMPLE)
        files = report.dataset("heart_rate").files
        self.assertEqual(len(files), 2)
        for entry in files:
            self.assertRegex(entry, r"^.*: \d+ row\(s\)$")
        self.assertIn("activities/HeartRate_2026-05-01.csv: 4 row(s)", files)

    def test_dataset_selection_narrows_the_import(self) -> None:
        report = self.run_csv(SAMPLE, datasets=["sleep"])
        self.assertEqual([d.dataset for d in report.datasets], ["sleep"])

    def test_archive_with_nothing_importable_fails_loudly(self) -> None:
        empty = self.root / "empty"
        empty.mkdir()
        (empty / "notes.csv").write_text("a,b\n1,2\n")
        report = self.run_csv(empty)
        self.assertEqual(report.status, "failed")
        self.assertEqual(report.exit_code, 1)

    def test_missing_archive_is_a_usage_error(self) -> None:
        from fitbit_monitor.errors import UsageError

        with self.assertRaises(UsageError):
            self.run_csv(self.root / "nope.zip")

    def test_summarise_files_shape(self) -> None:
        rows = summarise_files(parse_export(SAMPLE))
        self.assertIn("rows", rows[0])
        self.assertIn("skipped", rows[0])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
