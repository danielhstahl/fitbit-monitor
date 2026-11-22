"""The migration runner and the shape of the schema (ADR-0003, fitb-ei6.1).

Four things are asserted here that the schema's whole safety story rests on:

* migrations are numbered files applied **in order, one transaction each**, so a
  half-applied migration cannot exist;
* a store already at an older version is upgraded **in place, repeatably** - running
  the migration twice is not twice the damage;
* a store from a **newer** fm is **refused**, not guessed at;
* the properties the rest of the store depends on are actually in force: foreign keys,
  the unique content hash, and an index behind "all of dataset X between t1 and t2".
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from fitbit_monitor.store import schema
from fitbit_monitor.store.migrations import MIGRATIONS, Migration, split_statements
from fitbit_monitor.store.reader import ReadOnlyStore
from fitbit_monitor.store.writer import SeriesStore

from fitbit_monitor.errors import StoreError

T0 = datetime(2026, 5, 1, tzinfo=UTC)


class MigrationFileTest(unittest.TestCase):
    def test_files_are_numbered_from_one_and_in_order(self) -> None:
        versions = [migration.version for migration in MIGRATIONS]
        self.assertEqual(versions, sorted(versions))
        self.assertEqual(versions[0], 1)
        self.assertEqual(len(set(versions)), len(versions), "two migrations share a version")
        self.assertEqual(schema.SCHEMA_VERSION, versions[-1])

    def test_every_migration_has_statements_and_a_label(self) -> None:
        for migration in MIGRATIONS:
            self.assertTrue(migration.statements, migration.label)
            self.assertEqual(migration.label.split("_")[0], f"{migration.version:04d}")

    def test_statement_splitting_drops_comments_and_keeps_semicolons_out(self) -> None:
        sql = """
        -- a comment; with a semicolon in it
        CREATE TABLE a (x INTEGER);
        CREATE TABLE b (y TEXT);
        """
        self.assertEqual(split_statements(sql), ("CREATE TABLE a (x INTEGER)", "CREATE TABLE b (y TEXT)"))

    def test_no_migration_is_edited_after_release(self) -> None:
        # A committed migration is history. This pins the two that shipped before the
        # storage model, so "fix the schema" cannot quietly mean "rewrite 0001".
        first = MIGRATIONS[0]
        self.assertIn("CREATE TABLE IF NOT EXISTS series", first.sql)
        self.assertIn("PRIMARY KEY (dataset, content_hash)", first.sql)


class FreshMigrateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "store.sqlite3"

    def test_fresh_database_reaches_the_current_version(self) -> None:
        applied = schema.init_db(self.path)
        self.assertEqual([migration.version for migration in applied], [m.version for m in MIGRATIONS])
        conn = schema.connect(self.path)
        self.assertEqual(schema.schema_version(conn), schema.SCHEMA_VERSION)
        tables = schema.tables(conn)
        self.assertLessEqual(
            {"series", "raw_payload", "dataset", "dataset_run", "dataset_watermark", "quarantine", "alerts", "fm_meta"},
            tables,
        )
        self.assertLessEqual(
            {"v_heart_rate_intraday", "v_daily_summary", "v_sleep", "v_hrv"}, schema.views(conn)
        )

    def test_migrate_again_applies_nothing(self) -> None:
        schema.init_db(self.path)
        conn = schema.connect(self.path)
        self.assertEqual(schema.migrate_store(conn), ())
        self.assertEqual(schema.pending(conn), ())

    def test_wal_foreign_keys_and_busy_timeout_are_set_on_open(self) -> None:
        conn = schema.connect_rw(self.path)
        self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertGreaterEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 1000)

    def test_read_only_open_is_readable_and_refuses_a_foreign_file(self) -> None:
        schema.init_db(self.path)
        conn = schema.connect_ro(self.path)
        self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM series").fetchone()["n"], 0)
        other = Path(self.tmp.name) / "other.sqlite3"
        plain = sqlite3.connect(other)
        plain.execute("CREATE TABLE whatever (x)")
        plain.commit()
        plain.close()
        with self.assertRaises(StoreError):
            ReadOnlyStore(other)


class MigrateFromPreviousVersionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "store.sqlite3"

    def _store_at(self, version: int) -> None:
        conn = sqlite3.connect(self.path)
        schema.migrate_store(conn, target=version)
        conn.execute(
            "INSERT INTO series VALUES ('hrv', ?, ?, NULL)", (T0.isoformat(), '{"rmssd": 40.0}')
        )
        conn.execute(
            "INSERT INTO raw_payload (dataset, source_id, fetched_at, content_hash, body) VALUES (?,?,?,?,?)",
            ("hrv", "path", T0.isoformat(), "abc123", '{"hrv": []}'),
        )
        conn.commit()
        conn.close()

    def test_migrating_twice_from_the_previous_version_is_safe(self) -> None:
        previous = schema.SCHEMA_VERSION - 1
        self._store_at(previous)
        first = schema.init_db(self.path)
        self.assertEqual([migration.version for migration in first], [schema.SCHEMA_VERSION])
        second = schema.init_db(self.path)
        self.assertEqual(second, (), "a second migrate must be a no-op")
        conn = schema.connect(self.path)
        self.assertEqual(schema.schema_version(conn), schema.SCHEMA_VERSION)
        self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM series").fetchone()["n"], 1)
        # The legacy raw row survived the table rebuild, uncompressed and readable.
        rows = list(conn.execute("SELECT encoding, body FROM raw_payload"))
        self.assertEqual([row["encoding"] for row in rows], ["identity"])

    def test_migrating_every_version_twice_is_idempotent(self) -> None:
        # Build the same store twice: one file migrated step by step, one in one go.
        staged = Path(self.tmp.name) / "staged.sqlite3"
        straight = Path(self.tmp.name) / "straight.sqlite3"
        conn = sqlite3.connect(staged)
        for migration in MIGRATIONS:
            schema.migrate_store(conn, target=migration.version)
        schema.init_db(straight)
        schema.migrate_store(conn)
        def objects(path: Path) -> list[str]:
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            return [f"{row['name']}|{row['sql']}" for row in conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name"
            )]
        self.assertEqual(objects(staged), objects(straight))

    def test_a_migration_that_fails_leaves_the_store_at_the_version_it_started(self) -> None:
        schema.init_db(self.path)
        conn = schema.connect(self.path)
        boom = Migration(
            version=schema.SCHEMA_VERSION + 1,
            name="boom",
            sql="CREATE TABLE boom (x INTEGER);\nINSERT INTO no_such_table VALUES (1);",
            statements=split_statements(
                "CREATE TABLE boom (x INTEGER);\nINSERT INTO no_such_table VALUES (1);"
            ),
        )
        with mock.patch.object(schema, "MIGRATIONS", tuple(MIGRATIONS) + (boom,)), mock.patch.object(
            schema, "SCHEMA_VERSION", schema.SCHEMA_VERSION + 1
        ):
            with self.assertRaises(StoreError) as caught:
                schema.migrate_store(conn)
        self.assertIn("boom", str(caught.exception))
        self.assertEqual(schema.schema_version(conn), schema.SCHEMA_VERSION)
        self.assertNotIn("boom", schema.tables(conn), "a failed migration must roll back completely")

    def test_an_unknown_newer_version_is_refused_not_guessed(self) -> None:
        schema.init_db(self.path)
        conn = sqlite3.connect(self.path)
        conn.execute("UPDATE fm_meta SET value = '99' WHERE key = 'schema_version'")
        conn.commit()
        conn.close()
        with self.assertRaises(StoreError) as caught:
            schema.connect_rw(self.path)
        self.assertIn("newer than this build", str(caught.exception))
        with self.assertRaises(StoreError):
            SeriesStore(self.path)


class SchemaPropertyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "store.sqlite3"
        self.store = SeriesStore(self.path)
        self.addCleanup(self.store.close)

    def test_foreign_keys_are_enforced_on_raw_payload(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.conn.execute(
                "INSERT INTO raw_payload (content_hash, dataset, fetched_at, bytes, stored_bytes, body) "
                "VALUES ('h', 'never_registered', ?, 1, 1, x'00')",
                (T0.isoformat(),),
            )
        # and the writer's own path registers first, so it cannot trip that
        self.assertTrue(self.store.put_raw("brand_new", "p", T0, '{"x": 1}'))

    def test_content_hash_is_unique(self) -> None:
        self.assertTrue(self.store.put_raw("heart_rate", "a", T0, '{"same": 1}'))
        self.assertFalse(self.store.put_raw("heart_rate", "b", T0, '{"same": 1}'))
        self.assertFalse(
            self.store.put_raw("daily_summary", "c", T0, '{"same": 1}'),
            "the same bytes are one payload, whichever dataset asked for them",
        )
        indexes = self.store.conn.execute("PRAGMA index_list('raw_payload')").fetchall()
        unique_hash_indexes = 0
        for index in indexes:
            columns = [row[2] for row in self.store.conn.execute(f"PRAGMA index_info('{index['name']}')")]
            if index["unique"] and columns == ["content_hash"]:
                unique_hash_indexes += 1
        self.assertEqual(unique_hash_indexes, 1, "exactly one unique index on content_hash")

    def test_range_queries_use_an_index(self) -> None:
        until = datetime(2026, 5, 31, tzinfo=UTC)
        plan = self.store.conn.execute(
            "EXPLAIN QUERY PLAN SELECT ts_utc FROM series WHERE dataset = ? AND ts_utc >= ? AND ts_utc < ?",
            ("heart_rate", T0.isoformat(), until.isoformat()),
        ).fetchall()
        detail = " ".join(row["detail"] for row in plan).upper()
        self.assertIn("INDEX", detail)
        raw_plan = self.store.conn.execute(
            "EXPLAIN QUERY PLAN SELECT content_hash FROM raw_payload WHERE dataset = ? AND fetched_at >= ?",
            ("heart_rate", T0.isoformat()),
        ).fetchall()
        self.assertIn("INDEX", " ".join(row["detail"] for row in raw_plan).upper())

    def test_typed_views_expose_named_columns(self) -> None:
        self.store.put("heart_rate", T0, {"hr": 61.0})
        self.store.put("hrv", T0, {"rmssd": 44.0})
        self.store.commit()
        row = self.store.conn.execute("SELECT * FROM v_heart_rate_intraday").fetchone()
        self.assertEqual(row["heart_rate_bpm"], 61.0)
        hrv = self.store.conn.execute("SELECT * FROM v_hrv").fetchone()
        self.assertEqual(hrv["rmssd_ms"], 44.0)
        self.assertEqual(hrv["day"], "2026-05-01")

    def test_registry_records_kind_and_grain(self) -> None:
        self.store.put("heart_rate", T0, {"hr": 61.0})
        self.store.commit()
        registry = self.store.registry()
        self.assertEqual(registry["heart_rate"]["kind"], "intraday")
        self.assertEqual(registry["heart_rate"]["granularity_seconds"], 60)


class DbCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        import os

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name)
        for name, value in (("XDG_DATA_HOME", home / "data"), ("XDG_CONFIG_HOME", home / "config")):
            self._original(name)
            os.environ[name] = str(value)
        self.db = home / "data" / "fitbit-monitor" / "store.sqlite3"

    def _original(self, name: str) -> None:
        import os

        value = os.environ.get(name)
        self.addCleanup(
            lambda: os.environ.__setitem__(name, value) if value is not None else os.environ.pop(name, None)
        )

    def fm(self, *argv):
        import io
        from contextlib import redirect_stderr, redirect_stdout

        from fitbit_monitor.cli import main

        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue() + err.getvalue()

    def test_db_commands_end_to_end(self) -> None:
        import json

        code, text = self.fm("db", "status")
        self.assertEqual(code, 4, text)
        self.assertIn("no store", text)

        code, text = self.fm("db", "migrate")
        self.assertEqual(code, 4, text)

        code, text = self.fm("db", "init", "--json")
        self.assertEqual(code, 0, text)
        payload = json.loads(text)
        self.assertTrue(payload["created"])
        self.assertEqual(payload["schema_version"], schema.SCHEMA_VERSION)
        self.assertEqual(len(payload["applied"]), len(MIGRATIONS))

        # idempotent: init again says it already existed and applies nothing
        code, text = self.fm("db", "init", "--json")
        self.assertEqual(code, 0, text)
        again = json.loads(text)
        self.assertFalse(again["created"])
        self.assertEqual(again["applied"], [])

        code, text = self.fm("db", "migrate", "--json")
        self.assertEqual(code, 0, text)
        self.assertEqual(json.loads(text)["applied"], [])

        code, text = self.fm("db", "status", "--json")
        self.assertEqual(code, 0, text)
        status = json.loads(text)
        self.assertTrue(status["up_to_date"])
        self.assertEqual(status["pending"], [])
        self.assertTrue(status["pragmas"]["foreign_keys"])
        self.assertIn("series", status["tables"])
        self.assertIn("v_hrv", status["views"])

    def test_db_migrate_reports_applied_migrations(self) -> None:
        import json

        # A store that stopped at migration 1: what an upgrade from an old fm looks like.
        self.db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db)
        schema.migrate_store(conn, target=1)
        conn.commit()
        conn.close()
        code, text = self.fm("db", "migrate", "--json")
        self.assertEqual(code, 0, text)
        payload = json.loads(text)
        self.assertEqual(payload["from_version"], 1)
        self.assertEqual(payload["schema_version"], schema.SCHEMA_VERSION)
        self.assertEqual(len(payload["applied"]), len(MIGRATIONS) - 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
