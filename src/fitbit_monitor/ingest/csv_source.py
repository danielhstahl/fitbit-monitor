"""``fm import --source csv``: ingest a Fitbit "Export Data" / Google Takeout archive.

This is the fallback that makes the epic usable when the API is not: the ZIP you
download from the Fitbit web profile (or Google Takeout) holds years of history, and
re-importing it is one command. It goes through the same
:class:`~fitbit_monitor.ingest.pipeline.Ingest` as an API pull, so the dedup, quarantine
and watermark rules are identical and importing the same archive twice changes nothing.

**Every file type states its units and its timezone here**, because the files themselves
do not:

=========================================== ========================== ==========================
file                                        unit / meaning             timezone
=========================================== ========================== ==========================
``heart/HeartRate_<date>.csv``              ``Min`` = beats per minute local (account) time
``hrv/HRV_<date>.csv``                      ``Hrv Value`` = RMSSD, ms local (account) date
``sleep/Sleep_<date>.csv``                  minutes local (account) date
``activities/Daily_Summary.csv``            steps, kcal, minutes, bpm local (account) date
=========================================== ========================== ==========================

"local" is ``[import] timezone`` (``UTC`` by default) - the same setting the API
normalizers use, so an API import and a CSV import of the same week agree.

A file with no parser is **skipped with a reason**, not silently dropped: so are a row
with an unparseable date or a value that is not a number. Both the per-file row counts
and the skip reasons end up in the run report.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Iterator, Sequence

from ..errors import UsageError
from ..timeutil import parse_date, parse_timestamp, utcnow
from .pipeline import Ingest
from .report import (
    STATUS_FAILED,
    STATUS_OK,
    STATUS_PARTIAL,
    DatasetReport,
    ImportReport,
    new_run_id,
)

Row = tuple[datetime, dict[str, float]]

#: Files older than this are ignored (nothing has been on Fitbit longer).
DAY_ZERO = date(1900, 1, 1)


@dataclass
class ParsedFile:
    """One archive member and what came out of it."""

    name: str
    kind: str | None = None
    rows: list[Row] = field(default_factory=list)
    skipped: str | None = None
    bad_rows: int = 0
    rows_out_of_range: int = 0
    text: str = ""

    @property
    def usable(self) -> bool:
        return self.kind is not None and not self.skipped

    def summary(self) -> str:
        if self.skipped:
            return f"{self.name}: skipped - {self.skipped}"
        detail = f"{len(self.rows)} row(s)"
        if self.bad_rows:
            detail += f", {self.bad_rows} unparseable"
        if self.rows_out_of_range:
            detail += f", {self.rows_out_of_range} outside --since/--until"
        return f"{self.name}: {detail}"


@dataclass(frozen=True)
class Parser:
    """How to recognise one archive member and read it."""

    kind: str
    pattern: re.Pattern[str]
    read: Callable[[str, str, str], tuple[list[Row], int]]
    #: What the file's numbers mean - shown by ``--json`` and asserted by the tests.
    units: str = ""
    timezone_note: str = "local (account) time"


def _matches(parser: Parser, name: str) -> bool:
    return bool(parser.pattern.search(name))


def _cell(row: Sequence[str], index: int) -> str:
    return row[index].strip() if index < len(row) else ""


def _number(text: Any) -> float | None:
    try:
        value = float(str(text).strip().replace(",", ""))
    except (TypeError, ValueError):
        return None
    return value


def _stamp(text: str, tz_name: str, *, fmt: str | None = None) -> datetime | None:
    try:
        return parse_timestamp(text, tz_name=tz_name, field="csv timestamp", fmt=fmt)
    except UsageError:
        return None


def _split_datetime(row: Sequence[str], index: int) -> str:
    """Join a datetime that Fitbit's CSV splits across two columns."""
    first, second = _cell(row, index), _cell(row, index + 1)
    if re.match(r"^\d{4}[-/]\d{2}[-/]\d{2}$", first) and re.match(r"^\d{1,2}:\d{2}", second):
        return f"{first} {second}"
    return first


# --- heart rate ------------------------------------------------------------
HR_PATTERN = re.compile(r"(?:^|/)(?:heart|activities)/HeartRate[^/]*\.csv$", re.IGNORECASE)
HR_COLUMNS = ("min", "bpm", "heartrate", "heart rate")


