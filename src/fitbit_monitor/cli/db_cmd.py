"""``fm db`` - the store's own schema: create it, migrate it, inspect it.

The schema is numbered SQL (``fitbit_monitor/store/migrations``) and every ordinary
command migrates on open, so nobody *has* to run these. They exist because "what version
is this file, and what would an upgrade do to it?" is a question you want to answer
*before* you point a long import at somebody's only copy of their data - and because a
migration that only ever runs implicitly is a migration nobody tests.

``fm db init`` is idempotent; ``fm db migrate`` refuses a store written by a newer fm
rather than guessing at a schema it has not seen.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from ..store import schema
from .common import EXIT_NO_STORE, EXIT_OK, Session, print_json


def register(sub: "argparse._SubParsersAction") -> None:
    db = sub.add_parser("db", help="the store schema: init, migrate, status")
    dsub = db.add_subparsers(dest="db_command", required=True)

    init = dsub.add_parser("init", help="create the store and apply every migration")
    init.add_argument("--json", action="store_true")
    init.set_defaults(func=cmd_db_init)

    migrate = dsub.add_parser("migrate", help="apply pending migrations to an existing store")
    migrate.add_argument("--json", action="store_true")
    migrate.set_defaults(func=cmd_db_migrate)

    status = dsub.add_parser("status", help="schema version, objects, and pragmas in force")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_db_status)
    return db


def _applied(payload: list) -> list[str]:
    return [migration.label for migration in payload]


def cmd_db_init(args: argparse.Namespace, session: Session) -> int:
    path = session.store_path
    existed = path.exists()
    session.paths.ensure_data_dir()
    applied = schema.init_db(path)
    if args.json:
        print_json(
            {
                "store": str(path),
                "created": not existed,
                "schema_version": schema.SCHEMA_VERSION,
                "applied": _applied(list(applied)),
            }
        )
        return EXIT_OK
    verb = "already existed at" if existed else "created"
    print(f"store {verb} schema version {schema.SCHEMA_VERSION}: {path}")
    if applied:
        print(f"  applied {len(applied)} migration(s): {', '.join(_applied(list(applied)))}")
    else:
        print("  no pending migrations")
    return EXIT_OK


def cmd_db_migrate(args: argparse.Namespace, session: Session) -> int:
    path = session.store_path
    if not path.exists():
        print("no store at", path)
        print("  create one with `fm db init` (or `fm dev seed` for a seeded one)")
        return EXIT_NO_STORE
    conn = schema.connect(path)
    try:
        before = schema.read_version(conn)
        applied = list(schema.migrate_store(conn))
        version = schema.read_version(conn)
    finally:
        conn.close()
    if args.json:
        print_json(
            {
                "store": str(path),
                "from_version": before,
                "schema_version": version,
                "applied": _applied(applied),
            }
        )
        return EXIT_OK
    if not applied:
        print(f"already at schema version {version}: {path}")
        return EXIT_OK
    print(f"migrated {path}: {before} -> {version}")
    for migration in applied:
        print(f"  applied {migration.label}")
    return EXIT_OK


def cmd_db_status(args: argparse.Namespace, session: Session) -> int:
    """Informational: the exit code says "no store" (4) or nothing, never "out of date"."""
    path = session.store_path
    if not path.exists():
        print(f"no store at {path}")
        print("  create one with `fm db init`, or `fm dev seed --days 90` for sample data")
        return EXIT_NO_STORE
    conn = schema.connect(path)
    try:
        info = schema.inspect(conn)
        pending = [migration.label for migration in schema.pending(conn)]
    finally:
        conn.close()
    info["store"] = str(path)
    info["file_bytes"] = Path(path).stat().st_size
    info["pending"] = pending
    if args.json:
        print_json(info)
        return EXIT_OK
    print(f"store: {path} ({info['file_bytes']} bytes)")
    print(f"  schema version {info['schema_version']} (this build knows {info['schema_version_known']})")
    if pending:
        print(f"  pending: {', '.join(pending)}  (run `fm db migrate`)")
    elif info["up_to_date"]:
        print("  up to date")
    else:
        print("  this store is from a newer fm: upgrade before writing to it")
    print("  pragmas: " + ", ".join(f"{key}={value}" for key, value in info["pragmas"].items()))
    print(f"  {len(info['tables'])} tables: {', '.join(info['tables'])}")
    if info["views"]:
        print(f"  {len(info['views'])} views: {', '.join(info['views'])}")
    print(f"  {len(info['indexes'])} indexes")
    return EXIT_OK


__all__ = ["cmd_db_init", "cmd_db_migrate", "cmd_db_status", "register"]
