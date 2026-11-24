"""``fm import`` - the ad-hoc import, and the commands that report on it.

The headline request in one line:

    fm import --since 2026-05-01 --until 2026-05-14

That pulls a chosen window of every dataset the account allows, through the API client,
into the local store - idempotently, so running it again tomorrow costs requests and
not duplicates. ``--dry-run`` shows the plan and the approximate request cost before
anything is spent; ``--dataset`` narrows the pull; ``--full-history`` starts from the
account's signup date instead of the watermark; Ctrl-C is safe (chunks already fetched
are stored, the watermark resumes from there).

``--source csv --path export.zip`` is the fallback for when the API is not an option -
and the quickest way to backfill years of history: the same ingest path, reading a
Fitbit "Export Data" / Google Takeout archive.

``fm import history``, ``fm import report`` and ``fm import watermarks`` answer "what
did that run do?" afterwards, from the ``dataset_run`` table and the JSON report.

Exit codes for every ``fm import`` run:

===== ==========================================================
0     everything asked for landed
1     nothing landed - total failure
3     some datasets landed, some did not
5     it landed, with warnings (quarantined payloads, bad rows)
6     the configured API source is retired and was not dialled - see
      ``fm api source``; ``--source csv`` still works
130   interrupted; completed chunks are stored
===== ==========================================================
"""

from __future__ import annotations

import argparse
import sys
from contextlib import ExitStack
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any, Sequence

from ..api.catalog import CATALOG
from ..api.errors import NotAuthenticatedError
from ..errors import StoreUnavailableError, UsageError
from ..ingest.csv_source import CsvImport, parse_export, summarise_files
from ..ingest.importer import ImportPlanner, Importer, NullProgress, day_window
from ..ingest.pipeline import Ingest
from ..ingest.report import EXIT_INTERRUPTED, EXIT_PARTIAL_FAILURE, EXIT_TOTAL_FAILURE, ImportReport
from ..ingest.runlog import RunLog
from ..store.watermarks import Watermarks
from ..store.writer import SeriesStore, write_lock
from .common import EXIT_NO_STORE, EXIT_OK, Session, print_json, render_table