def parse_heart(text: str, tz_name: str, name: str) -> tuple[list[Row], int]:
    """``Min,DateTime`` intraday heart rate: one row per minute, values in bpm.

    The ``DateTime`` value contains a comma, so a "two column" file really arrives as
    three fields; both spellings are accepted.
    """
    rows: list[Row] = []
    bad = 0
    records = list(csv.reader(io.StringIO(text)))
    if not records:
        return [], 0
    header = [c.strip().lower() for c in records[0]]
    time_index = next((i for i, c in enumerate(header) if "datetime" in c or c == "time"), 1)
    value_index = next((i for i, c in enumerate(header) if c in HR_COLUMNS), 0)
    for record in records[1:]:
        if not record or not any(c.strip() for c in record):
            continue
        stamp = _split_datetime(record, time_index)
        value = _number(_cell(record, value_index))
        ts = _stamp(stamp, tz_name)
        if ts is None or value is None or not (20.0 <= value <= 260.0):
            bad += 1
            continue
        rows.append((ts, {"hr": value}))
    return rows, bad


# --- HRV -------------------------------------------------------------------
HRV_PATTERN = re.compile(r"(?:^|/)hrv/HRV[^/]*\.csv$", re.IGNORECASE)
HRV_VALUE_COLUMNS = ("hrv value", "hrv_value", "hrv", "rmssd")
HRV_TIME_COLUMNS = ("date time", "datetime", "date", "sleep start time")


def parse_hrv(text: str, tz_name: str, name: str) -> tuple[list[Row], int]:
    """One daily RMSSD figure per row, in milliseconds, on a local calendar date."""
    rows: list[Row] = []
    bad = 0
    records = list(csv.reader(io.StringIO(text)))
    if not records:
        return [], 0
    header = [c.strip().lower() for c in records[0]]
    time_index = next((i for i, c in enumerate(header) if c in HRV_TIME_COLUMNS), 0)
    value_index = next((i for i, c in enumerate(header) if c in HRV_VALUE_COLUMNS), 1)
    for record in records[1:]:
        if not record or not any(c.strip() for c in record):
            continue
        stamp = _split_datetime(record, time_index) or name
        value = _number(_cell(record, value_index))
        ts = _stamp(stamp, tz_name)
        if ts is None:
            fallback = _date_from_name(name)
            ts = _stamp(fallback, tz_name) if fallback else None
        if ts is None or value is None or value <= 0:
            bad += 1
            continue
        rows.append((ts, {"rmssd": value}))
    return rows, bad


# --- sleep -----------------------------------------------------------------
SLEEP_PATTERN = re.compile(r"(?:^|/)sleep/Sleep[^/]*\.csv$", re.IGNORECASE)
SLEEP_COLUMNS: dict[str, tuple[str, ...]] = {
    "sleep_minutes": ("time asleep", "minutes asleep", "minutes_asleep", "asleep", "time asleep"),
    "awake_minutes": ("awake time", "minutes awake", "minutes_awake", "awake"),
    "deep_minutes": ("deep sleep", "minutes deep", "deep minutes", "deep"),
    "light_minutes": ("light sleep", "minutes light"),
    "rem_minutes": ("rem sleep", "minutes rem", "rem"),
    "time_in_bed_minutes": ("time in bed", "minutes in bed"),
    "efficiency": ("efficiency", "sleep efficiency"),
}
SLEEP_DATE_COLUMNS = ("date of sleep", "date started", "date", "sleep date")
SLEEP_START_COLUMNS = ("fall asleep", "time asleep", "start time", "started")
SLEEP_FIELDS: dict[str, str] = {
    "date start": "date",
    "date end": "end",
    "time in bed": "time_in_bed_minutes",
    "time asleep": "sleep_minutes",
    "awake time": "awake_minutes",
    "deep sleep": "deep_minutes",
    "light sleep": "light_minutes",
    "rem sleep": "rem_minutes",
    "efficiency": "efficiency",
}


def _date_from_name(name: str) -> str | None:
    found = re.search(r"(\d{4})[-_](\d{2})[-_](\d{2})", name)
    if not found:
        return None
    return f"{found.group(1)}-{found.group(2)}-{found.group(3)}"


