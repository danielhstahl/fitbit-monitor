"""Store schema and the tiny migration runner.

This is the minimum the plugin system needs today - a normalized series table, a
raw-payload table so nothing ingested is thrown away, and the alert history table
that :mod:`fitbit_monitor.plugins.sinks` writes. It is deliberately small and
versioned: the storage epic (``fitb-ei6``) owns the full schema and can extend
``MIGRATIONS`` in place; readers refuse a store whose version they do not
understand rather than guessing.
"""

from __future__ import annotations

import sqlite3

from ..errors import StoreError

SCHEMA_VERSION = 2

SERIES_TABLE = "series"
RAW_TABLE = "raw_payload"
ALERTS_TABLE = "alerts"
WATERMARK_TABLE = "dataset_watermark"
RUN_TABLE = "dataset_run"
QUARANTINE_TABLE = "quarantine"

_MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        """
        CREATE TABLE IF NOT EXISTS fm_meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS raw_payload (
            dataset     TEXT NOT NULL,
            source_id   TEXT NOT NULL,
            fetched_at  TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            body        TEXT NOT NULL,
            PRIMARY KEY (dataset, content_hash)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS series (
            dataset  TEXT NOT NULL,
            ts_utc   TEXT NOT NULL,   -- ISO-8601, always UTC
            payload  TEXT NOT NULL,   -- JSON object of numeric fields
            source_id TEXT,
            PRIMARY KEY (dataset, ts_utc)
        )
        """,
        "CREATE INDEX IF NOT EXISTS series_ds_ts ON series (dataset, ts_utc)",
        """
        CREATE TABLE IF NOT EXISTS alerts (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            dedup_key        TEXT NOT NULL,
            plugin           TEXT NOT NULL,
            key              TEXT NOT NULL,
            severity         TEXT NOT NULL,
            title            TEXT NOT NULL,
            message          TEXT NOT NULL,
            confidence       REAL,
            occurred_at      TEXT NOT NULL,
            window_start     TEXT,
            window_end       TEXT,
            evidence         TEXT NOT NULL DEFAULT '[]',
            first_emitted_at TEXT NOT NULL,
            last_seen_at     TEXT NOT NULL,
            occurrences      INTEGER NOT NULL DEFAULT 1
        )
        """,
        "CREATE INDEX IF NOT EXISTS alerts_dedup ON alerts (dedup_key, last_seen_at)",
        "CREATE INDEX IF NOT EXISTS alerts_seen ON alerts (last_seen_at)",
    ),
    # Version 2 is the ad-hoc import epic (fitb-bmp): where a dataset got to,
    # what each import run did, and the payloads that were refused.
    2: (
        """
        CREATE TABLE IF NOT EXISTS dataset_watermark (
            dataset    TEXT NOT NULL,
            source     TEXT NOT NULL,
            last_day   TEXT,            -- last fully-fetched calendar day (inclusive)
            last_ts    TEXT,            -- last instant stored for the dataset
            updated_at TEXT NOT NULL,
            PRIMARY KEY (dataset, source)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS dataset_run (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id        TEXT NOT NULL,
            dataset       TEXT NOT NULL,
            source        TEXT NOT NULL,
            since         TEXT,
            until         TEXT,
            status        TEXT NOT NULL,
            chunks        INTEGER NOT NULL DEFAULT 0,
            requests      INTEGER NOT NULL DEFAULT 0,
            rows_inserted INTEGER NOT NULL DEFAULT 0,
            rows_duplicate INTEGER NOT NULL DEFAULT 0,
            quarantined   INTEGER NOT NULL DEFAULT 0,
            bytes_stored  INTEGER NOT NULL DEFAULT 0,
            duration_ms   INTEGER NOT NULL DEFAULT 0,
            watermark     TEXT,
            error         TEXT,
            started_at    TEXT NOT NULL,
            finished_at   TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS dataset_run_run ON dataset_run (run_id, dataset)",
        "CREATE INDEX IF NOT EXISTS dataset_run_started ON dataset_run (started_at DESC)",
        """
        CREATE TABLE IF NOT EXISTS quarantine (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            dataset        TEXT NOT NULL,
            source_id      TEXT,
            reason         TEXT NOT NULL,
            body           TEXT NOT NULL,
            quarantined_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS quarantine_dataset ON quarantine (dataset, quarantined_at DESC)",
    ),
}


def connect_rw(path) -> sqlite3.Connection:
    """Open a writable connection and bring the schema up to date.

    ``check_same_thread=False``: the plugin runner executes a plugin on a worker
    thread and hands it the store, and plugin execution is strictly sequential -
    one plugin at a time, main thread blocked in ``join`` - so exactly one thread
    ever touches a connection at a time. SQLite's own check is about accident, not
    safety, and this is the documented accident.
    """
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    migrate(conn)
    return conn


def connect_ro(path) -> sqlite3.Connection:
    """Open a connection that *cannot* write: SQLite enforces it, not a convention.

    ``mode=ro`` means a plugin reaching for the underlying connection still gets an
    ``OperationalError``, which is what the contract test asserts. See
    :func:`connect_rw` for why ``check_same_thread`` is off.
    """
    uri = f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations. Returns the schema version now in force."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS fm_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    row = conn.execute("SELECT value FROM fm_meta WHERE key = 'schema_version'").fetchone()
    current = int(row["value"]) if row else 0
    if current > SCHEMA_VERSION:
        raise StoreError(
            f"store schema version {current} is newer than this build knows ({SCHEMA_VERSION})",
            hint="upgrade fitbit-monitor, or point --db at a store this version wrote",
        )
    for version in range(current + 1, SCHEMA_VERSION + 1):
        for statement in _MIGRATIONS[version]:
            conn.execute(statement)
        conn.execute(
            "INSERT INTO fm_meta (key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(version),),
        )
    conn.commit()
    return SCHEMA_VERSION


def schema_version(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT value FROM fm_meta WHERE key='schema_version'").fetchone()
    return int(row["value"]) if row else None
