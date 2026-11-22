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
4    the local store is missing or unusable
===  ==========================================
"""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

from . import alerts_cmd, dev_cmd, plugins_cmd
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
    plugins_cmd.register(sub)
    alerts_cmd.register(sub)
    dev_cmd.register(sub)
    return parser


_EPILOG = """\
examples:
  fm dev seed --days 45                     sample data to try things on
  fm plugins list                           what is installed (and what is broken)
  fm plugins run --all --since 30d          compute metrics, deliver alerts
  fm alerts list --since 7d                what has been flagged
  fm plugin new resting_hrv --dir ~/plugins scaffold a plugin

Plugin system docs: docs/plugins.md, and `fm plugins show NAME` for any plugin's options.
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
