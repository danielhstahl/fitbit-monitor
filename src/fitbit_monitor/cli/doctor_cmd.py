"""``fm doctor`` - "what is wrong with this install?", declared before it is built.

A scaffold stub: in the parser, documented, exits
:data:`~fitbit_monitor.cli.common.EXIT_NOT_IMPLEMENTED` rather than printing a
checklist it has not produced. The checks it will run are config parses, credential
file permissions, store schema version and integrity, plugin load errors, and the
last import's failures - each of which is a command that already exists and works.

So the stub is not a dead end: it names the commands that answer the question today,
piece by piece, and gives one exit code a script can branch on.
"""

from __future__ import annotations

import argparse

from .common import Session, not_implemented

DESCRIPTION = """\
Diagnose this install: config, credentials, store, plugins, last import.

NOT IMPLEMENTED YET. This command is declared so the surface is stable; running it
exits 72 (not implemented) and names the commands that check these things today.
"""

EPILOG = """\
today, the same answers come one at a time from:
  fm config show                    the effective config, and what did not parse
  fm auth status                    token, scopes and expiry
  fm db status                      schema version and what is in the schema
  fm store verify                   re-hash the retained raw payloads
  fm plugins validate               load and configure every plugin without running it
"""


def register(sub: "argparse._SubParsersAction") -> None:
    doctor = sub.add_parser(
        "doctor",
        help="diagnose the install (planned - not implemented)",
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    doctor.add_argument("--json", action="store_true", help="machine-readable output")
    doctor.add_argument("--quick", action="store_true", help="skip the slow checks")
    doctor.set_defaults(func=cmd_doctor)
    return doctor


def cmd_doctor(args: argparse.Namespace, session: Session) -> int:
    """Refuse honestly: no check has run, so no claim is made."""
    return not_implemented(
        "fm doctor",
        planned="config, credentials, store and plugin checks in one pass, each with a verdict",
        instead=(
            "fm config show",
            "fm auth status",
            "fm db status",
            "fm store verify",
            "fm plugins validate",
        ),
    )
