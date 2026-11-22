"""The synthetic corpus (fitb-ei6.5): deterministic, complete, and honestly labelled.

Three consumers depend on this generator and each one fails differently if it drifts:
development without a real account, the CI benchmark (which must not need secrets),
and the labelled anomaly fixtures Epic 6 will assert against. So the property that
matters is not "looks plausible" but **byte-identical for a given seed**, and the
episodes a detector test relies on have to be findable *and* labelled as synthetic.
"""

from __future__ import annotations

import hashlib
import time
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from fitbit_monitor.store.reader import ReadOnlyStore
from fitbit_monitor.store.seed import (
    ANOMALY_KINDS,
    DATASETS,
    episode_annotations,
    seed_store,
)
from fitbit_monitor.store.writer import SeriesStore

END = date(2026, 5, 31)


def row_digest(store: SeriesStore) -> str:
    """A hash of every generated row, in a defined order: the determinism assertion.

    The ``dataset`` registry is deliberately not in it - it records when a dataset was
    first seen, which is a wall-clock fact about the process, not corpus content.
    """
    digest = hashlib.sha256()
    for table in ("series", "dev_annotation"):
        rows = store.conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2, 3")
        for row in rows:
            digest.update(repr(tuple(row)).encode("utf-8"))
    return digest.hexdigest()


class CorpusTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "store.sqlite3"
        self.store = SeriesStore(self.path)
        self.addCleanup(self.store.close)

    def reader(self) -> ReadOnlyStore:
        store = ReadOnlyStore(self.path)
        self.addCleanup(store.close)
        return store


class DeterminismTest(CorpusTestCase):
    def test_the_same_seed_produces_byte_identical_rows(self) -> None:
        seed_store(self.store, days=30, seed=7, end=END)
        first = row_digest(self.store)
        second_store = SeriesStore(Path(self.tmp.name) / "second.sqlite3")
        self.addCleanup(second_store.close)
        seed_store(second_store, days=30, seed=7, end=END)
        self.assertEqual(first, row_digest(second_store))

    def test_a_different_seed_or_end_date_changes_the_corpus(self) -> None:
        seed_store(self.store, days=14, seed=7, end=END)
        first = row_digest(self.store)
        other = SeriesStore(Path(self.tmp.name) / "other.sqlite3")
        self.addCleanup(other.close)
        seed_store(other, days=14, seed=8, end=END)
        self.assertNotEqual(first, row_digest(other))
        third = SeriesStore(Path(self.tmp.name) / "third.sqlite3")
        self.addCleanup(third.close)
        seed_store(third, days=14, seed=7, end=date(2026, 6, 30))
        self.assertNotEqual(first, row_digest(third))

    def test_a_quarter_of_a_year_writes_in_seconds(self) -> None:
        started = time.monotonic()
        plan = seed_store(self.store, days=90, seed=1337, end=END)
        self.assertLess(time.monotonic() - started, 20.0, "the benchmark budget is not a suggestion")
        self.assertGreater(plan.points, 20_000)


