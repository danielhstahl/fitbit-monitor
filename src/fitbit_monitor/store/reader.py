"""The read API: range iterators, coverage, gaps, raw payloads, downsample.

This is the surface a plugin gets (as ``ctx.store``), and the surface every ``fm``
report is built on. There are no write methods here, and the connection is opened
``mode=ro`` so even a plugin that digs down to ``store.connection.execute("DELETE …")``
is refused by SQLite.

Units and the UTC convention
-----------------------------
**Every stored instant is UTC.** ``ts_utc`` holds ISO-8601 text with an explicit
``+00:00`` offset; naive datetimes are refused at every boundary rather than guessed.
Iterators return aware ``datetime`` objects and every window is half-open,
``[since, until)``.

A *daily* dataset (``daily_summary``, ``hrv``, ``sleep`` totals) is stored at
``YYYY-MM-DDT00:00:00+00:00`` for its **source day**: the date part is the meaning and
the time part is a placeholder, so a local-midnight boundary is represented as "that
day, at UTC midnight" and the day label - not the instant - is what a report should
quote. Do not shift those instants into a local zone to recover a date; read
``point.ts.date()``. An intraday point is the opposite: a real instant, obtained by
reading the source's naive local timestamp in ``[import] timezone`` and converting it.

Field units come from :mod:`fitbit_monitor.store.datasets`, one table for the whole
program, so a plugin's ``MetricResult.unit`` and ``fm store stats`` say the same thing.
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
from . import datasets as catalog
from . import raw, schema
from .raw import RawPayload

#: A gap is a break longer than this many times the series' own grain.
GAP_TOLERANCE = 3
#: Used when a dataset declares no grain: "four hours of nothing is a gap".
DEFAULT_GAP_SECONDS = 4 * 3600
#: Spacings needed before the store trusts its own reading of a series' grain.
_GRAIN_MIN_SAMPLES = 8


def _day_start(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


@dataclass(frozen=True)
class SeriesPoint:
    """One stored sample: an instant plus the numeric fields recorded at it.

    Values are floats; units come from the dataset catalog, because one field means one
    thing everywhere it appears: ``point.unit("hr")`` is ``"beats per minute"``.
    """

    ts: datetime
    values: Mapping[str, float]
    dataset: str = ""

    def get(self, name: str, default: float | None = None) -> float | None:
        return self.values.get(name, default)

    def __getitem__(self, name: str) -> float:
        return self.values[name]

    def __contains__(self, name: str) -> bool:
        return name in self.values

    def unit(self, name: str) -> str | None:
        """The unit of one field, or ``None`` when the catalog does not name it."""
        return catalog.spec(self.dataset).unit(name) or catalog.FIELD_UNITS.get(name)

    def items_with_units(self) -> tuple[tuple[str, float, str | None], ...]:
        """``(field, value, unit)`` triples - what a report prints without guessing."""
        return tuple((name, value, self.unit(name)) for name, value in self.values.items())

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


@dataclass(frozen=True)
class Gap:
    """A break in a series longer than the dataset's own grain allows."""

    start: datetime
    end: datetime
    seconds: float

    @property
    def hours(self) -> float:
        return self.seconds / 3600.0

    def as_dict(self) -> dict[str, object]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "seconds": self.seconds,
            "hours": round(self.hours, 2),
        }


@dataclass(frozen=True)
class Completeness:
    """Coverage plus the gaps behind it: "is this window safe to compute on?"

    ``complete`` is False when anything is missing, and the gaps are listed rather than
    silently skipped, because a plugin that averages across a two-day hole in the data
    would report a number that never happened.
    """

    coverage: Coverage
    gaps: tuple[Gap, ...] = ()
    max_gap_seconds: float | None = None

    @property
    def complete(self) -> bool:
        return not self.gaps and not self.coverage.missing_days

    @property
    def longest_gap(self) -> Gap | None:
        return max(self.gaps, key=lambda gap: gap.seconds) if self.gaps else None

    def as_dict(self) -> dict[str, object]:
        return {
            **self.coverage.as_dict(),
            "complete": self.complete,
            "gaps": [gap.as_dict() for gap in self.gaps],
            "max_gap_seconds": self.max_gap_seconds,
        }


