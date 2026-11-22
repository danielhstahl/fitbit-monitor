"""Store maintenance: stats, verify, export, backup, prune.

This database becomes the only durable copy of someone's health history, so the
commands that prove it is intact - and get it out - are part of the storage model, not
an afterthought (ADR-0003).

* :func:`stats` - what is in here, how much of it there is, and what it costs.
* :func:`verify` - re-hash every retained payload against its content hash, re-check the
  structural invariants, and say exactly which ones broke. Corruption is a nonzero exit,
  not a shrug.
* :func:`export` - a portable copy of the normalized series or of the original payloads.
* :func:`backup` - one consistent file, through SQLite's online-backup API, while the
  store is being written to.
* :func:`prune` - retention, with **dry-run as the default**: nothing is deleted until
  the caller says ``execute=True``.

Everything here reads through a connection the caller owns, so the same functions serve
a CLI command and a test that already has a store open.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, Sequence

from ..errors import StoreError
from ..timeutil import ensure_utc, utcnow
from . import raw as raw_store
from . import schema
from .reader import days_with_data, stored_datasets

#: What ``fm store prune`` defaults to: raw payloads only. The normalized series is what
#: plugins compute on, so dropping it is always an explicit choice.
DEFAULT_PRUNE_WHAT = ("raw",)
PRUNE_WHAT = ("raw", "series", "quarantine", "all")
#: Documented default retention for raw payloads, in days (docs/storage.md).
DEFAULT_RETENTION_DAYS = 365
#: How many individual problems one check reports before saying "and N more".
MAX_REPORTED_PROBLEMS = 20


# --- stats -----------------------------------------------------------------
def human_bytes(count: float) -> str:
    """Bytes as a human reads them, binary units (a database is not a hard-drive ad)."""
    size = float(count)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(size) < 1024.0 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TiB"  # pragma: no cover - the loop always returns first


def _normalized_bytes(conn: sqlite3.Connection) -> dict[str, int]:
    """On-disk size of the normalized rows, per dataset (payload JSON plus keys)."""
    rows = conn.execute(
        "SELECT dataset, SUM(LENGTH(payload) + LENGTH(ts_utc) + LENGTH(dataset)) AS bytes "
        "FROM series GROUP BY dataset"
    ).fetchall()
    return {row["dataset"]: int(row["bytes"] or 0) for row in rows}


def stats(conn: sqlite3.Connection, *, path: Path | str | None = None) -> dict[str, Any]:
    """Everything ``fm store stats`` prints, as data.

    Raw bytes are reported twice on purpose - as fetched and as stored - because the
    gap between them is the argument for keeping raw at all.
    """
    infos = stored_datasets(conn)
    normalized = _normalized_bytes(conn)
    totals = raw_store.totals(conn)
    runs = conn.execute(
        "SELECT COALESCE(SUM(rows_duplicate), 0) AS rows_duplicate, "
        "COALESCE(SUM(payloads_duplicate), 0) AS payloads_duplicate, "
        "COALESCE(SUM(quarantined), 0) AS quarantined FROM dataset_run"
    ).fetchone()
    datasets_out: list[dict[str, Any]] = []
    for info in infos:
        entry = info.as_dict()
        entry["normalized_bytes"] = normalized.get(info.name, 0)
        datasets_out.append(entry)
    quarantined = int(conn.execute("SELECT COUNT(*) AS n FROM quarantine").fetchone()["n"])
    return {
        "database": str(path) if path else None,
        "schema_version": schema.read_version(conn),
        "schema_version_known": schema.SCHEMA_VERSION,
        "file_bytes": Path(path).stat().st_size if path and Path(path).exists() else 0,
        "datasets": datasets_out,
        "totals": {
            "points": sum(int(entry["points"]) for entry in datasets_out),
            "days_with_data": len(days_with_data(conn)),
            "normalized_bytes": sum(normalized.values()),
            "payloads": int(totals["all"]["payloads"]),
            "raw_bytes": int(totals["all"]["bytes"]),
            "raw_stored_bytes": int(totals["all"]["stored_bytes"]),
            "compression_ratio": round(
                (int(totals["all"]["stored_bytes"]) / int(totals["all"]["bytes"])), 4
            )
            if totals["all"]["bytes"]
            else None,
            "quarantined": quarantined,
            "duplicate_rows_by_import": int(runs["rows_duplicate"]),
            "duplicate_payloads_by_import": int(runs["payloads_duplicate"]),
        },
        "pragmas": {
            "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0],
            "foreign_keys": bool(conn.execute("PRAGMA foreign_keys").fetchone()[0]),
            "page_size": int(conn.execute("PRAGMA page_size").fetchone()[0]),
            "page_count": int(conn.execute("PRAGMA page_count").fetchone()[0]),
            "free_pages": int(conn.execute("PRAGMA freelist_count").fetchone()[0]),
        },
    }


# --- verify ----------------------------------------------------------------
@dataclass
class Check:
    """One thing that was checked, and what it found."""

    name: str
    ok: bool
    checked: int = 0
    problems: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {"check": self.name, "ok": self.ok, "checked": self.checked, "problems": list(self.problems)}


@dataclass
class Verification:
    """The outcome of ``fm store verify``."""

    ok: bool = True
    checks: list[Check] = field(default_factory=list)
    checked_at: datetime = field(default_factory=utcnow)

    def add(self, check: Check) -> Check:
        self.checks.append(check)
        if not check.ok:
            self.ok = False
        return check

    @property
    def problem_count(self) -> int:
        return sum(len(check.problems) for check in self.checks)

    @property
    def problems(self) -> list[str]:
        return [problem for check in self.checks for problem in check.problems]

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "checked_at": self.checked_at.isoformat(),
            "checks": [check.as_dict() for check in self.checks],
            "problem_count": self.problem_count,
        }


def _truncate(problems: list[str]) -> list[str]:
    if len(problems) <= MAX_REPORTED_PROBLEMS:
        return problems
    return problems[:MAX_REPORTED_PROBLEMS] + [f"... and {len(problems) - MAX_REPORTED_PROBLEMS} more"]


def verify(conn: sqlite3.Connection, *, include_bodies: bool = True, sample: int | None = None) -> Verification:
    """Re-check the store from the outside in.

    Order matters: cheap structural checks first, then the expensive re-hash of every
    retained payload. ``sample`` caps how many payloads are re-hashed (useful on a
    four-year store); ``include_bodies=False`` skips that check entirely.
    """
    result = Verification()

    version = schema.read_version(conn)
    version_ok = version is not None and version <= schema.SCHEMA_VERSION
    result.add(
        Check(
            name="schema_version",
            ok=version_ok,
            checked=1,
            problems=[] if version_ok else [f"schema version {version} (this build knows up to {schema.SCHEMA_VERSION})"],
        )
    )

    integrity = [row[0] for row in conn.execute("PRAGMA integrity_check").fetchall()]
    result.add(
        Check(
            name="integrity_check",
            ok=integrity == ["ok"],
            checked=1,
            problems=[] if integrity == ["ok"] else [str(line) for line in integrity],
        )
    )

    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    result.add(
        Check(
            name="foreign_keys",
            ok=not violations,
            checked=len(violations),
            problems=_truncate(
                [f"fk violation in {row['table']} rowid {row['rowid']}: no {row['parent']} row" for row in violations]
            ),
        )
    )

    unregistered = conn.execute(
        "SELECT DISTINCT s.dataset AS d FROM series s LEFT JOIN dataset d ON d.name = s.dataset "
        "WHERE d.name IS NULL ORDER BY s.dataset"
    ).fetchall()
    orphan_payloads = conn.execute(
        "SELECT DISTINCT r.dataset AS d FROM raw_payload r LEFT JOIN dataset d ON d.name = r.dataset "
        "WHERE d.name IS NULL ORDER BY r.dataset"
    ).fetchall()
    result.add(
        Check(
            name="dataset_registry",
            ok=not unregistered and not orphan_payloads,
            checked=len(unregistered) + len(orphan_payloads),
            problems=[f"rows stored for unregistered dataset {row['d']!r}" for row in unregistered]
            + [f"payloads stored for unregistered dataset {row['d']!r}" for row in orphan_payloads],
        )
    )

    bad_rows = conn.execute(
        "SELECT dataset, ts_utc FROM series WHERE NOT json_valid(payload) "
        "UNION ALL SELECT dataset, ts_utc FROM series WHERE ts_utc IS NULL OR NOT datetime(ts_utc)"
    ).fetchall()
    result.add(
        Check(
            name="series_rows",
            ok=not bad_rows,
            checked=int(conn.execute("SELECT COUNT(*) AS n FROM series").fetchone()["n"]),
            problems=_truncate(
                [f"{row['dataset']} @ {row['ts_utc']}: payload is not JSON or ts_utc is not a datetime" for row in bad_rows]
            ),
        )
    )

    if include_bodies:
        result.add(_verify_payloads(conn, sample=sample))

    result.add(_verify_recount(conn))

    return result


def _verify_recount(conn: sqlite3.Connection) -> Check:
    """Re-derive the row counts instead of trusting one number from one query.

    Per-dataset counts are what `fm store stats` prints and what the completeness report
    is judged against, so they are derived by grouping, summed, and compared with a
    straight COUNT of the table.
    """
    total = int(conn.execute("SELECT COUNT(*) AS n FROM series").fetchone()["n"])
    grouped = conn.execute("SELECT dataset, COUNT(*) AS n FROM series GROUP BY dataset").fetchall()
    derived = sum(int(row["n"]) for row in grouped)
    problems: list[str] = []
    if derived != total:
        problems.append(f"per-dataset counts sum to {derived}, a straight count says {total}")
    return Check(name="series_counts", ok=not problems, checked=total, problems=problems)


def _verify_payloads(conn: sqlite3.Connection, *, sample: int | None) -> Check:
    """Re-hash every retained payload and compare with what the row claims."""
    problems: list[str] = []
    checked = 0
    sql = "SELECT content_hash, dataset, encoding, bytes, stored_bytes, body FROM raw_payload ORDER BY rowid"
    if sample:
        sql += f" LIMIT {int(sample)}"
    for row in conn.execute(sql):
        checked += 1
        try:
            body = raw_store.decode_body(row["body"], row["encoding"])
        except (OSError, ValueError, UnicodeDecodeError, EOFError, TypeError) as exc:
            problems.append(f"{row['dataset']} {row['content_hash'][:12]}: cannot decompress ({type(exc).__name__})")
            continue
        digest = raw_store.content_hash(body)
        if digest != row["content_hash"]:
            problems.append(f"{row['dataset']} {row['content_hash'][:12]}: hash drift (re-hashed {digest[:12]})")
        if int(row["bytes"]) != len(body.encode("utf-8")):
            problems.append(f"{row['dataset']} {row['content_hash'][:12]}: claimed size {row['bytes']} != actual")
        if int(row["stored_bytes"]) != len(row["body"]):
            problems.append(f"{row['dataset']} {row['content_hash'][:12]}: claimed blob size != actual")
    return Check(name="raw_payload_hashes", ok=not problems, checked=checked, problems=_truncate(problems))


# --- export ----------------------------------------------------------------
SERIES_CSV_COLUMNS = ("dataset", "ts_utc", "source_id")
RAW_CSV_COLUMNS = (
    "content_hash",
    "dataset",
    "source",
    "source_id",
    "requested_start",
    "requested_end",
    "fetched_at",
    "bytes",
    "stored_bytes",
    "encoding",
    "body",
)


def iter_series_records(
    conn: sqlite3.Connection,
    *,
    dataset: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> Iterator[dict[str, Any]]:
    """Normalized rows as plain dicts, oldest first - the export record shape."""
    sql = "SELECT dataset, ts_utc, payload, source_id FROM series"
    where: list[str] = []
    params: list[object] = []
    if dataset:
        where.append("dataset = ?")
        params.append(dataset)
    if since is not None:
        where.append("ts_utc >= ?")
        params.append(ensure_utc(since, field="since").isoformat())
    if until is not None:
        where.append("ts_utc < ?")
        params.append(ensure_utc(until, field="until").isoformat())
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ts_utc, dataset"
    for row in conn.execute(sql, params):
        yield {
            "dataset": row["dataset"],
            "ts_utc": row["ts_utc"],
            "source_id": row["source_id"],
            "values": json.loads(row["payload"]),
        }


def export_field_names(conn: sqlite3.Connection, **filters) -> list[str]:
    """Every field name appearing in the exported rows - the CSV columns, found in one pass."""
    names: set[str] = set()
    for record in iter_series_records(conn, **filters):
        names.update(record["values"])
    return sorted(names)


def _check_format(fmt: str) -> None:
    if fmt not in ("jsonl", "csv"):
        raise StoreError(f"unknown export format {fmt!r}", hint="use jsonl or csv")


def _write_jsonl(handle, records: Iterator[dict[str, Any]]) -> int:
    written = 0
    for record in records:
        handle.write(json.dumps(record, sort_keys=True, default=str) + "\n")
        written += 1
    return written


def _write_csv(handle, columns: Sequence[str], records: Iterator[dict[str, Any]]) -> int:
    writer = csv.writer(handle)
    writer.writerow(list(columns))
    written = 0
    for record in records:
        row: list[object] = []
        for column in columns:
            value = record.get(column, "")
            if isinstance(value, dict):
                value = json.dumps(value, sort_keys=True)
            row.append("" if value is None else value)
        writer.writerow(row)
        written += 1
    return written


def export_series(
    conn: sqlite3.Connection,
    handle,
    *,
    fmt: str = "jsonl",
    dataset: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    fields: Sequence[str] | None = None,
) -> int:
    """Write the normalized series out and return the number of rows written.

    CSV is **wide**: one column per field, so a spreadsheet gets one row per sample. A
    field a row does not carry is an empty cell, which keeps "not measured" distinct from
    zero. JSONL keeps the nested shape: one object per row, ``values`` included.
    """
    _check_format(fmt)
    if fmt == "jsonl":
        return _write_jsonl(handle, iter_series_records(conn, dataset=dataset, since=since, until=until))
    names = list(fields) if fields else export_field_names(conn, dataset=dataset, since=since, until=until)
    columns = list(SERIES_CSV_COLUMNS) + names

    def rows() -> Iterator[dict[str, Any]]:
        for record in iter_series_records(conn, dataset=dataset, since=since, until=until):
            flat: dict[str, Any] = {column: record.get(column, "") for column in SERIES_CSV_COLUMNS}
            for name in names:
                flat[name] = record["values"].get(name, "")
            yield flat

    return _write_csv(handle, columns, rows())


def export_raw(
    conn: sqlite3.Connection,
    handle,
    *,
    fmt: str = "jsonl",
    dataset: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> int:
    """Write the original payloads out, body included: the copy you can re-derive from."""
    _check_format(fmt)
    payloads = raw_store.iter_payloads(conn, dataset=dataset, since=since, until=until)
    records = (payload.as_dict() for payload in payloads)
    if fmt == "jsonl":
        return _write_jsonl(handle, records)
    return _write_csv(handle, RAW_CSV_COLUMNS, records)


# --- backup ----------------------------------------------------------------
@dataclass(frozen=True)
class Backup:
    """One consistent snapshot, and what checking it on the way out found."""

    path: Path
    bytes: int
    pages: int
    ok: bool

    def as_dict(self) -> dict[str, object]:
        return {"path": str(self.path), "bytes": self.bytes, "pages": self.pages, "ok": self.ok}


def backup(source: Path | str, destination: Path | str) -> Backup:
    """Copy the store to a single file with SQLite's online backup API.

    ``Connection.backup`` rather than a file copy, because the store is in WAL mode and
    may be being written to: the API copies pages under a read lock and produces a
    snapshot of *committed* state, while ``cp`` of a live WAL database can produce a file
    that merely looks like a database. The copy is integrity-checked before it is
    reported as good.
    """
    source = Path(source)
    destination = Path(destination)
    if not source.exists():
        raise StoreError(f"no store at {source}", hint="point --db at the store fm created")
    if destination.exists() and destination.resolve() == source.resolve():
        raise StoreError("refusing to back a store up onto itself", hint="choose a different --output path")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()
    origin = schema.connect(source)
    try:
        target = sqlite3.connect(str(destination))
        try:
            origin.backup(target)
            target.commit()
            pages = int(target.execute("PRAGMA page_count").fetchone()[0])
            integrity = [row[0] for row in target.execute("PRAGMA integrity_check").fetchall()]
        finally:
            target.close()
    finally:
        origin.close()
    return Backup(path=destination, bytes=destination.stat().st_size, pages=pages, ok=integrity == ["ok"])


# --- prune -----------------------------------------------------------------
@dataclass
class PruneResult:
    """What a prune did - or, by default, what it *would* have done."""

    days: int
    cutoff: datetime
    execute: bool
    what: tuple[str, ...] = ()
    removed: dict[str, int] = field(default_factory=dict)
    bytes_freed: int = 0
    vacuumed: bool = False

    @property
    def dry_run(self) -> bool:
        return not self.execute

    @property
    def rows(self) -> int:
        return sum(self.removed.values())

    def as_dict(self) -> dict[str, object]:
        return {
            "days": self.days,
            "cutoff": self.cutoff.isoformat(),
            "dry_run": self.dry_run,
            "what": list(self.what),
            "removed": dict(self.removed),
            "bytes_freed": self.bytes_freed,
            "vacuumed": self.vacuumed,
        }


def prune(
    store,
    *,
    days: int,
    what: Sequence[str] = DEFAULT_PRUNE_WHAT,
    execute: bool = False,
    vacuum: bool = False,
    now: datetime | None = None,
) -> PruneResult:
    """Delete data older than ``days`` - **dry-run unless ``execute`` is True**.

    The default is the safe direction: a mistyped retention command must not be able to
    delete a year of someone's heart data on the strength of one flag. Nothing here
    touches the watermarks, so a pruned day is still a day fm knows it fetched - and, if
    the source still has it, can fetch again.
    """
    if days < 1:
        raise StoreError("--days must be at least 1", hint="retention counts the days you keep, not the days you drop")
    requested = tuple(what or DEFAULT_PRUNE_WHAT)
    if "all" in requested:
        requested = ("raw", "series", "quarantine")
    unknown = [name for name in requested if name not in PRUNE_WHAT]
    if unknown:
        raise StoreError(f"unknown prune target(s): {', '.join(unknown)}", hint=f"pick from: {', '.join(PRUNE_WHAT)}")
    cutoff = ensure_utc(now or utcnow()) - timedelta(days=days)
    result = PruneResult(days=days, cutoff=cutoff, execute=execute, what=requested)

    conn = store.conn
    stamp = cutoff.isoformat()
    counts: dict[str, int] = {}
    prospective_bytes = 0
    if "raw" in requested:
        row = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(stored_bytes), 0) AS bytes FROM raw_payload WHERE fetched_at < ?",
            (stamp,),
        ).fetchone()
        counts["raw_payload"] = int(row["n"])
        prospective_bytes += int(row["bytes"])
    if "series" in requested:
        counts["series"] = int(conn.execute("SELECT COUNT(*) AS n FROM series WHERE ts_utc < ?", (stamp,)).fetchone()["n"])
    if "quarantine" in requested:
        counts["quarantine"] = int(
            conn.execute("SELECT COUNT(*) AS n FROM quarantine WHERE quarantined_at < ?", (stamp,)).fetchone()["n"]
        )
    result.removed = counts
    result.bytes_freed = prospective_bytes

    if not execute:
        return result

    freed = 0
    if "raw" in requested:
        _, freed = store.delete_raw_before(cutoff)
    if "series" in requested:
        store.delete_series_before(cutoff)
    if "quarantine" in requested:
        store.delete_quarantine_before(cutoff)
    result.bytes_freed = freed
    if vacuum and result.rows:
        previous_level = conn.isolation_level
        conn.isolation_level = None  # VACUUM cannot run inside a transaction
        try:
            conn.execute("VACUUM")
            # In WAL mode the pages VACUUM freed are still sitting in the -wal file;
            # without this the promise "--vacuum gave the space back" would be false.
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.isolation_level = previous_level
        result.vacuumed = True
    return result


__all__ = [
    "DEFAULT_PRUNE_WHAT",
    "DEFAULT_RETENTION_DAYS",
    "PRUNE_WHAT",
    "Backup",
    "Check",
    "PruneResult",
    "Verification",
    "backup",
    "export_field_names",
    "export_raw",
    "export_series",
    "human_bytes",
    "iter_series_records",
    "prune",
    "stats",
    "verify",
]
