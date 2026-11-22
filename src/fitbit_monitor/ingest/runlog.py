"""The run log: one row per dataset per import run, kept in the store.

The JSON report is the artifact for a machine; this table is the same facts in the
database, which is what ``fm import history`` reads and what answers "what did that
run do?" a week later from a shell. It also gives the post-commit loop and
``fm doctor`` a single query to point at.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

STATUS_COLUMNS = (
    "run_id",
    "dataset",
    "source",
    "since",
    "until",
    "status",
    "chunks",
    "requests",
    "rows_inserted",
    "rows_duplicate",
    "payloads_duplicate",
    "quarantined",
    "bytes_stored",
    "duration_ms",
    "watermark",
    "error",
    "started_at",
    "finished_at",
)


@dataclass(frozen=True)
class DatasetRun:
    """One ``dataset_run`` row."""

    run_id: str
    dataset: str
    source: str
    since: str | None
    until: str | None
    status: str
    chunks: int = 0
    requests: int = 0
    rows_inserted: int = 0
    rows_duplicate: int = 0
    payloads_duplicate: int = 0
    quarantined: int = 0
    bytes_stored: int = 0
    duration_ms: int = 0
    watermark: str | None = None
    error: str | None = None
    started_at: str | None = None
    finished_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "dataset": self.dataset,
            "source": self.source,
            "since": self.since,
            "until": self.until,
            "status": self.status,
            "chunks": self.chunks,
            "requests": self.requests,
            "rows_inserted": self.rows_inserted,
            "rows_duplicate": self.rows_duplicate,
            "payloads_duplicate": self.payloads_duplicate,
            "quarantined": self.quarantined,
            "bytes_stored": self.bytes_stored,
            "duration_ms": self.duration_ms,
            "watermark": self.watermark,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class RunLog:
    """Writes and reads ``dataset_run`` through a writable store handle."""

    def __init__(self, store) -> None:
        self.store = store
        self.conn = store.conn

    def record(self, run: DatasetRun) -> None:
        columns = ", ".join(STATUS_COLUMNS)
        placeholders = ", ".join("?" for _ in STATUS_COLUMNS)
        self.conn.execute(
            f"INSERT INTO dataset_run ({columns}) VALUES ({placeholders})",
            tuple(getattr(run, name) for name in STATUS_COLUMNS),
        )

    def record_report(self, report, *, started_at: str, finished_at: str) -> list[DatasetRun]:
        """One row per dataset in an :class:`~fitbit_monitor.ingest.report.ImportReport`."""
        rows = [
            DatasetRun(
                run_id=report.run_id,
                dataset=dataset.dataset,
                source=dataset.source,
                since=dataset.since,
                until=dataset.until,
                status=dataset.status,
                chunks=dataset.chunks_fetched,
                requests=dataset.requests,
                rows_inserted=dataset.rows_inserted,
                rows_duplicate=dataset.rows_duplicate,
                payloads_duplicate=dataset.payloads_duplicate,
                quarantined=dataset.quarantined,
                bytes_stored=dataset.bytes_stored,
                duration_ms=dataset.duration_ms,
                watermark=dataset.watermark,
                error=dataset.error,
                started_at=started_at,
                finished_at=finished_at,
            )
            for dataset in report.datasets
        ]
        for row in rows:
            self.record(row)
        self.store.commit()
        return rows

    def recent(self, limit: int = 20, *, dataset: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM dataset_run"
        params: list[object] = []
        if dataset:
            sql += " WHERE dataset = ?"
            params.append(dataset)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        return [dict(r) for r in self.conn.execute(sql, params)]

    def last_run_id(self) -> str | None:
        row = self.conn.execute("SELECT run_id FROM dataset_run ORDER BY id DESC LIMIT 1").fetchone()
        return row["run_id"] if row else None

    def outcomes(self, source: str | None = None) -> dict[str, dict[str, Any]]:
        """Each dataset's most recent outcome: status, error, run and start time.

        ``fm import watermarks`` prints this beside the watermark, because "how far did
        it get" and "how did it end" are one question in practice.
        """
        sql = "SELECT dataset, source, status, error, run_id, started_at FROM dataset_run"
        if source:
            sql += " WHERE source = ?"
            rows = self.conn.execute(sql, (source,))
        else:
            rows = self.conn.execute(sql)
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:  # ordered by rowid, so the last write per dataset wins
            latest[row["dataset"]] = dict(row)
        for entry in latest.values():
            entry.pop("source", None)
        return latest

    def last_window(self, run_id: str | None = None) -> tuple[date, date] | None:
        """The ``--since/--until`` a previous run was asked for (default: the last one).

        ``fm import --resume`` needs it: a run that stopped halfway has to be re-planned
        over the *original* window, not over the last few days.
        """
        run_id = run_id or self.last_run_id()
        if not run_id:
            return None
        row = self.conn.execute(
            "SELECT MIN(since) AS since, MAX(until) AS until FROM dataset_run WHERE run_id = ?", (run_id,)
        ).fetchone()
        if not row or not row["since"] or not row["until"]:
            return None
        try:
            return date.fromisoformat(str(row["since"])[:10]), date.fromisoformat(str(row["until"])[:10])
        except ValueError:
            return None

    def for_run(self, run_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM dataset_run WHERE run_id = ? ORDER BY id", (run_id,))]

    def datasets_needing_retry(self, run_id: str | None = None) -> tuple[str, ...]:
        """Datasets whose latest attempt failed or only partly worked, newest run first."""
        run_id = run_id or self.last_run_id()
        if not run_id:
            return ()
        return tuple(
            r["dataset"]
            for r in self.conn.execute(
                "SELECT dataset, status FROM dataset_run WHERE run_id = ? ORDER BY id", (run_id,)
            )
            if r["status"] in ("failed", "partial", "interrupted")
        )


__all__ = ["DatasetRun", "RunLog", "STATUS_COLUMNS"]