def register(sub: "argparse._SubParsersAction") -> None:
    imp = sub.add_parser(
        "import",
        help="pull a chosen date range on demand (API, or a CSV export)",
        description=(
            "Run an ad-hoc import. Plain dates are inclusive of both ends; --since "
            "defaults to each dataset's watermark, so a nightly run picks up exactly "
            "what is new."
        ),
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    isub = imp.add_subparsers(dest="import_command")

    imp.add_argument("--since", default=None, help="first day to fetch (default: the dataset's watermark)")
    imp.add_argument("--until", default=None, help="last day to fetch (default: today)")
    imp.add_argument(
        "--dataset",
        action="append",
        metavar="NAME",
        help="only this dataset (repeatable, or comma-separated); default: everything the account allows",
    )
    imp.add_argument("--dry-run", action="store_true", help="print the plan and the request cost, fetch nothing")
    imp.add_argument("--full-history", action="store_true", help="start at the account's signup date, not the watermark")
    imp.add_argument(
        "--source",
        choices=("api", "health", "csv", "archive"),
        default=None,
        help="where to import from (default: api; health and archive are the same two)",
    )
    imp.add_argument("--path", default=None, help="export ZIP or directory to ingest (implies --source csv)")
    imp.add_argument("--glob", action="append", metavar="PATTERN", help="only CSV files matching this pattern (repeatable)")
    imp.add_argument("--file", action="append", metavar="NAME", help="only this file from the export (repeatable)")
    imp.add_argument("--exclude", action="append", metavar="PATTERN", help="skip CSV files matching this pattern (repeatable)")
    imp.add_argument(
        "--allow-unknown-files",
        action="store_true",
        help="note unrecognised CSV files instead of warning about them",
    )
    imp.add_argument("--chunk-days", type=int, default=None, help="days per chunk (smaller = finer resume, more requests)")
    imp.add_argument("--max-requests", type=int, default=None, help="stop after this many API requests and resume later")
    imp.add_argument(
        "--resume",
        action="store_true",
        help="pick up where the last run stopped (interrupted and failed datasets, from their watermarks)",
    )
    imp.add_argument("--retry", action="store_true", help="re-import only the datasets that failed in the last run")
    imp.add_argument("--yes", action="store_true", help="do not ask before a large pull")
    imp.add_argument("--json", action="store_true", help="print the JSON report instead of the table")
    imp.add_argument(
        "--fixtures",
        default=None,
        metavar="DIR",
        help="replay recorded responses from DIR instead of calling the API (offline rehearsal)",
    )
    imp.set_defaults(func=cmd_import)

    history = isub.add_parser("history", help="list recent import runs from the store")
    history.add_argument("--limit", type=int, default=10)
    history.add_argument("--dataset", default=None, help="only this dataset's runs")
    history.add_argument("--json", action="store_true")
    history.set_defaults(func=cmd_import_history)

    report = isub.add_parser("report", help="print the JSON report of a run")
    report.add_argument("--run-id", default=None, help="run id (default: the most recent report)")
    report.add_argument("--json", action="store_true", help="emit the report verbatim")
    report.set_defaults(func=cmd_import_report)

    watermarks = isub.add_parser("watermarks", help="where each dataset has been fetched to")
    watermarks.add_argument(
        "--source", default=None, choices=("api", "health", "csv", "archive"), help="only this source"
    )
    watermarks.add_argument("--json", action="store_true")
    watermarks.set_defaults(func=cmd_import_watermarks)


_EPILOG = """\
examples:
  fm import --dry-run                                    what would this cost, what would it fetch
  fm import --since 2026-05-01 --until 2026-05-14       pull two weeks
  fm import --dataset heart_rate --since 7d             one dataset, last week
  fm import --full-history                              everything since the account was created
  fm import --source csv --path ~/Downloads/export.zip  backfill from the web export
  fm import --resume                                    finish what the last run could not
  fm import history                                     what did the last runs land
  fm import watermarks                                  where does each dataset stop

Running the same command twice is safe: payloads dedupe by content hash, rows upsert,
and a watermark only moves forward.

exit codes:
  0     everything asked for landed
  1     nothing landed - total failure
  3     some datasets landed, some did not
  5     it landed, with warnings (quarantined payloads, bad rows)
  130   interrupted; completed chunks are stored
"""


#: ADR-0002 §9 names the two sources ``health`` and ``archive``; the store, the reports
#: and the first release call them ``api`` and ``csv``. Both names are accepted at the
#: flag, one name is stored: "api" stopped being a specific answer when there were two
#: APIs, but rewriting every watermark row to say "health" would buy nothing.
SOURCE_ALIASES = {"health": "api", "archive": "csv"}


def _source(name: str | None) -> str | None:
    """The stored name for a ``--source`` the user typed."""
    return None if name is None else SOURCE_ALIASES.get(name, name)


class EarlyExit(Exception):
    """A command that finished early with a known code (``--dry-run``, a refusal)."""

    def __init__(self, code: int) -> None:
        super().__init__(f"exit {code}")
        self.code = code


def cmd_import(args: argparse.Namespace, session: Session) -> int:
    source = _source(args.source) or ("csv" if args.path else "api")
    if source == "csv" and not args.path:
        raise UsageError("--source csv needs --path <export.zip|dir>")
    datasets = _dataset_list(args.dataset)
    client = None
    if source == "api":
        # Checked before the store is opened: a run that cannot authenticate should
        # not leave an empty database behind.
        client = session.api_client(fixtures=args.fixtures)
        # Before the token check, deliberately: the retired Fitbit host answers 401, and
        # reporting that as "log in again" is a loop with no exit. The archive source is
        # unaffected - `--source csv` needs no API and no credentials.
        client.guard_source()
        if not client.authenticated:
            raise NotAuthenticatedError(f"not authenticated: no credentials at {session.credentials().path}")
    try:
        with ExitStack() as stack:
            if not args.dry_run:
                # One import at a time per store: chunks, watermarks and the shared
                # rate-limit budget all assume a single writer.
                stack.enter_context(write_lock(session.store_path, what="an fm import"))
            store = _store_for(args, session)
            stack.callback(store.close)
            ingest = Ingest(store, source=source)
            watermarks = Watermarks(store)
            runlog = RunLog(store)
            if source == "csv":
                report = _run_csv(args, session, ingest, watermarks, runlog, datasets)
            else:
                report = _run_api(args, session, ingest, watermarks, runlog, datasets, client=client)
            report.write(session.paths.imports, keep=session.import_settings.report_keep)
    except EarlyExit as early:
        return early.code
    return _present(args, session, report)


# --- the two sources -------------------------------------------------------
def _store_for(args: argparse.Namespace, session: Session):
    """A dry run must work before the first import ever ran, so a missing store is not
    an error there: watermarks come from an empty in-memory store instead, which is
    exactly what "nothing fetched yet" means for the plan."""
    if not args.dry_run:
        return session.write_store()
    try:
        return session.read_store()
    except StoreUnavailableError:
        return SeriesStore(":memory:")


def _run_api(args, session, ingest, watermarks, runlog, datasets, *, client=None) -> ImportReport:
    client = client or session.api_client(fixtures=args.fixtures)
    if not client.authenticated:
        raise NotAuthenticatedError(f"not authenticated: no credentials at {session.credentials().path}")
    settings = session.import_settings
    if args.chunk_days:
        # A CLI override beats the config map for every dataset.
        settings = replace(settings, chunk_days={name: max(1, args.chunk_days) for name in CATALOG})
    planner = ImportPlanner(client, watermarks, settings)
    plan = planner.build(
        datasets=datasets, since=args.since, until=args.until, full_history=args.full_history, source="api"
    )
    if args.retry:
        retryable = set(runlog.datasets_needing_retry())
        if not retryable:
            print("nothing to retry: the most recent run did not fail")
            raise EarlyExit(EXIT_OK)
        plan.datasets = [entry for entry in plan.datasets if entry.dataset in retryable]
        plan.notes.append(f"--retry: re-importing {', '.join(sorted(retryable))}")
    if args.resume:
        names = Importer.resume_names(runlog, datasets)
        if not names:
            print("nothing to resume: no dataset is behind, and the last run did not fail")
            raise EarlyExit(EXIT_OK)
        # Resume means "finish the window the last run was asked for", from each
        # dataset's watermark; datasets the last run never reached are included.
        recorded = runlog.last_window()
        plan = planner.build(
            datasets=names,
            since=args.since,
            until=args.until or (recorded[1].isoformat() if recorded else None),
            source="api",
            resume_from=recorded[0] if recorded else None,
        )
        plan.notes.append(f"--resume: continuing {', '.join(names)} from their watermarks")
    if args.dry_run:
        _print_plan(args, plan)
        raise EarlyExit(EXIT_OK)
    _confirm(plan, args, session)
    importer = Importer(
        client=client,
        ingest=ingest,
        watermarks=watermarks,
        runlog=runlog,
        settings=settings,
        timezone=settings.timezone,
        progress=_progress(args),
    )
    try:
        return importer.run(plan, max_requests=args.max_requests)
    finally:
        client.flush()


def _run_csv(args, session, ingest, watermarks, runlog, datasets) -> ImportReport:
    path = Path(args.path).expanduser()
    if not path.exists():
        raise UsageError(f"no such export: {path}", hint="point --path at the export ZIP or the folder you unpacked")
    if args.dry_run:
        files = parse_export(path, timezone=session.import_settings.timezone, datasets=datasets)
        if args.json:
            print_json({"path": str(path), "files": summarise_files(files)})
        else:
            print(f"planned CSV import from {path}")
            for line in summarise_files(files):
                status = f"skipped - {line['skipped']}" if line["skipped"] else f"{line['rows']} row(s)"
                print(f"  {line['file']:<46} {line['dataset'] or '-':<14} {status}")
            print(f"  files: {len(files)}")
        raise EarlyExit(EXIT_OK)
    since, until = _csv_window(args)
    importer = CsvImport(
        ingest=ingest, watermarks=watermarks, runlog=runlog, timezone=session.import_settings.timezone
    )
    return importer.run(
        path,
        datasets=datasets,
        since=since,
        until=until,
        include=tuple(args.glob or ()),
        only=tuple(args.file or ()),
        exclude=tuple(args.exclude or ()),
        allow_unknown_files=args.allow_unknown_files,
        resume=args.resume,
    )


def _csv_window(args) -> tuple[date | None, date | None]:
    """A CSV archive is a backfill: with no dates asked for, everything is in range."""
    if not args.since and not args.until:
        return None, None
    start, stop = day_window(args.since, args.until, default_days=36500)
    return start, stop


def _dataset_list(values: Sequence[str] | None) -> tuple[str, ...] | None:
    if not values:
        return None
    out: list[str] = []
    for value in values:
        out.extend(part.strip() for part in value.split(",") if part.strip())
    return tuple(out) or None


def _confirm(plan, args: argparse.Namespace, session: Session) -> None:
    limit = session.import_settings.confirm_above_requests
    if args.yes or plan.requests < limit:
        return
    if not (hasattr(sys.stdin, "isatty") and sys.stdin.isatty()):
        plan.notes.append(
            f"~{plan.requests} requests exceeds [import] confirm_above_requests = {limit}; "
            "running unattended (use --max-requests to bound a cron pull)"
        )
        return
    answer = input(f"fetch ~{plan.requests} requests for {len(plan.active)} dataset(s)? [y/N] ").strip().lower()
    if answer not in ("y", "yes"):
        print("nothing fetched; re-run with --yes to skip the question")
        raise EarlyExit(EXIT_OK)


def _progress(args: argparse.Namespace):
    if args.json or getattr(args, "quiet", False):
        return NullProgress()
    return TerminalProgress()


class TerminalProgress:
    """Per-dataset, per-chunk progress - including how much rate-limit budget is left."""

    def dataset_started(self, plan) -> None:
        span = f"{plan.since} .. {plan.until}" if plan.until >= plan.since else "up to date"
        print(f"{plan.dataset} {span}  ({len(plan.chunks)} chunk(s))")

    def chunk_done(self, dataset, index, total, label, stats, remaining) -> None:
        parts = [f"+{stats.rows_inserted} new"]
        if stats.rows_duplicate:
            parts.append(f"={stats.rows_duplicate} already there")
        if stats.payloads_quarantined:
            parts.append(f"{stats.payloads_quarantined} quarantined")
        budget = "" if remaining is None else f", {remaining} requests left"
        print(f"  [{index}/{total}] {label}: {', '.join(parts)}{budget}")

    def note(self, text: str) -> None:
        print(f"  note: {text}")

    def dataset_finished(self, report) -> None:
        suffix = f" - {report.error}" if report.error else ""
        print(f"  => {report.status}: {report.rows_inserted} new rows, {report.rows_duplicate} already there{suffix}")


def _print_plan(args: argparse.Namespace, plan) -> None:
    if args.json:
        print_json(plan.as_dict())
        return
    for line in plan.describe():
        print(line)


def _present(args: argparse.Namespace, session: Session, report: ImportReport) -> int:
    if args.json:
        print_json(report.as_dict())
        return report.exit_code
    print(
        render_table(
            ("DATASET", "STATUS", "CHUNKS", "NEW", "EXISTING", "QUAR.", "REQS", "WATERMARK"),
            [
                (
                    d.dataset,
                    d.status,
                    f"{d.chunks_fetched}/{d.chunks_planned}" if d.chunks_planned else str(d.chunks_fetched),
                    d.rows_inserted,
                    d.rows_duplicate,
                    d.quarantined,
                    d.requests,
                    d.watermark or "-",
                )
                for d in sorted(report.datasets, key=lambda d: d.dataset)
            ],
        )
    )
    totals = report.totals
    print(
        f"\nrun {report.run_id}: {report.status} - {totals['rows_inserted']} new rows, "
        f"{totals['rows_duplicate']} already present, {totals['requests']} requests, "
        f"{totals['quarantined']} quarantined, {report.duration_ms} ms"
    )
    for note in report.notes:
        print(f"  note: {note}")
    # "warning" is a word people skim past; a dataset that was refused has no rows in the
    # store, and that has to read like the failure it is.
    for error in report.errors:
        print(f"  error: {error}")
    for warning in report.warnings:
        print(f"  warning: {warning}")
    if report.path:
        print(f"  report: {report.path}")
    if report.exit_code == EXIT_INTERRUPTED:
        print("  interrupted: completed chunks are stored - re-run the same command to continue")
    elif report.exit_code in (EXIT_PARTIAL_FAILURE, EXIT_TOTAL_FAILURE):
        print("  the failed days were not marked imported: fix the cause, then re-run the same command")
    return report.exit_code


# --- the reporting subcommands ---------------------------------------------
def _open_for_reading(session: Session):
    try:
        return session.read_store()
    except StoreUnavailableError:
        return None


def cmd_import_history(args: argparse.Namespace, session: Session) -> int:
    store = _open_for_reading(session)
    if store is None:
        print(f"no store at {session.store_path}; nothing has been imported")
        return EXIT_NO_STORE
    try:
        runs = RunLog(store).recent(limit=args.limit, dataset=args.dataset)
        if args.json:
            print_json({"count": len(runs), "runs": runs})
            return EXIT_OK
        if not runs:
            print("no import runs recorded")
            return EXIT_OK
        print(
            render_table(
                ("STARTED", "RUN", "DATASET", "SOURCE", "STATUS", "NEW", "REQS", "WATERMARK", "RANGE", "ERROR"),
                [
                    (
                        str(r["started_at"])[:19],
                        r["run_id"],
                        r["dataset"],
                        r["source"],
                        r["status"],
                        r["rows_inserted"],
                        r["requests"],
                        r["watermark"] or "-",
                        f"{str(r['since'])[:10]}..{str(r['until'])[:10]}",
                        (r["error"] or "-")[:70],
                    )
                    for r in runs
                ],
            )
        )
    finally:
        store.close()
    return EXIT_OK


def cmd_import_report(args: argparse.Namespace, session: Session) -> int:
    directory = session.paths.imports
    path: Path | None
    if args.run_id:
        path = directory / f"{args.run_id}.json"
        if not path.exists():
            raise UsageError(f"no report for run {args.run_id!r}", hint=f"reports live in {directory}")
    else:
        path = ImportReport.latest(directory)
    if path is None or not Path(path).exists():
        print(f"no import reports in {directory} yet")
        return EXIT_NO_STORE
    payload: dict[str, Any] = ImportReport.read(path)
    if args.json:
        print_json(payload)
        return int(payload.get("exit_code", EXIT_OK))
    totals = payload.get("totals", {})
    print(f"report: {path}")
    print(
        f"run {payload['run_id']}  {payload['source']}  {payload['status']}  "
        f"{payload['since']} .. {payload['until']}"
    )
    print(
        f"  {totals.get('rows_inserted', 0)} new rows, {totals.get('requests', 0)} requests, "
        f"{totals.get('quarantined', 0)} quarantined, {totals.get('duration_ms', 0)} ms"
    )
    for dataset in payload.get("datasets", []):
        note = f" - {dataset['error']}" if dataset.get("error") else ""
        print(
            f"  {dataset['dataset']:<15} {dataset['status']:<10} +{dataset['rows_inserted']:<6} "
            f"watermark {dataset['watermark'] or '-'}{note}"
        )
    for error in payload.get("errors", []):
        print(f"  error: {error}")
    for warning in payload.get("warnings", []):
        print(f"  warning: {warning}")
    return int(payload.get("exit_code", EXIT_OK))


def cmd_import_watermarks(args: argparse.Namespace, session: Session) -> int:
    store = _open_for_reading(session)
    if store is None:
        print(f"no store at {session.store_path}; nothing has been imported")
        return EXIT_NO_STORE
    try:
        source = _source(args.source)
        marks = Watermarks(store).all(source=source)
        outcomes = RunLog(store).outcomes(source)
        if args.json:
            payload = []
            for mark in marks:
                entry = mark.as_dict()
                entry.update(outcomes.get(mark.dataset, {}))
                payload.append(entry)
            print_json({"watermarks": payload})
            return EXIT_OK
        if not marks:
            print("no watermarks yet - every dataset will start from its default --since")
            return EXIT_OK
        print(
            render_table(
                ("DATASET", "SOURCE", "LAST DAY", "LAST POINT", "UPDATED", "LAST STATUS", "ERROR"),
                [
                    (
                        m.dataset,
                        m.source,
                        m.last_day.isoformat() if m.last_day else "-",
                        m.last_ts.isoformat() if m.last_ts else "-",
                        m.updated_at.isoformat()[:19] if m.updated_at else "-",
                        outcomes.get(m.dataset, {}).get("status", "-"),
                        (outcomes.get(m.dataset, {}).get("error") or "-")[:48],
                    )
                    for m in marks
                ],
            )
        )
    finally:
        store.close()
    return EXIT_OK


__all__ = [
    "EarlyExit",
    "TerminalProgress",
    "cmd_import",
    "cmd_import_history",
    "cmd_import_report",
    "cmd_import_watermarks",
    "register",
]