def parse_sleep(text: str, tz_name: str, name: str) -> tuple[list[Row], int]:
    """A night (or nap) of sleep, in minutes, dated in the account timezone.

    Fitbit publishes sleep two ways: a horizontal CSV with a column per figure, and -
    in newer exports and Takeout - a vertical ``Field,Value`` file per night. Both are
    read here because both appear in real archives.
    """
    records = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
    if not records:
        return [], 0
    header = [c.strip().lower() for c in records[0]]
    if len(records[0]) == 2 and header[0] in ("field", "key", "name"):
        return _parse_sleep_vertical(records[1:], tz_name, name)
    return _parse_sleep_horizontal(records, header, tz_name, name)


def _parse_sleep_vertical(records: Sequence[Sequence[str]], tz_name: str, name: str) -> tuple[list[Row], int]:
    fields = { _cell(r, 0).lower(): _cell(r, 1) for r in records }
    values: dict[str, float] = {}
    for key, target in SLEEP_FIELDS.items():
        raw = fields.get(key)
        if not raw:
            continue
        number = _number(raw)
        if number is not None:
            values[target] = number
    stamp = fields.get("date start") or fields.get("date") or _date_from_name(name)
    ts = _stamp(stamp, tz_name) if stamp else None
    if ts is None or not values:
        return [], 1
    return [(ts, values)], 0


def _parse_sleep_horizontal(
    records: Sequence[Sequence[str]], header: Sequence[str], tz_name: str, name: str
) -> tuple[list[Row], int]:
    rows: list[Row] = []
    bad = 0
    for record in records[1:]:
        cells = {header[i]: _cell(record, i) for i in range(len(header)) if i < len(header)}
        stamp = next((cells[k] for k in SLEEP_START_COLUMNS if cells.get(k)), "")
        if not stamp:
            day = next((cells[k] for k in SLEEP_DATE_COLUMNS if cells.get(k)), "") or _date_from_name(name) or ""
            stamp = day or ""
        values: dict[str, float] = {}
        for target, aliases in SLEEP_COLUMNS.items():
            for alias in aliases:
                if cells.get(alias):
                    number = _number(cells[alias])
                    if number is not None:
                        values[target] = number
                        break
        ts = _stamp(stamp, tz_name)
        if ts is None or not values:
            bad += 1
            continue
        rows.append((ts, values))
    return rows, bad


# --- daily summary ---------------------------------------------------------
SUMMARY_PATTERN = re.compile(r"(?:^|/)activities/(Daily_Summary|daily_summary)[^/]*\.csv$", re.IGNORECASE)
SUMMARY_COLUMNS: dict[str, tuple[str, ...]] = {
    "steps": ("steps", "step count", "total steps"),
    "calories": ("calories", "calories burned", "calories bmr"),
    "active_minutes": ("lightly active", "lightly active minutes", "active minutes"),
    "very_active_minutes": ("very active", "very active minutes"),
    "fairly_active_minutes": ("fairly active", "fairly active minutes"),
    "distance_km": ("distance", "distance km"),
    "resting_hr": ("resting heart rate", "resting heart rate (bpm)", "resting_hr"),
    "floors": ("floors", "floors climbed"),
}
SUMMARY_DATE_COLUMNS = ("date", "activity date", "day")


def parse_daily_summary(text: str, tz_name: str, name: str) -> tuple[list[Row], int]:
    """One row per calendar day: steps, kcal, minutes and resting bpm.

    Takeout spells the columns ``Date, Steps, Distance, Very Active, Calories,
    Resting Heart Rate``; the Fitbit web export is close enough that the alias table
    covers both.
    """
    records = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
    if not records:
        return [], 0
    header = [c.strip().lower() for c in records[0]]
    date_index = next((i for i, c in enumerate(header) if c in SUMMARY_DATE_COLUMNS), None)
    if date_index is None:
        return [], 0
    rows: list[Row] = []
    bad = 0
    for record in records[1:]:
        day = _cell(record, date_index)
        values: dict[str, float] = {}
        for target, aliases in SUMMARY_COLUMNS.items():
            for alias in aliases:
                if alias in header:
                    number = _number(_cell(record, header.index(alias)))
                    if number is not None:
                        values[target] = number
                        break
        ts = _stamp(day, tz_name)
        if ts is None:
            try:
                as_date = parse_date(day)
            except UsageError:
                bad += 1
                continue
            ts = _stamp(as_date.isoformat(), tz_name)
            if ts is None:
                bad += 1
                continue
        if not values:
            bad += 1
            continue
        rows.append((ts, values))
    return rows, bad


