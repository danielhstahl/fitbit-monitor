"""``fm store`` - prove the local copy is intact, get it out, and set its retention.

This database ends up being the only durable copy of someone's health history, so the
commands here are the difference between "I have a database" and "I have my data":

``fm store stats``     what is in here, and what it costs
``fm store verify``    re-hash the retained raw payloads and re-check the invariants
``fm store export``    a portable copy of the series or of the original payloads
``fm store backup``    one consistent file, safe to copy anywhere
``fm store prune``     retention - and it **shows what it would do until you say --execute**

:mod:`fitbit_monitor.store.maintenance` does the work; this module only turns arguments
into calls and results into text or JSON.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from ..store import maintenance
from ..store.maintenance import DEFAULT_RETENTION_DAYS, human_bytes
from ..store.writer import write_lock
from ..timeutil import parse_date_arg
from .common import EXIT_ERROR, EXIT_NO_STORE, EXIT_OK, Session, is_day_only, print_json, render_table


def register(sub: "argparse._SubParsersAction") -> None:
    store = sub.add_parser("store", help="inspect, verify, export, back up and prune the store")
    ssub = store.add_subparsers(dest="store_command", required=True)

    stats = ssub.add_parser("stats", help="row counts, coverage and bytes per dataset")
    stats.add_argument("--json", action="store_true")
    stats.set_defaults(func=cmd_store_stats)

    verify = ssub.add_parser("verify", help="re-hash stored payloads and check integrity (nonzero exit on trouble)")
    verify.add_argument("--quick", action="store_true", help="skip re-hashing the raw payload bodies")
    verify.add_argument("--sample", type=int, default=None, help="re-hash at most N payloads (big stores)")
    verify.add_argument("--json", action="store_true")
    verify.set_defaults(func=cmd_store_verify)

    export = ssub.add_parser("export", help="write a portable copy of the series or the raw payloads")
    export.add_argument("--format", default="jsonl", choices=("jsonl", "csv"), help="output format (default jsonl)")
    export.add_argument("--what", default="series", choices=("series", "raw"), help="what to export (default series)")
    export.add_argument("--dataset", default=None, help="only this dataset")
    export.add_argument("--since", default=None, help="start of the window (default: everything)")
    export.add_argument("--until", default=None, help="end of the window")
    export.add_argument("--output", default=None, help="write to this file instead of stdout")
    export.add_argument("--json", action="store_true", help="print a summary of the export instead of the data")
    export.set_defaults(func=cmd_store_export)

    backup = ssub.add_parser("backup", help="write one consistent snapshot with the SQLite backup API")
    backup.add_argument("--output", default=None, help="destination file (default: <data-dir>/backups/store-<utc>.sqlite3)")
    backup.add_argument("--json", action="store_true")
    backup.set_defaults(func=cmd_store_backup)

    prune = ssub.add_parser("prune", help="delete old data - dry run unless --execute is given")
    prune.add_argument("--days", type=int, default=DEFAULT_RETENTION_DAYS, help=f"keep this many days (default {DEFAULT_RETENTION_DAYS})")
    prune.add_argument(
        "--what",
        default="raw",
        choices=maintenance.PRUNE_WHAT,
        help="what to prune (default raw: the stored responses, not the series plugins read)",
    )
    prune.add_argument("--execute", action="store_true", help="actually delete; without this nothing is removed")
    prune.add_argument("--vacuum", action="store_true", help="VACUUM afterwards to return the space to the filesystem")
    prune.add_argument("--json", action="store_true")
    prune.set_defaults(func=cmd_store_prune)
    return store


def _window(args: argparse.Namespace) -> tuple[datetime | None, datetime | None]:
    since = parse_date_arg(args.since) if getattr(args, "since", None) else None
    until = (
        parse_date_arg(args.until, end_of_day=is_day_only(args.until))
        if getattr(args, "until", None)
        else None
    )
    return since, until


def cmd_store_stats(args: argparse.Namespace, session: Session) -> int:
    path = session.store_path
    if not path.exists():
        print(f"no store at {path}")
        print("  create one with `fm db init`, or `fm dev seed --days 90` for sample data")
        return EXIT_NO_STORE
    store = session.read_store()
    try:
        report = maintenance.stats(store.conn, path=path)
    finally:
        store.close()
    if args.json:
        print_json(report)
        return EXIT_OK
    totals = report["totals"]
    print(f"store: {path}  ({human_bytes(report['file_bytes'])}, schema version {report['schema_version']})")
    if not report["datasets"]:
        print("  no datasets registered - nothing has been stored yet")
        return EXIT_OK
    print(
        render_table(
            ("DATASET", "KIND", "POINTS", "DAYS", "FIRST", "LAST", "NORMALIZED", "RAW", "STORED"),
            [
                (
                    entry["dataset"],
                    entry["kind"],
                    entry["points"],
                    entry["days_present"],
                    (entry["first_ts"] or "-")[:10],
                    (entry["last_ts"] or "-")[:10],
                    human_bytes(entry["normalized_bytes"]),
                    human_bytes(entry["payload_bytes"]),
                    human_bytes(entry["stored_bytes"]),
                )
                for entry in report["datasets"]
            ],
        )
    )
    print(
        f"  {totals['points']} points over {totals['days_with_data']} days; "
        f"normalized {human_bytes(totals['normalized_bytes'])}, "
        f"raw {human_bytes(totals['raw_bytes'])} in {totals['payloads']} payload(s) "
        f"({human_bytes(totals['raw_stored_bytes'])} stored"
        + (f", ratio {totals['compression_ratio']})" if totals["compression_ratio"] is not None else ")")
    )
    print(
        f"  duplicates refused at import: {totals['duplicate_rows_by_import']} row(s), "
        f"{totals['duplicate_payloads_by_import']} payload(s); quarantined: {totals['quarantined']}"
    )
    print(
        f"  free pages: {report['pragmas']['free_pages']} "
        f"({human_bytes(report['pragmas']['free_pages'] * report['pragmas']['page_size'])} reclaimable"
        " with `fm store prune --vacuum`)"
    )
    return EXIT_OK


def cmd_store_verify(args: argparse.Namespace, session: Session) -> int:
    path = session.store_path
    if not path.exists():
        print(f"no store at {path}")
        return EXIT_NO_STORE
    store = session.read_store()
    try:
        result = maintenance.verify(store.conn, include_bodies=not args.quick, sample=args.sample)
    finally:
        store.close()
    if args.json:
        print_json(result.as_dict())
        return EXIT_OK if result.ok else EXIT_ERROR
    print(f"verifying {path}")
    for check in result.checks:
        mark = "ok  " if check.ok else "FAIL"
        extra = f" ({check.checked})" if check.checked and not check.ok else ""
        print(f"  {mark} {check.name}{extra}")
        for problem in check.problems:
            print(f"        {problem}")
    if result.ok:
        print(f"store verified: {len(result.checks)} checks passed, nothing to report")
        return EXIT_OK
    print(f"{result.problem_count} problem(s) found - the data on disk does not match its own record")
    print("  if you have a backup: `fm store backup` is one-way traffic, restore from the backup instead")
    return EXIT_ERROR


def cmd_store_export(args: argparse.Namespace, session: Session) -> int:
    since, until = _window(args)
    path = session.store_path
    if not path.exists():
        print(f"no store at {path}")
        return EXIT_NO_STORE
    store = session.read_store()
    try:
        destination = Path(args.output) if args.output else None
        handle: TextIO
        if destination is not None:
            destination.parent.mkdir(parents=True, exist_ok=True)
            handle = destination.open("w", encoding="utf-8", newline="")
        else:
            handle = sys.stdout
        try:
            if args.what == "raw":
                written = maintenance.export_raw(
                    store.conn, handle, fmt=args.format, dataset=args.dataset, since=since, until=until
                )
            else:
                written = maintenance.export_series(
                    store.conn, handle, fmt=args.format, dataset=args.dataset, since=since, until=until
                )
        finally:
            if destination is not None:
                handle.close()
    finally:
        store.close()
    summary = {
        "what": args.what,
        "format": args.format,
        "dataset": args.dataset,
        "since": since.isoformat() if since else None,
        "until": until.isoformat() if until else None,
        "records": written,
        "output": str(args.output) if args.output else "stdout",
    }
    if args.json:
        print_json(summary)
    elif args.output:
        print(f"wrote {written} {args.what} record(s) to {args.output} ({args.format})")
    else:
        print(
            f"# {written} {args.what} record(s) in {args.format}",
            file=sys.stderr,
        )
    return EXIT_OK


def cmd_store_backup(args: argparse.Namespace, session: Session) -> int:
    source = session.store_path
    if not source.exists():
        print(f"no store at {source}")
        return EXIT_NO_STORE
    destination = Path(args.output) if args.output else _default_backup_path(session)
    with write_lock(source, what="an import"):
        snapshot = maintenance.backup(source, destination)
    if args.json:
        print_json(snapshot.as_dict())
        return EXIT_OK if snapshot.ok else EXIT_ERROR
    if snapshot.ok:
        print(f"backed up {source} -> {snapshot.path} ({human_bytes(snapshot.bytes)}, {snapshot.pages} pages)")
        print("  the snapshot passed integrity_check; copy it anywhere, `fm --db <file> db status` reads it")
        return EXIT_OK
    print(f"backup written to {snapshot.path} but it did NOT pass integrity_check", file=sys.stderr)
    return EXIT_ERROR


def _default_backup_path(session: Session) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    directory = session.paths.data_dir / "backups"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"store-{stamp}.sqlite3"


def cmd_store_prune(args: argparse.Namespace, session: Session) -> int:
    path = session.store_path
    if not path.exists():
        print(f"no store at {path}")
        return EXIT_NO_STORE
    with write_lock(path, what="an import"):
        store = session.write_store()
        try:
            result = maintenance.prune(
                store,
                days=args.days,
                what=(args.what,),
                execute=args.execute,
                vacuum=args.vacuum,
            )
        finally:
            store.close()
    if args.json:
        print_json(result.as_dict())
        return EXIT_OK
    verb = "would delete" if result.dry_run else "deleted"
    horizon = result.cutoff.strftime("%Y-%m-%d %H:%M UTC")
    print(f"prune: {verb} data older than {horizon} ({result.days} days kept, {', '.join(result.what)})")
    if not result.rows:
        print("  nothing older than that - nothing to do")
        return EXIT_OK
    for table, count in result.removed.items():
        print(f"  {table:<14} {count}")
    if result.bytes_freed:
        print(f"  {human_bytes(result.bytes_freed)} of stored payloads")
    if result.dry_run:
        print("\ndry run: nothing was deleted. Re-run with --execute to delete.")
    elif result.vacuumed:
        print("  vacuumed: the file itself shrank")
    else:
        print("  deleted. The file keeps the space until you VACUUM (see --vacuum) or `fm store backup`.")
    return EXIT_OK


__all__ = ["cmd_store_backup", "cmd_store_export", "cmd_store_prune", "cmd_store_stats", "cmd_store_verify", "register"]
