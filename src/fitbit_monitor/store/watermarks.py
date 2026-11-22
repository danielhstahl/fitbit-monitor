"""Watermarks: how far each dataset has been fetched, per source.

A watermark is the last **completely fetched** calendar day for a dataset from one
source, plus the last instant actually stored. That is the whole mechanism behind
"run it again tomorrow and it picks up where it stopped", and it is deliberately
conservative:

* a day only advances the watermark when every request for that day succeeded, so a
  half-fetched day is simply fetched again - rows are idempotent, so re-fetching
  costs requests, not correctness;
* watermarks are per ``source``, so an import from a CSV export cannot claim to have
  brought the API's watermark forward;
* they only move forward - re-importing an old window must not make tomorrow's
  default ``--since`` go backwards.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from ..timeutil import ensure_utc, utcnow


@dataclass(frozen=True)
class Watermark:
    """Where a dataset stands for one source."""

    dataset: str
    source: str
    last_day: date | None = None
    last_ts: datetime | None = None
    updated_at: datetime | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "dataset": self.dataset,
            "source": self.source,
            "last_day": self.last_day.isoformat() if self.last_day else None,
            "last_ts": self.last_ts.isoformat() if self.last_ts else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class Watermarks:
    """Read/advance watermarks through a writable store handle."""

    def __init__(self, store) -> None:
        self.store = store
        self.conn = store.conn

    def get(self, dataset: str, source: str = "api") -> Watermark | None:
        row = self.conn.execute(
            "SELECT * FROM dataset_watermark WHERE dataset = ? AND source = ?", (dataset, source)
        ).fetchone()
        if row is None:
            return None
        return Watermark(
            dataset=dataset,
            source=source,
            last_day=date.fromisoformat(row["last_day"]) if row["last_day"] else None,
            last_ts=datetime.fromisoformat(row["last_ts"]) if row["last_ts"] else None,
            updated_at=datetime.fromisoformat(row["updated_at"]) if row["updated_at"] else None,
        )

    def all(self, source: str | None = None) -> tuple[Watermark, ...]:
        """Every watermark, optionally narrowed to one source."""
        sql = "SELECT dataset, source FROM dataset_watermark"
        params: list[object] = []
        if source:
            sql += " WHERE source = ?"
            params.append(source)
        sql += " ORDER BY dataset, source"
        rows = self.conn.execute(sql, params).fetchall()
        return tuple(self.get(r["dataset"], r["source"]) for r in rows)  # type: ignore[misc]

    def advance(self, dataset: str, source: str, *, last_day: date | None, last_ts: datetime | None) -> Watermark:
        """Move a watermark forward. Never backwards, so an old backfill is harmless."""
        current = self.get(dataset, source)
        keep_day = current.last_day if current and last_day and current.last_day and current.last_day > last_day else last_day
        keep_ts = current.last_ts if current and last_ts and current.last_ts and current.last_ts > last_ts else last_ts
        now = utcnow()
        self.conn.execute(
            """
            INSERT INTO dataset_watermark (dataset, source, last_day, last_ts, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(dataset, source) DO UPDATE SET
                last_day = excluded.last_day,
                last_ts = excluded.last_ts,
                updated_at = excluded.updated_at
            """,
            (
                dataset,
                source,
                keep_day.isoformat() if keep_day else None,
                ensure_utc(keep_ts).isoformat() if keep_ts else None,
                now.isoformat(),
            ),
        )
        return Watermark(dataset=dataset, source=source, last_day=keep_day, last_ts=keep_ts, updated_at=now)

    def next_since(self, dataset: str, source: str = "api") -> date | None:
        """The day an incremental import of ``dataset`` should start at, if any."""
        watermark = self.get(dataset, source)
        if watermark is None or watermark.last_day is None:
            return None
        return watermark.last_day + timedelta(days=1)

    def clear(self, dataset: str | None = None) -> int:
        if dataset:
            cur = self.conn.execute("DELETE FROM dataset_watermark WHERE dataset = ?", (dataset,))
        else:
            cur = self.conn.execute("DELETE FROM dataset_watermark")
        self.conn.commit()
        return cur.rowcount


__all__ = ["Watermark", "Watermarks"]