#: Every format this source knows, in match order. The reason a file is skipped is
#: reported verbatim, so an archive the tool cannot read is visible in the report.
PARSERS: tuple[Parser, ...] = (
    Parser(
        kind="heart_rate",
        pattern=HR_PATTERN,
        read=parse_heart,
        units="Min = beats per minute, one row per minute",
        timezone_note="local (account) time",
    ),
    Parser(
        kind="hrv",
        pattern=HRV_PATTERN,
        read=parse_hrv,
        units="Hrv Value = RMSSD in milliseconds, daily",
        timezone_note="local (account) date",
    ),
    Parser(
        kind="sleep",
        pattern=SLEEP_PATTERN,
        read=parse_sleep,
        units="minutes (asleep / awake / deep / light / REM)",
        timezone_note="local (account) date",
    ),
    Parser(
        kind="daily_summary",
        pattern=SUMMARY_PATTERN,
        read=parse_daily_summary,
        units="steps, kcal, minutes, bpm",
        timezone_note="local (account) date",
    ),
)

#: Files that look plausible but are deliberately not imported, with the reason said out loud.
DECLINED: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"activities[_/][^/]*\.csv$", re.IGNORECASE), "intraday activity log: steps come from Daily_Summary.csv"),
    (re.compile(r"(?:^|/)body/", re.IGNORECASE), "body weight is not a dataset fm imports"),
    (re.compile(r"(?:^|/)(bp|blood[-_]?pressure)", re.IGNORECASE), "blood pressure is not a dataset fm imports"),
)


#: Column signatures, used only when the file name does not identify the dataset.
#: People unpack archives, rename things, and Takeout drops files flat into one folder;
#: the header says what a file *is* far more reliably than its name.
COLUMN_SIGNATURES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("hrv", ("hrvvalue", "hrv value", "hrv_value", "rmssd")),
    ("sleep", ("minutesasleep", "minutes asleep", "time asleep")),
    ("daily_summary", ("steps", "step count", "calories burned")),
    ("heart_rate", ("heartrate", "heart rate", "bpm", ",datetime")),
)


def sniff_columns(text: str) -> str | None:
    """The dataset a file's header line implies, or None."""
    header = text.split("\n", 1)[0].lower() if text else ""
    if not header.strip():
        return None
    for kind, needles in COLUMN_SIGNATURES:
        if any(needle in header for needle in needles):
            return kind
    return None


def find_parser(name: str, text: str | None = None) -> Parser | None:
    """The parser for a member: by path first, then by header if the name says nothing.

    A file explicitly declined by its path stays declined even when its header happens
    to look like a dataset - ``activities/*.csv`` has a Steps column, and importing it
    as a daily summary would double-count the day.
    """
    for parser in PARSERS:
        if _matches(parser, name):
            return parser
    if text is not None and decline_reason(name) is None:
        kind = sniff_columns(text)
        for parser in PARSERS:
            if parser.kind == kind:
                return parser
    return None


def decline_reason(name: str) -> str | None:
    for pattern, reason in DECLINED:
        if pattern.search(name):
            return reason
    return None


def iter_archive(path: Path) -> Iterator[tuple[str, str]]:
    """Yield ``(name, text)`` for every CSV in a ZIP or a directory tree."""
    if path.is_dir():
        for member in sorted(path.rglob("*.csv")):
            yield member.relative_to(path).as_posix(), member.read_text(encoding="utf-8-sig", errors="replace")
        return
    if path.suffix.lower() in (".zip", ".takeout") or zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if info.is_dir() or not info.filename.lower().endswith(".csv"):
                    continue
                with archive.open(info) as handle:
                    yield info.filename, handle.read().decode("utf-8-sig", errors="replace")
        return
    raise UsageError(f"{path} is not a ZIP archive or a directory", hint="point --path at the export.zip or its folder")


def parse_member(
    name: str, text: str, *, timezone: str = "UTC", window: tuple[date, date] | None = None
) -> ParsedFile:
    """Read one archive member, or say why it cannot be read."""
    parsed = ParsedFile(name=name, text=text)
    parser = find_parser(name, text)
    if parser is None:
        parsed.skipped = decline_reason(name) or "no parser for this file (not a dataset fm imports)"
        return parsed
    parsed.kind = parser.kind
    try:
        rows, bad = parser.read(text, timezone, name)
    except Exception as exc:  # noqa: BLE001 - one unreadable file must not stop the archive
        parsed.skipped = f"unreadable ({type(exc).__name__}: {exc})"
        return parsed
    parsed.bad_rows = bad
    if window:
        floor, ceiling = window
        kept = [row for row in rows if floor <= row[0].date() <= ceiling]
        parsed.rows_out_of_range = len(rows) - len(kept)
        rows = kept
    parsed.rows = rows
    if not rows and not bad:
        parsed.skipped = "no data rows"
    return parsed


