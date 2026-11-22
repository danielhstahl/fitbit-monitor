"""The ad-hoc import: plan a window, fetch it, store it, say what happened.

This is the composition layer the ticket is really about - ``fm import`` is a thin
skin over the pieces here:

**Plan first.** :class:`ImportPlanner` turns ``--since/--until/--dataset/--full-history``
into an :class:`ImportPlan`: per dataset, the days, the chunks, and the request cost.
``--dry-run`` prints that and stops, so the expensive question ("how many calls is
this going to cost?") is answerable before spending them.

**Chunks are the unit of durability.** A chunk is a few days of one dataset; its
payloads and rows commit together. Ctrl-C or a rate limit between chunks is therefore
always safe: what is stored is stored, and the watermark advances only over days whose
requests all succeeded, so the next run re-fetches exactly the ambiguous part. Rows are
idempotent, so ambiguity costs requests, never correctness.

**Failure is per dataset.** A dataset that 403s (scope missing, no SpO2 on the account)
is marked ``failed`` with the reason while the others continue, and the run exits
*partially* (3) instead of silently zero.

**Report everything.** :class:`~fitbit_monitor.ingest.report.ImportReport` gets one
:class:`~fitbit_monitor.ingest.report.DatasetReport` per dataset, is written to the data
dir, recorded in ``dataset_run``, and mapped onto distinct exit codes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Callable, Protocol, Sequence

from ..api.catalog import CATALOG, Payload, Request, daterange, lookup, permitted_datasets
from ..api.client import FitbitClient
from ..api.errors import (
    APIUnavailableError,
    CredentialsExpiredError,
    FitbitAPIError,
    HTTPError,
    RateLimitError,
)
from ..config import ImportSettings
from ..errors import UsageError
from ..store.watermarks import Watermarks
from ..timeutil import parse_date_arg, utcnow
from .pipeline import Ingest, IngestStats
from .report import (
    STATUS_FAILED,
    STATUS_INTERRUPTED,
    STATUS_OK,
    STATUS_PARTIAL,
    STATUS_SKIPPED,
    DatasetReport,
    ImportReport,
    new_run_id,
)

#: Statuses where retrying the same request would fail the same way.
FATAL_STATUSES = (400, 401, 403)
#: Statuses that simply mean "nothing stored for those days".
EMPTY_STATUSES = (404, 410)


@dataclass(frozen=True)
class ChunkPlan:
    """A few days of one dataset, fetched and committed together."""

    dataset: str
    start: date
    stop: date
    requests: tuple[Request, ...] = ()

    @property
    def label(self) -> str:
        return self.start.isoformat() if self.start == self.stop else f"{self.start}..{self.stop}"

    @property
    def days(self) -> tuple[date, ...]:
        return tuple(daterange(self.start, self.stop))

    def paths(self) -> tuple[str, ...]:
        """The concrete paths this chunk will ask for, in order."""
        return tuple(request.path() for request in self.requests)

    def as_dict(self) -> dict[str, Any]:
        return {"label": self.label, "requests": len(self.requests), "days": len(self.days)}


@dataclass
class DatasetPlan:
    """One dataset's share of a plan."""

    dataset: str
    since: date
    until: date
    chunk_days: int
    chunks: list[ChunkPlan] = field(default_factory=list)
    notes: tuple[str, ...] = ()
    skipped: bool = False
    reason: str | None = None

    @property
    def days(self) -> tuple[date, ...]:
        return tuple(daterange(self.since, self.until)) if self.until >= self.since else ()

    @property
    def requests(self) -> int:
        return sum(len(chunk.requests) for chunk in self.chunks)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "since": self.since.isoformat(),
            "until": self.until.isoformat(),
            "days": len(self.days),
            "chunk_days": self.chunk_days,
            "chunks": len(self.chunks),
            "requests": self.requests,
            "skipped": self.skipped,
            "reason": self.reason,
            "notes": list(self.notes),
        }


