"""The import report: what landed, per dataset, in a file you can parse.

"Did that import anything?" has to be answerable without reading prose, so every run
- successful, partial or ruined - writes a JSON report into the data dir
(``<data-dir>/imports/<run-id>.json``) and prints it with ``--json``. Per dataset it
carries the requested range, chunks fetched, rows inserted, rows already present,
quarantined payloads, bytes stored, requests made, duration and the resulting
watermark. Failures name the dataset and the reason.

The exit codes are distinct on purpose, because a cron job or an agent reading the
code cannot tell "fine" from "fine but missing three datasets" out of a zero:

===== =========================================================== ==================
code  meaning                                                     fm import status
===== =========================================================== ==================
0     everything asked for landed                                 ``ok`` / ``skipped``
1     nothing landed - total failure                              ``failed`` (all)
3     some datasets landed, some did not                          ``partial``
4     there was no store to write to                              (raised, not run)
5     it landed, with warnings: quarantined payloads or chunks    ``ok`` + warnings
130   interrupted (Ctrl-C); completed chunks are stored           ``interrupted``
===== =========================================================== ==================
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

EXIT_OK = 0
EXIT_TOTAL_FAILURE = 1
EXIT_PARTIAL_FAILURE = 3
EXIT_NO_STORE = 4
EXIT_WITH_WARNINGS = 5
EXIT_INTERRUPTED = 130

STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_PARTIAL = "partial"
STATUS_SKIPPED = "skipped"
STATUS_INTERRUPTED = "interrupted"

#: How many report files to keep; older ones are pruned by :meth:`ImportReport.write`.
REPORT_KEEP = 20


@dataclass
class DatasetReport:
    """One dataset's half of one run."""

    dataset: str
    source: str = "api"
    since: str | None = None
    until: str | None = None
    status: str = STATUS_OK
    chunks_planned: int = 0
    chunks_fetched: int = 0
    requests: int = 0
    payloads: int = 0
    payloads_duplicate: int = 0
    rows_inserted: int = 0
    rows_duplicate: int = 0
    quarantined: int = 0
    bytes_stored: int = 0
    duration_ms: int = 0
    watermark: str | None = None
    error: str | None = None
    notes: list[str] = field(default_factory=list)
    info: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "source": self.source,
            "since": self.since,
            "until": self.until,
            "status": self.status,
            "chunks_planned": self.chunks_planned,
            "chunks_fetched": self.chunks_fetched,
            "requests": self.requests,
            "payloads": self.payloads,
            "payloads_duplicate": self.payloads_duplicate,
            "rows_inserted": self.rows_inserted,
            "rows_duplicate": self.rows_duplicate,
            "quarantined": self.quarantined,
            "bytes_stored": self.bytes_stored,
            "duration_ms": self.duration_ms,
            "watermark": self.watermark,
            "error": self.error,
            "notes": list(self.notes),
            "info": list(self.info),
            "files": list(self.files),
        }

    @property
    def warning(self) -> bool:
        """A dataset that "worked" but should have said more.

        Deliberately excludes ``info``: "no HRV recorded that week" is not a warning,
        a payload we could not parse is.
        """
        return bool(self.quarantined or self.error or self.notes)

    def merged(self, other: "DatasetReport") -> "DatasetReport":
        """Fold a second attempt at the same dataset into one line (used by ``--retry``)."""
        self.chunks_fetched += other.chunks_fetched
        self.requests += other.requests
        self.payloads += other.payloads
        self.payloads_duplicate += other.payloads_duplicate
        self.rows_inserted += other.rows_inserted
        self.rows_duplicate += other.rows_duplicate
        self.quarantined += other.quarantined
        self.bytes_stored += other.bytes_stored
        self.duration_ms += other.duration_ms
        self.notes.extend(n for n in other.notes if n not in self.notes)
        self.info.extend(i for i in other.info if i not in self.info)
        self.files.extend(f for f in other.files if f not in self.files)
        if other.watermark:
            self.watermark = other.watermark
        if other.status == STATUS_OK:
            self.status = STATUS_OK
            self.error = None
        return self