class CoverageTest(CorpusTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.plan = seed_store(self.store, days=90, seed=1337, end=END)

    def test_every_dataset_is_written_over_the_whole_window(self) -> None:
        for dataset in DATASETS:
            self.assertGreater(self.store.count(dataset), 0, dataset)
        reader = self.reader()
        heart = next(info for info in reader.stored_datasets() if info.name == "heart_rate")
        self.assertEqual(heart.kind, "intraday")
        self.assertGreaterEqual(heart.days_present, 80, "90 days minus the deliberate gaps")
        self.assertEqual(heart.first_ts.date(), self.plan.start)
        self.assertEqual(heart.last_ts.date(), self.plan.end)

    def test_gaps_are_deliberate_and_reported(self) -> None:
        self.assertTrue(self.plan.dropped_days)
        days = self.store.days_with_data()
        for dropped in self.plan.dropped_days:
            self.assertNotIn(dropped, days)
        plan = seed_store(self.store, days=6, seed=3, end=END, drop_missing_days=False)
        self.assertEqual(plan.dropped_days, ())

    def test_the_day_has_overnight_dips_and_exercise_bursts(self) -> None:
        from statistics import median

        reader = self.reader()
        day_start = datetime(self.plan.start.year, self.plan.start.month, self.plan.start.day, tzinfo=UTC)
        night = [p["hr"] for p in reader.query("heart_rate", day_start, day_start + timedelta(hours=4))]
        day = [
            p["hr"]
            for p in reader.query("heart_rate", day_start + timedelta(hours=9), day_start + timedelta(hours=18))
        ]
        self.assertTrue(night and day)
        self.assertLess(median(night), median(day), "the overnight median should sit below the daytime one")

        corpus = [p["hr"] for p in reader.query("heart_rate")]
        self.assertGreater(sum(1 for value in corpus if value > 120), 200, "exercise bursts exist")
        self.assertTrue(all(36 <= value <= 191 for value in corpus))


class LabelledEpisodeTest(CorpusTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.plan = seed_store(self.store, days=90, seed=1337, end=END)

    def test_all_three_episode_shapes_are_injected(self) -> None:
        kinds = {episode.kind for episode in self.plan.episodes}
        self.assertEqual(kinds, set(ANOMALY_KINDS))
        self.assertGreaterEqual(len(self.plan.episodes), 6)

    def test_labels_are_stored_next_to_the_data(self) -> None:
        stored = episode_annotations(self.reader().conn)
        self.assertEqual(len(stored), len(self.plan.episodes))
        for record in stored:
            self.assertEqual(record["dataset"], "heart_rate")
            self.assertEqual(record["label"], record["kind"])
            self.assertEqual(record["details"]["synthetic"], True)
            self.assertGreater(record["details"]["minutes"], 0)
        self.assertEqual(episode_annotations(self.reader().conn, kind="sustained_low")[0]["kind"], "sustained_low")

    def test_a_sustained_high_episode_is_actually_sustained_and_high(self) -> None:
        episode = next(e for e in self.plan.episodes if e.kind == "sustained_high")
        values = [
            point["hr"]
            for point in self.reader().query("heart_rate", episode.start, episode.end + timedelta(minutes=5))
        ]
        self.assertGreaterEqual(len(values), 8)
        self.assertGreater(min(values), 120.0, "every reading in the window is high")
        self.assertAlmostEqual(sum(values) / len(values), episode.mean_hr, delta=1.0)
        self.assertGreaterEqual(episode.minutes, 40)

    def test_a_sustained_low_episode_sits_in_the_bradycardia_range(self) -> None:
        episode = next(e for e in self.plan.episodes if e.kind == "sustained_low")
        values = [
            point["hr"]
            for point in self.reader().query("heart_rate", episode.start, episode.end + timedelta(minutes=5))
        ]
        self.assertLess(max(values), 50.0)
        self.assertLess(episode.start.hour, 6, "and it is placed where a real one shows up: overnight")

    def test_an_irregular_episode_scatters_instead_of_levelling(self) -> None:
        episode = next(e for e in self.plan.episodes if e.kind == "irregular")
        values = [
            point["hr"]
            for point in self.reader().query("heart_rate", episode.start, episode.end + timedelta(minutes=5))
        ]
        self.assertGreater(max(values) - min(values), 40.0, "the point is the scatter")

    def test_episodes_can_be_turned_off_or_counted_exactly(self) -> None:
        clean = SeriesStore(Path(self.tmp.name) / "clean.sqlite3")
        self.addCleanup(clean.close)
        plan = seed_store(clean, days=90, seed=1337, end=END, anomalies=0)
        self.assertEqual(plan.episodes, ())
        self.assertEqual(episode_annotations(clean.conn), [])

        two = SeriesStore(Path(self.tmp.name) / "two.sqlite3")
        self.addCleanup(two.close)
        plan = seed_store(two, days=90, seed=1337, end=END, anomalies=2)
        self.assertEqual(len(plan.episodes), 2)

    def test_seeding_twice_does_not_duplicate_labels(self) -> None:
        seed_store(self.store, days=90, seed=1337, end=END)
        self.assertEqual(len(episode_annotations(self.store.conn)), len(self.plan.episodes))


class RejectsNonsenseTest(unittest.TestCase):
    def test_zero_days_is_still_refused(self) -> None:
        with TemporaryDirectory() as tmp:
            store = SeriesStore(Path(tmp) / "s.sqlite3")
            self.addCleanup(store.close)
            with self.assertRaises(ValueError):
                seed_store(store, days=0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
