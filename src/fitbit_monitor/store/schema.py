"""Schema access: opening the database, and the migration runner.

The schema itself lives in numbered ``.sql`` files in
:mod:`fitbit_monitor.store.migrations` - this module is only the runner and the two
connection factories, so there is one place that decides how the store is opened
(WAL, foreign keys on, a busy timeout) and one place that decides what "up to date"
means.

Every database open goes through here:

* ``connect_rw`` - writable, migrates on open, and refuses a store whose recorded
  schema version is newer than this build knows.
* ``connect_ro`` - ``mode=ro`` plus ``query_only``, which is how the plugin contract's
  "read-only" is enforced by SQLite rather than by manners.

``fm db init`` and ``fm db migrate`` call :func:`migrate` explicitly; ordinary
commands get it on open, so a store written by an older fm upgrades itself before
anything reads it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from ..errors import StoreError
from .migrations import LATEST_VERSION, MIGRATIONS, Migration

#: Version this build of fm knows how to write.
SCHEMA_VERSION = LATEST_VERSION

SERIES_TABLE = "series"
RAW_TABLE = "raw_payload"
ALERTS_TABLE = "alerts"
WATERMARK_TABLE = "dataset_watermark"
RUN_TABLE = "dataset_run"
QUARANTINE_TABLE = "quarantine"
DATASET_TABLE = "dataset"
ANNOTATION_TABLE = "dev_annotation"
META_TABLE = "fm_meta"

#: How long a writer waits for another writer's lock before giving up. The advisory
#: flock in :func:`fitbit_monitor.store.writer.write_lock` keeps two ``fm`` commands
#: out of the same store; this is the backstop for anything that bypasses it.
BUSY_TIMEOUT_MS = 5000

_META_SQL = f"CREATE TABLE IF NOT EXISTS {META_TABLE} (key TEXT PRIMARY KEY, value TEXT NOT NULL)"


def connect(path: Path | str, *, readonly: bool = False) -> sqlite3.Connection:
    """Open the store with this project's pragmas applied.

    ``check_same_thread=False``: the plugin runner executes a plugin on a worker
    thread and hands it the store, and plugin execution is strictly sequential - one
    plugin at a time, main thread blocked in ``join`` - so exactly one thread ever
    touches a connection at a time. SQLite's own check is about accident, not safety,
    and this is the documented accident.
    """
    path = Path(path)
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    else:
        conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    if readonly:
        conn.execute("PRAGMA query_only=ON")
    return conn


def connect_rw(path: Path | str, *, migrate: bool = True) -> sqlite3.Connection:
    """Open a writable connection and, unless told otherwise, bring it up to date."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    if migrate:
        migrate_store(conn)
    return conn


def connect_ro(path: Path | str) -> sqlite3.Connection:
    """Open a connection that *cannot* write: SQLite enforces it, not a convention.

    ``mode=ro`` means a plugin reaching for the underlying connection still gets an
    ``OperationalError``, which is what the contract test asserts.
    """
    return connect(path, readonly=True)


def schema_version(conn: sqlite3.Connection) -> int | None:
    """The recorded version, or ``None`` when the file has no fm schema at all."""
    try:
        row = conn.execute(f"SELECT value FROM {META_TABLE} WHERE key = 'schema_version'").fetchone()
    except sqlite3.OperationalError as exc:
        raise StoreError(f"not a fitbit-monitor store ({exc})") from exc
    # Indexed, not by name: a caller may hand us a connection it opened itself.
    return int(row[0]) if row else None


def read_version(conn: sqlite3.Connection) -> int | None:
    """Like :func:`schema_version`, but tolerant of a database with no fm tables."""
    try:
        return schema_version(conn)
    except (StoreError, sqlite3.Error):
        return None


def pending(conn: sqlite3.Connection, *, target: int | None = None) -> tuple[Migration, ...]:
    """The migrations that would be applied by :func:`migrate_store` right now."""
    target = SCHEMA_VERSION if target is None else target
    current = read_version(conn) or 0
    return tuple(m for m in MIGRATIONS if current < m.version <= target)