def parse_export(
    path: Path | str,
    *,
    timezone: str = "UTC",
    window: tuple[date, date] | None = None,
    datasets: Sequence[str] | None = None,
    include: Sequence[str] = (),
    only: Sequence[str] = (),
    exclude: Sequence[str] = (),
) -> list[ParsedFile]:
    """Every archive member, parsed or explained.

    ``datasets`` narrows by dataset; ``include``/``exclude`` are glob patterns over the
    member name and ``only`` names members exactly - the archive is usually a few
    hundred files and you often want three of them.
    """
    wanted = set(datasets) if datasets else None
    out: list[ParsedFile] = []
    for name, text in iter_archive(Path(path)):
        basename = PurePosixPath(name).name
        if only and not _matches_any(basename, only) and not _matches_any(name, only):
            continue
        if exclude and (_matches_any(basename, exclude) or _matches_any(name, exclude)):
            continue
        parsed = parse_member(name, text, timezone=timezone, window=window)
        if parsed.kind and wanted and parsed.kind not in wanted:
            parsed.skipped = f"not selected (--dataset: {', '.join(sorted(wanted))})"
        elif not parsed.kind and include and not (_matches_any(basename, include) or _matches_any(name, include)):
            # An unrecognised file the user did not ask for by name is noise, not a warning.
            parsed.skipped = parsed.skipped or "unrecognised file (not named by --file/--glob)"
        elif include and parsed.kind and not (_matches_any(basename, include) or _matches_any(name, include)):
            parsed.skipped = parsed.skipped or "not named by --glob"
        out.append(parsed)
    return out


def _matches_any(name: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch(name, pattern) or name == pattern for pattern in patterns)


