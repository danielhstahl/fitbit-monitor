"""The ``fm`` command line.

Thin by design: ``main`` parses, builds a :class:`~fitbit_monitor.cli.common.Session`,
and calls one ``cmd_*`` function. Every command module takes ``(args, session)`` and
returns an exit code, so nothing below the parser prints or exits and everything is
importable and testable without spawning a process.

Exit codes (the same for every subcommand):

===  ==========================================
0    success - including "ran and found nothing"
1    a plugin errored, or an operation failed
2    bad arguments, or config that does not parse
3    an import partially failed (some datasets landed, some did not)
4    the local store is missing or unusable
5    an import landed with warnings (quarantined payloads, bad rows)
130  an import was interrupted; what was fetched is stored
===  ==========================================
"""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

from . import alerts_cmd, auth_cmd, config_cmd, db_cmd, dev_cmd, import_cmd, plugins_cmd, store_cmd
from .common import (
    EXIT_ERROR,
    EXIT_NO_STORE,
    EXIT_OK,
    EXIT_USAGE,
    add_common_args,
    configure_logging,
    session_from_args,
)

PROG = "fm"
DESCRIPTION = "Local-first Fitbit monitor: store your data, then compute and alert on it."


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROG, description=DESCRIPTION, epilog=_EPILOG,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    add_common_args(parser)
    parser.add_argument("--version", action="store_true", help="print the package version and exit")
    sub = parser.add_subparsers(dest="command", required=False)
    config_cmd.register(sub)
    auth_cmd.register(sub)
    db_cmd.register(sub)
    store_cmd.register(sub)
    import_cmd.register(sub)
    plugins_cmd.register(sub)
    alerts_cmd.register(sub)
    dev_cmd.register(sub)
    return parser


_EPILOG = """\
examples:
  fm config init                              write the starter config.toml
  fm db status                                schema version and what is in the schema
  fm dev seed --days 90                       deterministic sample data (not real health data)
  fm auth login                               authorize fm against your account
  fm import --dry-run                         what an import would cost, before spending it
  fm import --since 7d                        pull the last week (or: since the last import)
  fm import --source csv --path export.zip    backfill from the Fitbit web export
  fm store stats                              rows, coverage and bytes per dataset
  fm store verify                             re-hash the retained raw payloads
  fm store backup                             one consistent snapshot file
  fm store prune --days 365                   what retention would delete (dry run)
  fm plugins list                             what is installed (and what is broken)
  fm plugins run --all --since 30d            compute metrics, deliver alerts
  fm alerts list --since 7d                   what has been flagged
  fm plugin new resting_hrv --dir ~/plugins   scaffold a plugin

Docs: docs/storage.md (the store, retention, the read API), docs/importing.md (backfill,
overlaps, rate limits), docs/plugins.md (writing a plugin).
"""


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "version", False):
        from .. import __version__

        print(f"fm (fitbit-monitor) {__version__}")
        return EXIT_OK
    func = getattr(args, "func", None)
    if (
        getattr(args, "command", None) == "plugins"
        and getattr(args, "api", False)
        and not getattr(args, "plugins_command", None)
    ):
        from ..plugins.contract import PLUGIN_API_VERSION

        print(f"plugin API version {PLUGIN_API_VERSION}")
        return EXIT_OK
    if func is None:
        parser.print_help()
        return EXIT_USAGE
    try:
        session = session_from_args(args)
        configure_logging(session.verbose, getattr(args, "quiet", False))
        return int(func(args, session))
    except SystemExit as exc:  # argparse --help / --error paths
        return int(exc.code or 0)
    except KeyboardInterrupt:
        print("interrupted: anything already committed is stored", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI boundary: report, do not traceback
        from ..errors import FmError

        if isinstance(exc, FmError):
            print(f"error: {exc}", file=sys.stderr)
            return int(exc.exit_code or EXIT_ERROR)
        if session.verbose:
            raise
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ERROR


def cli() -> None:  # pragma: no cover - console_script shim
    sys.exit(main())


EXIT_CODES = {"ok": EXIT_OK, "error": EXIT_ERROR, "usage": EXIT_USAGE, "no_store": EXIT_NO_STORE}

__all__ = ["EXIT_CODES", "build_parser", "cli", "main"]