def migrate_store(conn: sqlite3.Connection, *, target: int | None = None) -> tuple[Migration, ...]:
    """Apply pending migrations in order, one transaction each.

    Returns the migrations that were applied (empty when the store is already current).

    Raises :class:`StoreError` when the database records a version this build does not
    know - refusing to guess is the only safe answer when the alternative is writing
    into a schema newer than the code doing the writing.
    """
    target = SCHEMA_VERSION if target is None else target
    if target > SCHEMA_VERSION:  # pragma: no cover - only reachable from tests/typos
        raise StoreError(f"cannot migrate to schema version {target}; this build knows {SCHEMA_VERSION}")
    conn.execute(_META_SQL)
    current = schema_version(conn) or 0
    if current > SCHEMA_VERSION:
        raise StoreError(
            f"store schema version {current} is newer than this build knows ({SCHEMA_VERSION})",
            hint="upgrade fitbit-monitor, or point --db at a store this version wrote",
        )
    queue = tuple(m for m in MIGRATIONS if current < m.version <= target)
    if not queue:
        conn.commit()
        return ()

    # A rebuild (0003 renames and re-creates raw_payload) must not be checked against
    # half-finished references; SQLite cannot change this pragma inside a transaction.
    was_enabled = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.execute("PRAGMA foreign_keys=OFF")
    applied: list[Migration] = []
    try:
        for migration in queue:
            conn.commit()  # close any implicit transaction before taking the write lock
            conn.execute("BEGIN IMMEDIATE")
            for statement in migration.statements:
                conn.execute(statement)
            conn.execute(
                f"INSERT INTO {META_TABLE} (key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(migration.version),),
            )
            conn.execute("COMMIT")
            applied.append(migration)
    except sqlite3.Error as exc:
        conn.rollback()
        raise StoreError(
            f"migration {migration.label} failed: {exc}",
            hint="the store is still at the version it was at before this run; fix and re-run `fm db migrate`",
        ) from exc
    finally:
        conn.execute(f"PRAGMA foreign_keys={'ON' if was_enabled else 'OFF'}")
    conn.commit()
    return tuple(applied)


#: Backwards-compatible name used by the writer and older call sites.
migrate = migrate_store


def init_db(path: Path | str) -> tuple[Migration, ...]:
    """Create the store if it is missing and apply pending migrations."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        return migrate_store(conn)
    finally:
        conn.close()


def inspect(conn: sqlite3.Connection) -> dict[str, Any]:
    """What ``fm db status`` prints: version, objects, and pragmas that matter."""
    version = read_version(conn)
    objects: dict[str, list[str]] = {"tables": [], "views": [], "indexes": []}
    kind_map = {"table": "tables", "view": "views", "index": "indexes"}
    for row in conn.execute("SELECT type, name FROM sqlite_master ORDER BY type, name"):
        bucket = kind_map.get(row[0])
        if bucket and not row[1].startswith("sqlite_"):
            objects[bucket].append(row[1])
    pragmas = {
        "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0],
        "foreign_keys": bool(conn.execute("PRAGMA foreign_keys").fetchone()[0]),
        "busy_timeout": conn.execute("PRAGMA busy_timeout").fetchone()[0],
    }
    return {
        "schema_version": version,
        "schema_version_known": SCHEMA_VERSION,
        "up_to_date": version is not None and version >= SCHEMA_VERSION,
        "pragmas": pragmas,
        **objects,
    }


def tables(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        if not row[0].startswith("sqlite_")
    }


def views(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'view'")}


__all__ = [
    "BUSY_TIMEOUT_MS",
    "MIGRATIONS",
    "SCHEMA_VERSION",
    "connect",
    "connect_ro",
    "connect_rw",
    "inspect",
    "init_db",
    "migrate",
    "migrate_store",
    "pending",
    "read_version",
    "schema_version",
    "tables",
    "views",
]
