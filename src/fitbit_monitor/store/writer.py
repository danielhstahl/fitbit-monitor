"""The writable side of the store: schema creation and series upserts.

Only the host uses this. Ingest (when it lands), the seeding helper and the alert
history table write through here; plugins never see a :class:`SeriesStore`.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Mapping

from ..errors import StoreError
from ..timeutil import ensure_utc
from . import schema


def _iso(value: datetime) -> str:
    return ensure_utc(value, field="ts").isoformat()


class SeriesStore:
    """Writable handle on the SQLite store."""

    def __init__(self, path: Path | str, conn: sqlite3.Connection | None = None) -> None:
        self.path = Path(path)
        self._own_conn = conn is None
        if conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = schema.connect_rw(self.path)
        self.conn = conn

    # --- lifecycle ----------------------------------------------------------
    @classmethod
    def open(cls, path: Path | str, *, create: bool = True) -> "SeriesStore":
        path = Path(path)
        if not create and not path.exists():
            raise StoreError(f"store not found: {path}", hint="create it with `fm dev seed --days 30`")
        return cls(path)

    def close(self) -> None:
        """Commit anything pending, then close. A writer handle never drops data."""
        if self._own_conn:
            try:
                self.conn.commit()
            except sqlite3.Error:  # pragma: no cover - a closed/locked connection
                pass
            self.conn.close()

    def __enter__(self) -> "SeriesStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def commit(self) -> None:
        self.conn.commit()

    # --- writes -------------------------------------------------------------
    def put(
        self,
        dataset: str,
        ts: datetime,
        values: Mapping[str, float],
        *,
        source_id: str | None = None,
    ) -> None:
        """Upsert one normalized point. Re-ingesting the same instant is a no-op."""
        self.conn.execute(
            "INSERT INTO series (dataset, ts_utc, payload, source_id) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(dataset, ts_utc) DO UPDATE SET payload = excluded.payload, "
            "source_id = excluded.source_id",
            (dataset, _iso(ts), json.dumps(dict(values), sort_keys=True), source_id),
        )

    def put_many(self, rows: Iterable[tuple[str, datetime, Mapping[str, float]]]) -> int:
        count = 0
        for dataset, ts, values in rows:
            self.put(dataset, ts, values)
            count += 1
        self.conn.commit()
        return count

    def put_raw(self, dataset: str, source_id: str, fetched_at: datetime, body: str) -> bool:
        """Retain a raw payload. Returns False when an identical one is already kept."""
        import hashlib

        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO raw_payload (dataset, source_id, fetched_at, content_hash, body) "
            "VALUES (?, ?, ?, ?, ?)",
            (dataset, source_id, _iso(fetched_at), digest, body),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def delete_range(self, dataset: str, since: datetime, until: datetime) -> int:
        cur = self.conn.execute(
            "DELETE FROM series WHERE dataset = ? AND ts_utc >= ? AND ts_utc < ?",
            (dataset, _iso(since), _iso(until)),
        )
        self.conn.commit()
        return cur.rowcount

    def clear(self) -> None:
        for table in (schema.SERIES_TABLE, schema.RAW_TABLE, schema.ALERTS_TABLE):
            self.conn.execute(f"DELETE FROM {table}")
        self.conn.commit()

    # --- introspection ------------------------------------------------------
    def datasets(self) -> tuple[str, ...]:
        rows = self.conn.execute("SELECT DISTINCT dataset FROM series ORDER BY dataset").fetchall()
        return tuple(r["dataset"] for r in rows)

    def count(self, dataset: str | None = None) -> int:
        if dataset:
            row = self.conn.execute("SELECT COUNT(*) AS n FROM series WHERE dataset=?", (dataset,)).fetchone()
        else:
            row = self.conn.execute("SELECT COUNT(*) AS n FROM series").fetchone()
        return int(row["n"])

    def days_with_data(self) -> list[date]:
        rows = self.conn.execute("SELECT DISTINCT substr(ts_utc, 1, 10) AS d FROM series ORDER BY d").fetchall()
        return [date.fromisoformat(r["d"]) for r in rows]
