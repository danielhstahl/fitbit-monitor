"""The read API: range iterators, coverage, latest/first.

This is the surface a plugin gets (as ``ctx.store``). There are no write methods
here, and the connection is opened ``mode=ro`` so even a plugin that digs down to
``store.connection.execute("DELETE …")`` is refused by SQLite.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterator, Mapping, Sequence

from ..errors import StoreError, StoreUnavailableError
from ..timeutil import DateRange, ensure_utc, to_day
from . import schema


def _day_start(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


@dataclass(frozen=True)
class SeriesPoint:
    """One stored sample: an instant plus the numeric fields recorded at it."""

    ts: datetime
    values: Mapping[str, float]

    def get(self, name: str, default: float | None = None) -> float | None:
        return self.values.get(name, default)

    def __getitem__(self, name: str) -> float:
        return self.values[name]

    def __contains__(self, name: str) -> bool:
        return name in self.values

    def as_dict(self) -> dict[str, float]:
        return dict(self.values)


@dataclass(frozen=True)
class Coverage:
    """How complete a dataset is over a window - plugins report this honestly."""

    dataset: str
    since: datetime
    until: datetime
    points: int
    days_expected: int
    days_present: int
    missing_days: tuple[date, ...] = ()
    first_ts: datetime | None = None
    last_ts: datetime | None = None

    @property
    def fraction(self) -> float:
        if self.days_expected <= 0:
            return 0.0
        return max(0.0, min(1.0, self.days_present / self.days_expected))

    @property
    def has_data(self) -> bool:
        return self.points > 0

    def as_dict(self) -> dict[str, object]:
        return {
            "dataset": self.dataset,
            "points": self.points,
            "days_expected": self.days_expected,
            "days_present": self.days_present,
            "coverage": round(self.fraction, 4),
            "missing_days": [d.isoformat() for d in self.missing_days],
            "first_ts": self.first_ts.isoformat() if self.first_ts else None,
            "last_ts": self.last_ts.isoformat() if self.last_ts else None,
        }


@dataclass
class ReadOnlyStore:
    """Read-only view of the local store. The plugin's only window onto data.

    Every accessor takes an optional half-open ``[since, until)`` window, so a
    plugin never has to scan more than the run window it was given.
    """

    path: Path
    connection: sqlite3.Connection | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        if self.connection is None:
            if not self.path.exists():
                raise StoreUnavailableError(
                    f"no store at {self.path}",
                    hint="create it with `fm dev seed --days 30` (sample data) or an import run once one exists",
                )
            try:
                self.connection = schema.connect_ro(self.path)
            except sqlite3.OperationalError as exc:
                raise StoreError(f"cannot open store {self.path}: {exc}") from exc
        try:
            version = schema.schema_version(self.connection)
        except sqlite3.OperationalError as exc:
            raise StoreError(
                f"{self.path} is not a fitbit-monitor store ({exc})",
                hint="point --db at the store fm created, or remove it and let fm rebuild it",
            ) from exc
        if version is None:
            raise StoreError(
                f"{self.path} has no fitbit-monitor schema",
                hint="create it with `fm dev seed`, or point --db at the store fm already built",
            )
        if version > schema.SCHEMA_VERSION:
            raise StoreError(f"store schema version {version} is newer than this build")

    # --- context management -------------------------------------------------
    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def __enter__(self) -> "ReadOnlyStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def conn(self) -> sqlite3.Connection:
        if self.connection is None:
            raise StoreError("store is closed")
        return self.connection

    # --- the read API -------------------------------------------------------
    def datasets(self) -> tuple[str, ...]:
        rows = self.conn.execute("SELECT DISTINCT dataset FROM series ORDER BY dataset").fetchall()
        return tuple(r["dataset"] for r in rows)

    def has_dataset(self, dataset: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM series WHERE dataset=? LIMIT 1", (dataset,)).fetchone()
        return row is not None

    def query(
        self,
        dataset: str,
        since: datetime | None = None,
        until: datetime | None = None,
        *,
        fields: Sequence[str] | None = None,
        order: str = "asc",
        limit: int | None = None,
    ) -> Iterator[SeriesPoint]:
        """Iterate points of ``dataset`` in ``[since, until)``.

        ``fields`` drops points that do not carry every named field, which keeps
        plugins from writing ``if 'hr' in p`` inside a hot loop.
        """
        if since is not None:
            since = ensure_utc(since, field="since")
        if until is not None:
            until = ensure_utc(until, field="until")
        if since is not None and until is not None and until < since:
            raise StoreError("query window is inverted (until < since)")
        sql = "SELECT ts_utc, payload FROM series WHERE dataset = ?"
        params: list[object] = [dataset]
        if since is not None:
            sql += " AND ts_utc >= ?"
            params.append(since.isoformat())
        if until is not None:
            sql += " AND ts_utc < ?"
            params.append(until.isoformat())
        sql += " ORDER BY ts_utc " + ("DESC" if order.lower() == "desc" else "ASC")
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        wanted = set(fields or ())
        for row in self.conn.execute(sql, params):
            values = json.loads(row["payload"])
            if wanted and not wanted.issubset(values):
                continue
            yield SeriesPoint(ts=datetime.fromisoformat(row["ts_utc"]), values=values)

    def series(self, dataset: str, window: DateRange, **kw) -> Iterator[SeriesPoint]:
        """``query`` with the run window already applied."""
        return self.query(dataset, window.since, window.until, **kw)

    def first_ts(self, dataset: str) -> datetime | None:
        row = self.conn.execute(
            "SELECT ts_utc FROM series WHERE dataset=? ORDER BY ts_utc ASC LIMIT 1", (dataset,)
        ).fetchone()
        return datetime.fromisoformat(row["ts_utc"]) if row else None

    def last_ts(self, dataset: str) -> datetime | None:
        row = self.conn.execute(
            "SELECT ts_utc FROM series WHERE dataset=? ORDER BY ts_utc DESC LIMIT 1", (dataset,)
        ).fetchone()
        return datetime.fromisoformat(row["ts_utc"]) if row else None

    def latest(self, dataset: str, *, fields: Sequence[str] | None = None) -> SeriesPoint | None:
        for point in self.query(dataset, order="desc", fields=fields, limit=25):
            return point
        return None

    def count(self, dataset: str, since: datetime | None = None, until: datetime | None = None) -> int:
        sql = "SELECT COUNT(*) AS n FROM series WHERE dataset = ?"
        params: list[object] = [dataset]
        if since is not None:
            sql += " AND ts_utc >= ?"
            params.append(ensure_utc(since, field="since").isoformat())
        if until is not None:
            sql += " AND ts_utc < ?"
            params.append(ensure_utc(until, field="until").isoformat())
        return int(self.conn.execute(sql, params).fetchone()["n"])

    def coverage(self, dataset: str, since: datetime, until: datetime) -> Coverage:
        """Point count plus which calendar days are present in ``[since, until)``."""
        since = ensure_utc(since, field="since")
        until = ensure_utc(until, field="until")
        rows = self.conn.execute(
            "SELECT DISTINCT substr(ts_utc, 1, 10) AS d, COUNT(*) AS n FROM series "
            "WHERE dataset = ? AND ts_utc >= ? AND ts_utc < ? GROUP BY d ORDER BY d",
            (dataset, since.isoformat(), until.isoformat()),
        ).fetchall()
        present = {date.fromisoformat(r["d"]) for r in rows}
        total = sum(int(r["n"]) for r in rows)
        expected: list[date] = []
        day = since.date()
        stop = to_day(until)
        while day < stop:
            expected.append(day)
            day += timedelta(days=1)
        if not expected:
            expected = [since.date()]
            present = present & set(expected)
        missing = tuple(d for d in expected if d not in present)
        first = _day_start(date.fromisoformat(rows[0]["d"])) if rows else None
        last = _day_start(date.fromisoformat(rows[-1]["d"])) if rows else None
        return Coverage(
            dataset=dataset,
            since=since,
            until=until,
            points=total,
            days_expected=len(expected),
            days_present=len(present & set(expected)),
            missing_days=missing,
            first_ts=first,
            last_ts=last,
        )

    def days_with_data(self, dataset: str | None = None) -> list[date]:
        """Distinct calendar days that have at least one stored point."""
        sql = "SELECT DISTINCT substr(ts_utc, 1, 10) AS d FROM series"
        params: list[object] = []
        if dataset:
            sql += " WHERE dataset = ?"
            params.append(dataset)
        sql += " ORDER BY d"
        return [date.fromisoformat(r["d"]) for r in self.conn.execute(sql, params)]

    def has_any_data(self, datasets: Sequence[str], window: DateRange) -> bool:
        """False when *nothing* is stored for the window - a run says so out loud."""
        for dataset in datasets:
            if self.count(dataset, window.since, window.until) > 0:
                return True
        return False