@dataclass
class ImportPlan:
    """Everything a run intends to do - the thing ``--dry-run`` shows."""

    source: str
    since: date
    until: date
    datasets: list[DatasetPlan] = field(default_factory=list)
    probe_requests: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def requests(self) -> int:
        return sum(d.requests for d in self.datasets) + self.probe_requests

    @property
    def chunks(self) -> int:
        return sum(len(d.chunks) for d in self.datasets)

    @property
    def days(self) -> int:
        return len(tuple(daterange(self.since, self.until)))

    @property
    def active(self) -> list[DatasetPlan]:
        return [d for d in self.datasets if not d.skipped]

    def dataset(self, name: str) -> DatasetPlan | None:
        return next((d for d in self.datasets if d.dataset == name), None)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "since": self.since.isoformat(),
            "until": self.until.isoformat(),
            "days": self.days,
            "requests_estimate": self.requests,
            "probe_requests": self.probe_requests,
            "chunks": self.chunks,
            "notes": list(self.notes),
            "datasets": [d.as_dict() for d in self.datasets],
        }

    def describe(self) -> list[str]:
        """The ``--dry-run`` output: what would be fetched, and what it would cost."""
        lines = [f"planned import ({self.source}) {self.since} .. {self.until} ({self.days} days)"]
        for entry in self.datasets:
            if entry.skipped:
                lines.append(f"  {entry.dataset:<15} skipped - {entry.reason}")
                continue
            lines.append(
                f"  {entry.dataset:<15} {entry.since} .. {entry.until}  "
                f"{len(entry.chunks)} chunk(s) of {entry.chunk_days}d, ~{entry.requests} requests"
            )
            for note in entry.notes:
                lines.append(f"      note: {note}")
        lines.append(f"  total requests (approx): {self.requests}")
        lines.extend(f"note: {note}" for note in self.notes)
        return lines


class Progress(Protocol):
    """Progress seam: the CLI prints it, tests collect it."""

    def dataset_started(self, plan: DatasetPlan) -> None: ...

    def chunk_done(self, dataset: str, index: int, total: int, label: str, stats: IngestStats, remaining: int | None) -> None: ...

    def note(self, text: str) -> None: ...

    def dataset_finished(self, report: DatasetReport) -> None: ...


class NullProgress:
    """The no-op sink used by tests and ``--quiet``."""

    def dataset_started(self, plan: DatasetPlan) -> None:
        return None

    def chunk_done(self, dataset, index, total, label, stats, remaining) -> None:
        return None

    def note(self, text: str) -> None:
        return None

    def dataset_finished(self, report: DatasetReport) -> None:
        return None


def day_window(since: str | None = None, until: str | None = None, *, default_days: int = 7) -> tuple[date, date]:
    """Resolve the two date arguments to an inclusive pair of calendar days.

    ``--until`` given as a bare date means the *end* of that day (the same rule
    ``fm plugins run`` uses); a full timestamp is a hard, exclusive bound.
    """
    if until:
        day_only = _is_day_only(until)
        until_dt = parse_date_arg(until, end_of_day=day_only)
        # ``--until 2026-05-14`` means *through* the 14th: parse_date_arg hands back the
        # next midnight, so the last included day is one microsecond before it.
        stop_day = (until_dt - timedelta(microseconds=1)).date()
    else:
        stop_day = utcnow().date()
    if since:
        start_day = parse_date_arg(since).date()
    else:
        start_day = stop_day - timedelta(days=max(1, default_days) - 1)
    if stop_day < start_day:
        raise UsageError(
            f"--until ({stop_day}) must not be before --since ({start_day})",
            hint="plain dates are inclusive of both ends: --since 2026-05-01 --until 2026-05-14",
        )
    return start_day, stop_day


def _is_day_only(text: str) -> bool:
    stripped = text.strip()
    return len(stripped) == 10 or stripped.lower() in ("today", "yesterday")


class Interrupted(BaseException):
    """Ctrl-C, turned into a clean stop instead of a half-written chunk."""


