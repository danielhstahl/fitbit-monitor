"""``fm report`` - the cross-run reporting surface, declared before it is built.

This is a scaffold stub: the command is in the parser, ``--help`` says what it will
mean, and running it exits :data:`~fitbit_monitor.cli.common.EXIT_NOT_IMPLEMENTED`
instead of pretending. Declaring the surface first is what lets a cron line, a doc
page, or a test name ``fm report`` before the first line of its logic exists.

What the job already has today is split across three commands, and they are the
answer the stub prints:

``fm import report [--run-id R]``  per-dataset counts for one import run
``fm import history``             the recent runs and how each ended
``fm alerts list [--since D]``     what the plugins have flagged

``fm report`` will be the one place that joins them - a range in, one document out -
which is why it is a separate name rather than a fourth subcommand of ``fm import``.
"""

from __future__ import annotations

import argparse

from .common import Session, not_implemented

DESCRIPTION = """\
Build a report over a date range: what was imported, what it covers, what was flagged.

NOT IMPLEMENTED YET. This command is declared so the surface is stable; running it
exits 72 (not implemented) and tells you where the numbers live today.
"""

EPILOG = """\
today, the same numbers come from:
  fm import history                 recent runs and how each ended
  fm import report --run-id RUN     per-dataset counts for one run
  fm store stats                    rows, coverage and bytes per dataset
  fm alerts list --since 7d         what plugins have flagged
"""


def register(sub: "argparse._SubParsersAction") -> None:
    report = sub.add_parser(
        "report",
        help="report over a range (planned - not implemented)",
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    report.add_argument("--since", default=None, help="start of the range (date or Nd)")
    report.add_argument("--until", default=None, help="end of the range (date or Nd)")
    report.add_argument("--json", action="store_true", help="machine-readable output")
    report.set_defaults(func=cmd_report)
    return report


def cmd_report(args: argparse.Namespace, session: Session) -> int:
    """Refuse honestly: nothing is computed and nothing is written."""
    return not_implemented(
        "fm report",
        planned="one document over a range, joining import runs, coverage and alerts",
        instead=("fm import report", "fm import history", "fm store stats", "fm alerts list"),
    )