@dataclass(frozen=True)
class DatasetInfo:
    """One dataset as stored: shape, extent, and what it costs on disk."""

    name: str
    kind: str = "daily"
    granularity_seconds: int | None = None
    points: int = 0
    first_ts: datetime | None = None
    last_ts: datetime | None = None
    days_present: int = 0
    payload_count: int = 0
    payload_bytes: int = 0
    stored_bytes: int = 0

    @property
    def units(self) -> dict[str, str]:
        return catalog.spec(self.name).units

    @property
    def fields(self) -> tuple[str, ...]:
        return catalog.fields(self.name)

    def as_dict(self) -> dict[str, object]:
        return {
            "dataset": self.name,
            "kind": self.kind,
            "granularity_seconds": self.granularity_seconds,
            "points": self.points,
            "first_ts": self.first_ts.isoformat() if self.first_ts else None,
            "last_ts": self.last_ts.isoformat() if self.last_ts else None,
            "days_present": self.days_present,
            "payload_count": self.payload_count,
            "payload_bytes": self.payload_bytes,
            "stored_bytes": self.stored_bytes,
            "fields": list(self.fields),
            "units": self.units,
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
        except (StoreError, sqlite3.OperationalError) as exc:
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
            yield SeriesPoint(ts=datetime.fromisoformat(row["ts_utc"]), values=values, dataset=dataset)

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

    # --- one day, and the shape of a dataset --------------------------------
    def day(self, dataset: str, day: date | datetime | str) -> list[SeriesPoint]:
        """Every point of ``dataset`` on one calendar day (UTC), oldest first.

        Accepts ``date``, aware ``datetime`` or ``YYYY-MM-DD``. For a daily dataset this
        is the one-row lookup a report does most often.
        """
        if isinstance(day, str):
            day = date.fromisoformat(day[:10])
        start = _day_start(to_day(day))
        return list(self.query(dataset, start, start + timedelta(days=1)))

    def describe(self, dataset: str) -> catalog.DatasetSpec:
        """Kind, grain, fields and units of one dataset (see :mod:`.datasets`)."""
        return catalog.spec(dataset)

    def units(self, dataset: str) -> dict[str, str]:
        """``{field: unit}`` for a dataset - the units a report must print."""
        return catalog.spec(dataset).units

    def grain_seconds(self, dataset: str) -> int | None:
        """Native spacing of a dataset, or ``None`` when fm does not know it."""
        return catalog.spec(dataset).granularity_seconds

    # --- completeness -------------------------------------------------------
    def _observed_grain_seconds(self, dataset: str, since: datetime, until: datetime, *, sample: int = 500) -> float | None:
        """Typical spacing actually present, from a bounded sample of the window.

        The **lower quartile**, not the median: the point is the tight spacing between
        ordinary samples, and a median walks away from the truth as soon as the sample is
        short and contains the very gap being looked for. Below ``sample_min`` spacings
        there is no grain to measure, so the catalog's is used instead.
        """
        rows = self.conn.execute(
            "SELECT ts_utc FROM series WHERE dataset = ? AND ts_utc >= ? AND ts_utc < ? "
            "ORDER BY ts_utc LIMIT ?",
            (dataset, ensure_utc(since, field="since").isoformat(), ensure_utc(until, field="until").isoformat(), sample),
        ).fetchall()
        stamps = [datetime.fromisoformat(row["ts_utc"]) for row in rows]
        if len(stamps) < _GRAIN_MIN_SAMPLES + 1:
            return None
        diffs = sorted((b - a).total_seconds() for a, b in zip(stamps, stamps[1:]))
        return float(diffs[len(diffs) // 4])

    def gaps(
        self,
        dataset: str,
        since: datetime,
        until: datetime,
        *,
        max_gap_seconds: float | None = None,
        tolerance: float = GAP_TOLERANCE,
    ) -> tuple[Gap, ...]:
        """Breaks in a series inside ``[since, until)``, longest break first.

        A gap is a break longer than ``tolerance`` times the series' grain, where the
        grain is the coarser of the catalog's native grain and the spacing actually
        present in the window - so a weekly HRV series is not "all gaps" and a
        heart-rate series that stops for a day is. Pass ``max_gap_seconds`` to decide for
        yourself.
        """
        since = ensure_utc(since, field="since")
        until = ensure_utc(until, field="until")
        if max_gap_seconds is None:
            catalog_grain = self.grain_seconds(dataset) or DEFAULT_GAP_SECONDS
            observed = self._observed_grain_seconds(dataset, since, until)
            step = max(float(catalog_grain), observed or 0.0)
            max_gap_seconds = step * tolerance
        found: list[Gap] = []
        previous: datetime | None = None
        for row in self.conn.execute(
            "SELECT ts_utc FROM series WHERE dataset = ? AND ts_utc >= ? AND ts_utc < ? ORDER BY ts_utc",
            (dataset, since.isoformat(), until.isoformat()),
        ).fetchall():
            stamp = datetime.fromisoformat(row["ts_utc"])
            if previous is not None:
                seconds = (stamp - previous).total_seconds()
                if seconds > max_gap_seconds:
                    found.append(Gap(start=previous, end=stamp, seconds=seconds))
            previous = stamp
        return tuple(sorted(found, key=lambda gap: -gap.seconds))

    def completeness(
        self, dataset: str, since: datetime, until: datetime, *, max_gap_seconds: float | None = None
    ) -> Completeness:
        """Coverage and gaps together - the answer before computing a metric on a window."""
        gaps = self.gaps(dataset, since, until, max_gap_seconds=max_gap_seconds)
        return Completeness(coverage=self.coverage(dataset, since, until), gaps=gaps)

    # --- what is in here ----------------------------------------------------
    def stored_datasets(self) -> tuple[DatasetInfo, ...]:
        """Every registered dataset with its extent and its cost on disk."""
        return stored_datasets(self.conn)

    # --- raw payloads -------------------------------------------------------
    def iter_raw(self, **filters) -> Iterator[RawPayload]:
        """Stored payloads, oldest fetch first. See :func:`fitbit_monitor.store.raw.iter_payloads`."""
        return raw.iter_payloads(self.conn, **filters)

    def raw_payload(self, content_hash: str, *, with_body: bool = True) -> RawPayload | None:
        """One stored payload by content hash, bytes included."""
        return raw.payload_by_hash(self.conn, content_hash, with_body=with_body)

    def raw_for(self, dataset: str, ts: datetime) -> RawPayload | None:
        """The most recent payload fetched for a dataset at or before ``ts``."""
        row = self.conn.execute(
            "SELECT content_hash FROM raw_payload WHERE dataset = ? AND fetched_at <= ? "
            "ORDER BY fetched_at DESC, rowid DESC LIMIT 1",
            (dataset, ensure_utc(ts, field="ts").isoformat()),
        ).fetchone()
        return self.raw_payload(row["content_hash"]) if row else None

    def raw_totals(self, dataset: str | None = None) -> dict[str, dict[str, float]]:
        """Payload counts and bytes per dataset (``"all"`` included)."""
        return raw.totals(self.conn, dataset=dataset)

    # --- read-only proof ----------------------------------------------------
    def assert_read_only(self) -> None:
        """Raise if this handle could write. Cheap, and used by the contract test suite."""
        try:
            self.conn.execute("CREATE TABLE _fm_read_only_probe (x INTEGER)")
        except sqlite3.OperationalError:
            return
        self.conn.execute("DROP TABLE IF EXISTS _fm_read_only_probe")  # pragma: no cover
        raise StoreError("this store handle is writable; a read-only handle was expected")


def stored_datasets(conn: sqlite3.Connection) -> tuple[DatasetInfo, ...]:
    """Every registered dataset with its extent and its cost on disk.

    Driven off the ``dataset`` registry rather than ``series``, so a dataset whose
    payloads are all there but whose rows all failed to normalize still shows up.
    """
    rows = conn.execute(
        """
        SELECT d.name AS name, d.kind AS kind, d.granularity_seconds AS granularity_seconds,
               (SELECT COUNT(*) FROM series s WHERE s.dataset = d.name) AS points,
               (SELECT MIN(ts_utc) FROM series WHERE dataset = d.name) AS first_ts,
               (SELECT MAX(ts_utc) FROM series WHERE dataset = d.name) AS last_ts,
               (SELECT COUNT(DISTINCT substr(ts_utc, 1, 10)) FROM series WHERE dataset = d.name) AS days_present,
               (SELECT COUNT(*) FROM raw_payload r WHERE r.dataset = d.name) AS payload_count,
               (SELECT COALESCE(SUM(bytes), 0) FROM raw_payload WHERE dataset = d.name) AS payload_bytes,
               (SELECT COALESCE(SUM(stored_bytes), 0) FROM raw_payload WHERE dataset = d.name) AS stored_bytes
          FROM dataset d ORDER BY d.name
        """
    ).fetchall()
    return tuple(
        DatasetInfo(
            name=row["name"],
            kind=row["kind"],
            granularity_seconds=row["granularity_seconds"],
            points=int(row["points"]),
            first_ts=datetime.fromisoformat(row["first_ts"]) if row["first_ts"] else None,
            last_ts=datetime.fromisoformat(row["last_ts"]) if row["last_ts"] else None,
            days_present=int(row["days_present"]),
            payload_count=int(row["payload_count"]),
            payload_bytes=int(row["payload_bytes"]),
            stored_bytes=int(row["stored_bytes"]),
        )
        for row in rows
    )


def days_with_data(conn: sqlite3.Connection, dataset: str | None = None) -> list[date]:
    """Distinct UTC calendar days present, optionally for one dataset."""
    sql = "SELECT DISTINCT substr(ts_utc, 1, 10) AS d FROM series"
    params: list[object] = []
    if dataset:
        sql += " WHERE dataset = ?"
        params.append(dataset)
    sql += " ORDER BY d"
    return [date.fromisoformat(row["d"]) for row in conn.execute(sql, params)]


__all__ = [
    "Completeness",
    "Coverage",
    "DatasetInfo",
    "Gap",
    "ReadOnlyStore",
    "RawPayload",
    "SeriesPoint",
    "days_with_data",
    "stored_datasets",
]