class CsvImport:
    """The ``--source csv`` half of ``fm import``: same ingest path, same report."""

    source = "csv"

    def __init__(
        self,
        *,
        ingest: Ingest,
        watermarks=None,
        runlog=None,
        timezone: str = "UTC",
        clock: Callable[[], datetime] = utcnow,
        progress=None,
    ) -> None:
        self.ingest = ingest
        self.watermarks = watermarks
        self.runlog = runlog
        self.timezone = timezone
        self.clock = clock
        self.progress = progress

    def run(
        self,
        path: Path | str,
        *,
        datasets: Sequence[str] | None = None,
        since: date | None = None,
        until: date | None = None,
        include: Sequence[str] = (),
        only: Sequence[str] = (),
        exclude: Sequence[str] = (),
        allow_unknown_files: bool = False,
        resume: bool = False,
    ) -> ImportReport:
        archive = Path(path)
        if not archive.exists():
            raise UsageError(f"no such export: {archive}", hint="point --path at the export ZIP or its directory")
        window = (since or DAY_ZERO, until or self.clock().date())
        files = parse_export(
            archive,
            timezone=self.timezone,
            window=window,
            datasets=datasets,
            include=include,
            only=only,
            exclude=exclude,
        )
        report = ImportReport(
            run_id=new_run_id("import-csv"),
            source=self.source,
            since=window[0].isoformat(),
            until=window[1].isoformat(),
            started_at=self.clock(),
            notes=[f"archive: {archive}"],
        )
        if resume:
            # An export is a backfill: a previous partial run is continued by dropping
            # the days already stored, which the dedupe would do anyway - so the only
            # thing to do here is say what happened and keep the window whole.
            report.notes.append("--resume: re-reading the archive; stored rows dedupe, so only new ones land")
        by_kind: dict[str, list[ParsedFile]] = {}
        for parsed in files:
            if parsed.usable and parsed.rows:
                by_kind.setdefault(parsed.kind or "?", []).append(parsed)
            elif parsed.skipped:
                report.notes.append(f"{parsed.name}: {parsed.skipped}")
        for kind, members in sorted(by_kind.items()):
            report.add(self._ingest_dataset(kind, members, window))
        skipped = [parsed for parsed in files if parsed.skipped]
        if not by_kind:
            report.status = STATUS_FAILED
            report.datasets.append(
                DatasetReport(
                    dataset="csv",
                    source=self.source,
                    status=STATUS_FAILED,
                    error=f"no importable files in {archive} ({len(files)} file(s) seen)",
                    info=[parsed.skipped or "" for parsed in skipped][:20],
                )
            )
        else:
            report.status = self._overall(report, files)
        report.finished_at = self.clock()
        if self.runlog is not None:
            self.runlog.record_report(
                report,
                started_at=report.started_at.isoformat() if report.started_at else "",
                finished_at=report.finished_at.isoformat() if report.finished_at else "",
            )
        return report

    # --- one dataset --------------------------------------------------------
    def _ingest_dataset(self, kind: str, members: list[ParsedFile], window: tuple[date, date]) -> DatasetReport:
        from .pipeline import IngestStats

        started = self.clock()
        result = DatasetReport(
            dataset=kind,
            source=self.source,
            since=window[0].isoformat(),
            until=window[1].isoformat(),
            chunks_planned=len(members),
        )
        stats = IngestStats()
        for parsed in members:
            chunk_stats = self.ingest.payload(kind, parsed.name, parsed.text, rows=parsed.rows, commit=False)
            self.ingest.commit()
            stats = stats + chunk_stats
            result.files.append(f"{parsed.name}: {len(parsed.rows)} row(s)")
            if parsed.bad_rows:
                result.notes.append(f"{parsed.name}: {parsed.bad_rows} unparseable row(s)")
            if parsed.rows_out_of_range:
                result.info.append(f"{parsed.name}: {parsed.rows_out_of_range} row(s) outside --since/--until")
        result.chunks_fetched = len(members)
        result.requests = 0
        result.payloads = stats.payloads
        result.payloads_duplicate = stats.payloads_duplicate
        result.rows_inserted = stats.rows_inserted
        result.rows_duplicate = stats.rows_duplicate
        result.quarantined = stats.payloads_quarantined + sum(p.bad_rows for p in members)
        result.bytes_stored = stats.bytes_stored
        result.duration_ms = max(0, int((self.clock() - started).total_seconds() * 1000))
        # Bad rows are a warning (the data that could be read landed); a dataset is only
        # *partial* when some of its files could not be read at all.
        unreadable = [parsed.name for parsed in members if parsed.skipped]
        if not members:
            result.status = STATUS_FAILED
            result.error = f"no readable files for {kind}"
        elif unreadable and not (result.rows_inserted or result.rows_duplicate):
            result.status = STATUS_FAILED
            result.error = "no file could be read: " + "; ".join(unreadable[:3])
        elif unreadable:
            result.status = STATUS_PARTIAL
            result.notes.append(f"not read: {', '.join(unreadable[:5])}")
        else:
            result.status = STATUS_OK
        stamps = [row[0] for member in members for row in member.rows]
        if self.watermarks is not None and stamps:
            watermark = self.watermarks.advance(
                kind, self.source, last_day=max(ts.date() for ts in stamps), last_ts=max(stamps)
            )
            result.watermark = watermark.last_day.isoformat() if watermark.last_day else None
        return result

    @staticmethod
    def _overall(report: ImportReport, files: Sequence[ParsedFile]) -> str:
        statuses = [d.status for d in report.datasets]
        if statuses and all(s == STATUS_FAILED for s in statuses):
            return STATUS_FAILED
        if any(s in (STATUS_FAILED, STATUS_PARTIAL) for s in statuses):
            return STATUS_PARTIAL
        return STATUS_OK


def summarise_files(files: Iterable[ParsedFile]) -> list[dict[str, Any]]:
    """Per-file view for ``--json``: what was read, what was skipped and why."""
    return [
        {
            "file": f.name,
            "dataset": f.kind,
            "rows": len(f.rows),
            "bad_rows": f.bad_rows,
            "rows_out_of_range": f.rows_out_of_range,
            "skipped": f.skipped,
        }
        for f in files
    ]


__all__ = [
    "PARSERS",
    "CsvImport",
    "ParsedFile",
    "Parser",
    "find_parser",
    "sniff_columns",
    "iter_archive",
    "parse_daily_summary",
    "parse_export",
    "parse_hrv",
    "parse_member",
    "parse_sleep",
    "parse_heart",
    "summarise_files",
]