class StopRun(Exception):
    """A dataset failed for a reason every later dataset would share (rate limit, no network)."""


class ImportPlanner:
    """Works out what to fetch, including where each dataset's watermark says to resume."""

    def __init__(
        self,
        client: FitbitClient,
        watermarks: Watermarks,
        settings: ImportSettings | None = None,
        *,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.client = client
        self.watermarks = watermarks
        self.settings = settings or ImportSettings()
        self.clock = clock
        self.profile_error: str | None = None
        self._profile: dict[str, Any] | None = None

    @property
    def profile(self) -> dict[str, Any]:
        """The profile call doubles as the "what does this account allow" probe."""
        if self._profile is None:
            try:
                self._profile = self.client.profile()
            except (FitbitAPIError, OSError) as exc:
                # The profile doubles as a probe; a failure here (revoked scope, rate
                # limit, network) must not lose the import. Dataset selection falls back
                # to the token's granted scopes and the reason is kept for the report.
                self._profile = {}
                self.profile_error = f"profile probe failed: {exc}"
        return self._profile

    def build(
        self,
        *,
        datasets: Sequence[str] | None = None,
        since: str | None = None,
        until: str | None = None,
        full_history: bool = False,
        source: str = "api",
        resume_from: date | None = None,
    ) -> ImportPlan:
        """Plan a window.

        ``resume_from`` is where a stopped run originally started (the previous run's
        recorded ``--since``): a dataset with no watermark starts there instead of at
        the short default window, so ``--resume`` on a half-finished year-long backfill
        continues the year rather than quietly narrowing to the last week.
        """
        start_day, stop_day = day_window(since, until, default_days=self.settings.recent_days)
        if resume_from is not None and resume_from < start_day:
            # A resume covers the window the previous run was asked for, not the short
            # default window: otherwise the days between that run's start and today's
            # default window would be silently skipped.
            start_day = resume_from
        names, notes = self._resolve_datasets(datasets)
        probed = 0 if self._profile is None else 1
        plan = ImportPlan(source=source, since=start_day, until=stop_day, probe_requests=probed, notes=notes)
        for name in names:
            plan.datasets.append(
                self._dataset_plan(
                    name,
                    start_day,
                    stop_day,
                    explicit_since=bool(since),
                    full_history=full_history,
                    resume_from=resume_from,
                )
            )
        return plan

    # --- internals ----------------------------------------------------------
    def _resolve_datasets(self, requested: Sequence[str] | None) -> tuple[tuple[str, ...], list[str]]:
        notes: list[str] = []
        if requested:
            names: list[str] = []
            for item in requested:
                for part in str(item).split(","):
                    part = part.strip()
                    if part:
                        lookup(part)
                        if part not in names:
                            names.append(part)
            if not names:
                raise UsageError("no datasets selected", hint="e.g. --dataset heart_rate --dataset sleep")
            return tuple(names), notes
        granted = tuple(self.client.tokens.scopes) if self.client.authenticated else ()
        allowed, skipped = permitted_datasets(self.profile, granted)
        notes.extend(f"not imported: {item}" for item in skipped)
        if self.profile_error:
            notes.append(self.profile_error)
        return allowed, notes

    def _dataset_plan(
        self,
        name: str,
        start_day: date,
        stop_day: date,
        *,
        explicit_since: bool,
        full_history: bool,
        resume_from: date | None = None,
    ) -> DatasetPlan:
        dataset = lookup(name)
        notes: list[str] = []
        chunk_days = self.settings.chunk_days_for(name, dataset.chunk_days)
        if explicit_since:
            first = start_day
        elif full_history:
            first = self._history_start(stop_day, notes)
        else:
            resume = self.watermarks.next_since(name)
            if resume is None:
                if resume_from is not None:
                    first = resume_from
                    notes.append(f"no watermark - resuming the window of the previous run from {first.isoformat()}")
                else:
                    first = min(stop_day - timedelta(days=max(1, self.settings.recent_days) - 1), start_day)
                    notes.append(
                        f"no watermark - starting {first.isoformat()} "
                        f"([import] recent_days = {self.settings.recent_days}); --full-history imports everything"
                    )
            else:
                # A default overlap: Fitbit recalculates sleep and HRV after a late
                # sync, and re-reading a day or two costs a request, not duplicates.
                # Only applied when there is still something new to fetch - a dataset
                # that is current stays a no-op rather than a daily refetch.
                overlap = max(0, int(getattr(self.settings, "overlap_days", 0)))
                first = resume if resume > stop_day else max(start_day, resume - timedelta(days=overlap))
                if first < resume:
                    notes.append(
                        f"re-fetching {(resume - first).days} already-imported day(s) "
                        f"([import] overlap_days = {overlap}) to catch late corrections"
                    )
        plan = DatasetPlan(dataset=name, since=first, until=stop_day, chunk_days=chunk_days, notes=tuple(notes))
        if first > stop_day:
            plan.skipped = True
            plan.reason = f"already fetched through {(first - timedelta(days=1)).isoformat()}"
            return plan
        for chunk_start in _step(first, stop_day, chunk_days):
            chunk_stop = min(chunk_start + timedelta(days=chunk_days - 1), stop_day)
            plan.chunks.append(
                ChunkPlan(
                    dataset=name,
                    start=chunk_start,
                    stop=chunk_stop,
                    requests=tuple(dataset.requests_in_chunk(chunk_start, chunk_stop)),
                )
            )
        return plan

    def _history_start(self, stop_day: date, notes: list[str]) -> date:
        raw = str(self.profile.get("user", {}).get("memberSinceDate") or "")
        if len(raw) >= 10:
            try:
                return date.fromisoformat(raw[:10])
            except ValueError:
                pass
        notes.append("no memberSinceDate in the profile; backfilling 365 days instead of the whole history")
        return stop_day - timedelta(days=364)


def _step(start: date, stop: date, days: int) -> list[date]:
    out: list[date] = []
    day = start
    while day <= stop:
        out.append(day)
        day += timedelta(days=max(1, days))
    return out


class Importer:
    """Runs an :class:`ImportPlan` and writes the report."""

    def __init__(
        self,
        *,
        client: FitbitClient,
        ingest: Ingest,
        watermarks: Watermarks,
        runlog=None,
        settings: ImportSettings | None = None,
        timezone: str | None = None,
        clock: Callable[[], datetime] = utcnow,
        progress: Progress | None = None,
        source: str = "api",
    ) -> None:
        self.client = client
        self.ingest = ingest
        self.watermarks = watermarks
        self.runlog = runlog
        self.settings = settings or ImportSettings()
        self.timezone = timezone or self.settings.timezone
        self.clock = clock
        self.progress: Progress = progress or NullProgress()
        self.source = source

    @staticmethod
    def resume_names(runlog, only: Sequence[str] | None = None) -> tuple[str, ...]:
        """Which datasets a ``--resume`` should cover.

        Datasets that stopped (interrupted, failed or left partial) come first; when the
        last run finished cleanly everything gets a pass, and the planner's watermark
        logic turns the up-to-date ones into no-ops.
        """
        stopped: list[str] = []
        if runlog is not None:
            for entry in runlog.recent(limit=200):
                if entry["status"] in ("failed", "interrupted", "partial") and entry["dataset"] not in stopped:
                    stopped.append(str(entry["dataset"]))
        if only:
            return tuple(name for name in only if not stopped or name in stopped) or tuple(only)
        return tuple(stopped) if stopped else tuple(sorted(CATALOG))

    def run(self, plan: ImportPlan, *, max_requests: int | None = None) -> ImportReport:
        report = ImportReport(
            run_id=new_run_id(f"import-{plan.source}"),
            source=plan.source,
            since=plan.since.isoformat(),
            until=plan.until.isoformat(),
            started_at=self.clock(),
            notes=list(plan.notes),
        )
        for entry in plan.datasets:
            if entry.skipped:
                report.add(
                    DatasetReport(
                        dataset=entry.dataset,
                        source=self.source,
                        since=entry.since.isoformat(),
                        until=entry.until.isoformat(),
                        status=STATUS_SKIPPED,
                        info=[entry.reason or "skipped"],
                    )
                )
        interrupted = False
        for entry in plan.active:
            try:
                self._run_dataset(entry, report, max_requests=max_requests)
            except (Interrupted, KeyboardInterrupt):
                interrupted = True
                break
            except StopRun as exc:
                report.notes.append(str(exc))
                break
        report.status = STATUS_INTERRUPTED if interrupted else self._overall(report)
        report.finished_at = self.clock()
        if self.runlog is not None:
            self.runlog.record_report(
                report,
                started_at=report.started_at.isoformat() if report.started_at else "",
                finished_at=report.finished_at.isoformat() if report.finished_at else "",
            )
        return report

    # --- one dataset --------------------------------------------------------
    def _run_dataset(self, entry: DatasetPlan, report: ImportReport, *, max_requests: int | None) -> None:
        dataset = lookup(entry.dataset)
        started = self.clock()
        result = DatasetReport(
            dataset=entry.dataset,
            source=self.source,
            since=entry.since.isoformat(),
            until=entry.until.isoformat(),
            chunks_planned=len(entry.chunks),
            info=list(entry.notes),
        )
        self.progress.dataset_started(entry)
        completed_days: list[date] = []
        last_ts: datetime | None = None
        index = 0
        stopped_by: str | None = None
        try:
            for index, chunk in enumerate(entry.chunks, start=1):
                if max_requests is not None and self.client.requests >= max_requests:
                    result.notes.append(f"stopped at --max-requests={max_requests}; re-run the same command to resume")
                    break
                requests_before = self.client.requests
                stats, chunk_last_ts = self._fetch_chunk(dataset, chunk, result)
                completed_days.extend(chunk.days)
                result.chunks_fetched += 1
                result.requests += self.client.requests - requests_before
                result.rows_inserted += stats.rows_inserted
                result.rows_duplicate += stats.rows_duplicate
                result.payloads += stats.payloads
                result.payloads_duplicate += stats.payloads_duplicate
                result.quarantined += stats.payloads_quarantined
                result.bytes_stored += stats.bytes_stored
                if chunk_last_ts is not None:
                    last_ts = max(last_ts or chunk_last_ts, chunk_last_ts)
                self.progress.chunk_done(
                    entry.dataset,
                    index,
                    len(entry.chunks),
                    chunk.label,
                    stats,
                    self.client.rate_limit.remaining,
                )
        except _ChunkFailed as failure:
            self.ingest.rollback()
            result.error = str(failure)
            result.status = STATUS_PARTIAL if result.chunks_fetched else STATUS_FAILED
        except (RateLimitError, APIUnavailableError, CredentialsExpiredError) as exc:
            self.ingest.rollback()
            result.error = str(exc)
            result.status = STATUS_PARTIAL if result.chunks_fetched else STATUS_FAILED
            stopped_by = str(exc)
        except (Interrupted, KeyboardInterrupt):
            # Anything half-fetched is dropped; everything already committed stays.
            self.ingest.rollback()
            result.status = STATUS_INTERRUPTED
            result.notes.append(
                f"interrupted during chunk {index} of {len(entry.chunks)} ({self._label(entry, index)}); "
                "re-run with --resume to continue from here"
            )
            self._finish(result, completed_days, last_ts, started)
            report.add(result)
            self.progress.dataset_finished(result)
            raise Interrupted()
        self._finish(result, completed_days, last_ts, started)
        if result.status == STATUS_OK and result.chunks_fetched < len(entry.chunks) and not result.notes:
            result.status = STATUS_PARTIAL
        report.add(result)
        self.progress.dataset_finished(result)
        if stopped_by:
            raise StopRun(f"{entry.dataset}: {stopped_by}")

    def _finish(
        self, result: DatasetReport, completed_days: list[date], last_ts: datetime | None, started: datetime
    ) -> None:
        if completed_days:
            watermark = self.watermarks.advance(
                result.dataset, self.source, last_day=max(completed_days), last_ts=last_ts
            )
            result.watermark = watermark.last_day.isoformat() if watermark.last_day else None
        else:
            current = self.watermarks.get(result.dataset, self.source)
            result.watermark = current.last_day.isoformat() if current and current.last_day else None
        result.duration_ms = max(0, int((self.clock() - started).total_seconds() * 1000))

    def _fetch_chunk(
        self, dataset, chunk: ChunkPlan, result: DatasetReport
    ) -> tuple[IngestStats, datetime | None]:
        """Fetch every request in a chunk, store it, and commit. All or nothing."""
        stats = IngestStats()
        payloads: list[Payload] = []
        for request in chunk.requests:
            path = request.path()
            try:
                response = self.client.get(path)
            except HTTPError as exc:
                if exc.status in EMPTY_STATUSES:
                    result.info.append(f"{path}: no data (HTTP {exc.status})")
                    continue
                raise _ChunkFailed(f"{path}: {exc}")
            parsed = _parse_json(response)
            if parsed is None:
                stats += self.ingest.payload(dataset.name, path, response.text, rows=None, commit=False)
                result.notes.append(f"{path}: payload was not JSON (quarantined)")
                continue
            stats += self.ingest.payload(dataset.name, path, response.text, rows=[], commit=False)
            payloads.append(Payload(day=request.day, body=parsed))
        last_ts: datetime | None = None
        if payloads:
            rows = self._normalize(dataset, payloads, chunk)
            stats += self.ingest.rows(dataset.name, rows, source_id=chunk.label)
            stamps = [ts for ts, _ in rows]
            last_ts = max(stamps) if stamps else None
        self.ingest.commit()
        return stats, last_ts

    def _normalize(
        self, dataset, payloads: Sequence[Payload], chunk: ChunkPlan
    ) -> list[tuple[datetime, dict[str, float]]]:
        try:
            rows = dataset.normalize(list(payloads), self.timezone)
        except Exception as exc:  # noqa: BLE001 - a normalizer bug must not lose the raw payload
            raise _ChunkFailed(f"{dataset.name}: could not normalise the response ({type(exc).__name__}: {exc})")
        floor = datetime(chunk.start.year, chunk.start.month, chunk.start.day, tzinfo=UTC)
        ceiling = datetime(chunk.stop.year, chunk.stop.month, chunk.stop.day, tzinfo=UTC) + timedelta(days=1)
        return [(ts, values) for ts, values in rows if floor <= ts < ceiling]

    def _label(self, entry: DatasetPlan, index: int) -> str:
        if 1 <= index <= len(entry.chunks):
            return entry.chunks[index - 1].label
        return "?"

    @staticmethod
    def _overall(report: ImportReport) -> str:
        statuses = [d.status for d in report.datasets if d.status != STATUS_SKIPPED]
        if not statuses:
            return STATUS_SKIPPED
        if all(s == STATUS_FAILED for s in statuses):
            return STATUS_FAILED
        if any(s in (STATUS_FAILED, STATUS_PARTIAL) for s in statuses):
            return STATUS_PARTIAL
        return STATUS_OK


class _ChunkFailed(Exception):
    """A chunk could not be fetched; its dataset stops, the rest of the run continues."""


def _parse_json(response) -> dict[str, Any] | None:
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001 - callers quarantine whatever will not parse
        return None
    return payload if isinstance(payload, dict) else None


__all__ = [
    "ChunkPlan",
    "DatasetPlan",
    "ImportPlan",
    "ImportPlanner",
    "Importer",
    "Interrupted",
    "NullProgress",
    "Progress",
    "StopRun",
    "day_window",
]