@dataclass
class ImportReport:
    """One whole import run."""

    run_id: str
    source: str = "api"
    since: str | None = None
    until: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    status: str = STATUS_OK
    datasets: list[DatasetReport] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    path: Path | None = None

    # --- shape --------------------------------------------------------------
    @property
    def duration_ms(self) -> int:
        if self.started_at and self.finished_at:
            return int((self.finished_at - self.started_at).total_seconds() * 1000)
        return sum(d.duration_ms for d in self.datasets)

    def dataset(self, name: str) -> DatasetReport | None:
        return next((d for d in self.datasets if d.dataset == name), None)

    def add(self, report: DatasetReport) -> DatasetReport:
        existing = self.dataset(report.dataset)
        if existing is not None:
            return existing.merged(report)
        self.datasets.append(report)
        return report

    @property
    def totals(self) -> dict[str, int]:
        keys = (
            "chunks_fetched",
            "requests",
            "payloads",
            "payloads_duplicate",
            "rows_inserted",
            "rows_duplicate",
            "quarantined",
            "bytes_stored",
        )
        totals = {key: sum(int(getattr(d, key)) for d in self.datasets) for key in keys}
        totals["duration_ms"] = self.duration_ms
        totals["datasets"] = len(self.datasets)
        return totals

    @property
    def warnings(self) -> list[str]:
        """Things a reader should look at - run-level ``notes`` are informational."""
        out = [f"{d.dataset}: {note}" for d in self.datasets for note in d.notes]
        out.extend(f"{d.dataset}: {d.error}" for d in self.datasets if d.error)
        out.extend(f"{d.dataset}: {d.quarantined} payload(s) quarantined" for d in self.datasets if d.quarantined)
        return out

    @property
    def exit_code(self) -> int:
        """Distinct codes for the four outcomes a caller can act on differently."""
        statuses = [d.status for d in self.datasets]
        if self.status == STATUS_INTERRUPTED or STATUS_INTERRUPTED in statuses:
            return EXIT_INTERRUPTED
        active = [s for s in statuses if s != STATUS_SKIPPED]
        if not active:
            return EXIT_OK
        if all(s == STATUS_FAILED for s in active):
            return EXIT_TOTAL_FAILURE
        if any(s in (STATUS_FAILED, STATUS_PARTIAL) for s in active):
            return EXIT_PARTIAL_FAILURE
        if any(d.warning for d in self.datasets):
            return EXIT_WITH_WARNINGS
        return EXIT_OK

    @property
    def ok(self) -> bool:
        return self.exit_code in (EXIT_OK, EXIT_WITH_WARNINGS)

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "source": self.source,
            "since": self.since,
            "until": self.until,
            "status": self.status,
            "exit_code": self.exit_code,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "duration_ms": self.duration_ms,
            "notes": list(self.notes),
            "warnings": self.warnings,
            "totals": self.totals,
            "datasets": [d.as_dict() for d in sorted(self.datasets, key=lambda d: d.dataset)],
            "report_path": str(self.path) if self.path else None,
        }

    # --- the artifact -------------------------------------------------------
    def write(self, directory: Path | str, *, keep: int = REPORT_KEEP) -> Path:
        """Write ``<directory>/<run-id>.json`` and prune back to the newest ``keep``."""
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / f"{self.run_id}.json"
        self.path.write_text(json.dumps(self.as_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        if keep > 0:
            self.prune(directory, keep)
        return self.path

    @staticmethod
    def prune(directory: Path, keep: int) -> list[Path]:
        """Delete all but the ``keep`` newest reports. Returns what was removed."""
        reports = sorted(Path(directory).glob("*.json"), key=lambda p: p.name, reverse=True)
        removed = []
        for stale in reports[max(0, keep) :]:
            stale.unlink(missing_ok=True)
            removed.append(stale)
        return removed

    @staticmethod
    def latest(directory: Path | str) -> Path | None:
        """The newest report file, which is what ``fm import report`` reads by default."""
        reports = sorted(Path(directory).glob("*.json"), key=lambda p: p.name, reverse=True)
        return reports[0] if reports else None

    @staticmethod
    def read(path: Path | str) -> dict[str, Any]:
        return json.loads(Path(path).read_text(encoding="utf-8"))

    # --- human view ---------------------------------------------------------
    def table_lines(self) -> Sequence[str]:  # pragma: no cover - display helper
        from ..cli.common import render_table

        rows = [
            (
                d.dataset,
                d.status,
                f"{d.chunks_fetched}/{d.chunks_planned}" if d.chunks_planned else str(d.chunks_fetched),
                str(d.rows_inserted),
                str(d.rows_duplicate),
                str(d.quarantined),
                str(d.requests),
                d.watermark or "-",
                d.error or "",
            )
            for d in sorted(self.datasets, key=lambda d: d.dataset)
        ]
        return [
            render_table(
                ("DATASET", "STATUS", "CHUNKS", "INSERTED", "DUPLICATE", "QUARANTINED", "REQS", "WATERMARK", "ERROR"),
                rows,
            )
        ]


def new_run_id(prefix: str = "import") -> str:
    """Sortable, collision-resistant run id, e.g. ``import-20260601T091500Z-3f9c1a``."""
    import secrets
    from ..timeutil import utcnow

    stamp = utcnow().strftime("%Y%m%dT%H%M%S")
    return f"{prefix}-{stamp}Z-{secrets.token_hex(3)}"


def summarise_runs(runs: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse per-dataset run rows into one row per run id (for ``fm import history``)."""
    grouped: dict[str, dict[str, Any]] = {}
    for run in runs:
        entry = grouped.setdefault(
            run["run_id"],
            {
                "run_id": run["run_id"],
                "source": run.get("source", "api"),
                "started_at": run.get("started_at"),
                "datasets": [],
                "statuses": [],
                "rows_inserted": 0,
                "requests": 0,
                "quarantined": 0,
            },
        )
        entry["datasets"].append(run["dataset"])
        entry["statuses"].append(run.get("status"))
        entry["rows_inserted"] += int(run.get("rows_inserted") or 0)
        entry["requests"] += int(run.get("requests") or 0)
        entry["quarantined"] += int(run.get("quarantined") or 0)
    out = list(grouped.values())
    for entry in out:
        failed = [s for s in entry["statuses"] if s in (STATUS_FAILED, STATUS_PARTIAL)]
        if failed:
            entry["status"] = STATUS_PARTIAL if len(failed) < len(entry["statuses"]) else STATUS_FAILED
        else:
            entry["status"] = STATUS_OK
        entry["datasets"] = sorted(set(entry["datasets"]))
    return sorted(out, key=lambda e: str(e.get("started_at") or ""), reverse=True)


__all__ = [
    "DatasetReport",
    "ImportReport",
    "REPORT_KEEP",
    "STATUS_FAILED",
    "STATUS_INTERRUPTED",
    "STATUS_OK",
    "STATUS_PARTIAL",
    "STATUS_SKIPPED",
    "new_run_id",
    "summarise_runs",
]
