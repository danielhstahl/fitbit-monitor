"""The writable side of the store: the dataset registry, series upserts, raw retention.

Only the host uses this. Ingest, the seeding helper and the alert history table write
through here; plugins never see a :class:`SeriesStore`.

Two promises live here rather than in the schema:

* **every dataset is registered** before it is written, so the ``dataset`` table can
  answer "what is in this store, and what shape is it" and the foreign keys on
  ``raw_payload`` hold;
* **a raw payload is stored once per distinct content**, compressed, with the range
  that was asked for - which is what makes an overlapping re-import free.
"""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Iterator, Mapping

from ..errors import StoreError
from ..timeutil import ensure_utc, utcnow
from . import datasets, raw, schema


def _iso(value: datetime | date | None) -> str | None:
    """ISO-8601 for a stored instant, or a bare day for a requested range."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return ensure_utc(value, field="ts").isoformat()
    return value.isoformat()


@contextmanager
def write_lock(path: Path | str, *, what: str = "another fm command"):
    """One writer per store, enforced for the life of the block.

    Two imports against the same database would interleave chunks and fight over the
    same rate-limit budget, and SQLite's default busy handling turns that into either
    a slow stall or a ``database is locked`` error. An advisory ``flock`` next to the
    file is the cheap correct answer: it dies with the process, so a crashed import
    never leaves the store locked, and it needs no lockfile cleanup or pid scraping.
    """
    lock_path = Path(path).with_name(Path(path).name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise StoreError(
                f"{what} is already writing to {lock_path.name[:-5]}",
                hint="wait for it to finish (it is safe to re-run afterwards), or point --db at a different store",
            ) from exc
        os.ftruncate(handle, 0)
        os.write(handle, str(os.getpid()).encode())
        yield lock_path
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        os.close(handle)


class SeriesStore:
    """Writable handle on the SQLite store."""

    def __init__(self, path: Path | str, conn: sqlite3.Connection | None = None) -> None:
        self.path = Path(path)
        self._own_conn = conn is None
        self._registered: set[str] = set()
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
        if not self._own_conn:
            return
        try:
            self.conn.commit()
        except sqlite3.Error:  # pragma: no cover - a closed/locked connection
            pass
        try:
            self.conn.close()
        except sqlite3.Error:  # pragma: no cover - closing twice is not a bug to raise about
            pass

    def __enter__(self) -> "SeriesStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def commit(self) -> None:
        self.conn.commit()

    # --- writes -------------------------------------------------------------
    def register_dataset(self, dataset: str) -> None:
        """Record ``dataset`` in the registry, once per handle.

        The registry is what ``fm store stats`` and the read API answer "what is in
        here?" from, and ``raw_payload`` has a foreign key on it. The catalog
        (:mod:`fitbit_monitor.store.datasets`) decides kind and grain; a dataset it has
        never heard of is registered as daily and unitless rather than refused, because
        a CSV export with a column fm has not seen is a normal thing.
        """
        if dataset in self._registered:
            return
        spec = datasets.spec(dataset)
        self.conn.execute(
            "INSERT INTO dataset (name, kind, granularity_seconds, first_seen_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET "
            "  kind = excluded.kind, "
            "  granularity_seconds = COALESCE(excluded.granularity_seconds, dataset.granularity_seconds), "
            "  first_seen_at = COALESCE(dataset.first_seen_at, excluded.first_seen_at)",
            (dataset, spec.kind, spec.granularity_seconds, _iso(utcnow())),
        )
        self._registered.add(dataset)

    def payload_at(self, dataset: str, ts: datetime) -> dict | None:
        """The stored payload for one instant, or None when there is no row yet."""
        row = self.conn.execute(
            "SELECT payload FROM series WHERE dataset = ? AND ts_utc = ?", (dataset, _iso(ts))
        ).fetchone()
        return json.loads(row["payload"]) if row else None

    def put(
        self,
        dataset: str,
        ts: datetime,
        values: Mapping[str, float],
        *,
        source_id: str | None = None,
        merge: bool = True,
    ) -> None:
        """Upsert one normalized point.

        Fields **merge** by default, because two partial writers legitimately describe
        the same instant: the daily-summary import takes ``steps`` from one endpoint
        and ``resting_hr`` from another, and a CSV import may meet the same day from a
        different file. Same field twice is last-write-wins, so re-importing a window
        stays a no-op rather than a lossy overwrite.

        The merge happens in SQL (`json_patch`), not by reading the row first: this is
        the hottest function in the codebase - a year of intraday heart rate is a
        million calls - and the benchmark says a SELECT per write costs ~25%.
        """
        self.register_dataset(dataset)
        self.conn.execute(
            "INSERT INTO series (dataset, ts_utc, payload, source_id) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(dataset, ts_utc) DO UPDATE SET "
            "payload = CASE WHEN ? THEN json_patch(series.payload, excluded.payload) "
            "ELSE excluded.payload END, "
            "source_id = excluded.source_id",
            (dataset, _iso(ts), json.dumps(dict(values), sort_keys=True), source_id, 1 if merge else 0),
        )

    def instants_between(self, dataset: str, since: datetime, until: datetime) -> set[str]:
        """The instants already stored for a dataset in a range, as ISO strings."""
        rows = self.conn.execute(
            "SELECT ts_utc FROM series WHERE dataset = ? AND ts_utc >= ? AND ts_utc <= ?",
            (dataset, _iso(since), _iso(until)),
        )
        return {row[0] for row in rows}

    def put_many(
        self, rows: Iterable[tuple[str, datetime, Mapping[str, float]]], *, source_id: str | None = None
    ) -> int:
        count = 0
        for dataset, ts, values in rows:
            self.put(dataset, ts, values, source_id=source_id)
            count += 1
        self.conn.commit()
        return count

    def put_raw(
        self,
        dataset: str,
        source_id: str,
        fetched_at: datetime,
        body: str,
        *,
        source: str = "api",
        requested_start: date | datetime | None = None,
        requested_end: date | datetime | None = None,
        commit: bool = True,
    ) -> bool:
        """Retain a raw payload. Returns False when an identical one is already kept.

        Stored gzipped and keyed by content hash, so a payload two overlapping imports
        both fetched costs the store one row. ``requested_start``/``requested_end``
        record the window the source was asked for, which is what a later
        ``fm store verify`` or a re-import uses to tell what is covered.

        ``commit=False`` leaves the write inside the caller's transaction, which is how
        the ingest layer makes a whole chunk land atomically or not at all.
        """
        self.register_dataset(dataset)
        digest = raw.content_hash(body)
        blob, payload_bytes, stored_bytes, encoding = raw.encode_body(body)
        before = self.conn.total_changes
        self.conn.execute(
            "INSERT OR IGNORE INTO raw_payload (content_hash, dataset, source, source_id, "
            "requested_start, requested_end, fetched_at, bytes, stored_bytes, encoding, body) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                digest,
                dataset,
                source,
                source_id,
                _iso(requested_start),
                _iso(requested_end),
                _iso(fetched_at),
                payload_bytes,
                stored_bytes,
                encoding,
                blob,
            ),
        )
        stored = self.conn.total_changes > before
        if commit:
            self.conn.commit()
        return stored

    def raw_payloads(self, **filters) -> Iterator[raw.RawPayload]:
        """Stored payloads, oldest first. See :func:`fitbit_monitor.store.raw.iter_payloads`."""
        return raw.iter_payloads(self.conn, **filters)

    def raw_total_bytes(self, *, stored: bool = True) -> int:
        """Bytes the raw payloads take - stored (compressed) by default, original on demand."""
        column = "stored_bytes" if stored else "bytes"
        row = self.conn.execute(f"SELECT COALESCE(SUM({column}), 0) AS n FROM raw_payload").fetchone()
        return int(row["n"])

    def quarantine(self, dataset: str, source_id: str | None, reason: str, body: str, *, commit: bool = False) -> None:
        """Keep a payload we could not use, with the reason, instead of dropping it."""
        self.conn.execute(
            "INSERT INTO quarantine (dataset, source_id, reason, body, quarantined_at) VALUES (?, ?, ?, ?, ?)",
            (dataset, source_id, reason, body, _iso(utcnow())),
        )
        if commit:
            self.conn.commit()

    def delete_range(self, dataset: str, since: datetime, until: datetime) -> int:
        cur = self.conn.execute(
            "DELETE FROM series WHERE dataset = ? AND ts_utc >= ? AND ts_utc < ?",
            (dataset, _iso(since), _iso(until)),
        )
        self.conn.commit()
        return cur.rowcount

    def delete_series_before(self, cutoff: datetime) -> int:
        """Drop normalized rows older than ``cutoff``. Returns the rows removed."""
        cur = self.conn.execute("DELETE FROM series WHERE ts_utc < ?", (_iso(ensure_utc(cutoff)),))
        self.conn.commit()
        return cur.rowcount

    def delete_raw_before(self, cutoff: datetime) -> tuple[int, int]:
        """Drop payloads fetched before ``cutoff``. Returns ``(rows, bytes_freed)``."""
        stamp = _iso(ensure_utc(cutoff))
        row = self.conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(stored_bytes), 0) AS bytes FROM raw_payload WHERE fetched_at < ?",
            (stamp,),
        ).fetchone()
        self.conn.execute("DELETE FROM raw_payload WHERE fetched_at < ?", (stamp,))
        self.conn.commit()
        return int(row["n"]), int(row["bytes"])

    def delete_quarantine_before(self, cutoff: datetime) -> int:
        """Drop quarantined payloads older than ``cutoff``. Returns the rows removed."""
        cur = self.conn.execute(
            "DELETE FROM quarantine WHERE quarantined_at < ?", (_iso(ensure_utc(cutoff)),)
        )
        self.conn.commit()
        return cur.rowcount

    def clear(self) -> None:
        for table in (
            schema.SERIES_TABLE,
            schema.RAW_TABLE,
            schema.ALERTS_TABLE,
            schema.ANNOTATION_TABLE,
        ):
            self.conn.execute(f"DELETE FROM {table}")
        self.conn.commit()

    # --- introspection ------------------------------------------------------
    def datasets(self) -> tuple[str, ...]:
        rows = self.conn.execute("SELECT DISTINCT dataset FROM series ORDER BY dataset").fetchall()
        return tuple(r["dataset"] for r in rows)

    def registry(self) -> dict[str, dict[str, object]]:
        """The dataset registry as ``{name: {kind, granularity_seconds, first_seen_at}}``."""
        rows = self.conn.execute(
            "SELECT name, kind, granularity_seconds, first_seen_at FROM dataset ORDER BY name"
        ).fetchall()
        return {
            row["name"]: {
                "kind": row["kind"],
                "granularity_seconds": row["granularity_seconds"],
                "first_seen_at": row["first_seen_at"],
            }
            for row in rows
        }

    def count(self, dataset: str | None = None) -> int:
        if dataset:
            row = self.conn.execute("SELECT COUNT(*) AS n FROM series WHERE dataset=?", (dataset,)).fetchone()
        else:
            row = self.conn.execute("SELECT COUNT(*) AS n FROM series").fetchone()
        return int(row["n"])

    def days_with_data(self) -> list[date]:
        rows = self.conn.execute("SELECT DISTINCT substr(ts_utc, 1, 10) AS d FROM series ORDER BY d").fetchall()
        return [date.fromisoformat(r["d"]) for r in rows]
