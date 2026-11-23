"""``fm api probe`` - why an import lands nothing, asked of the API instead of the logs.

One command, four answers, all of them about the account behind the stored token:

* **scopes** - what the token was actually given versus what the catalog wants, naming
  every missing scope and the datasets it disables (the usual cause of an empty import,
  and the one a user cannot see from the outside);
* **datasets** - whether each dataset answered with data, or with ``403`` (refused), or
  with ``404`` (nothing for those days), or with something unexpected;
* **rate limit** - the ``X-RateLimit-*`` headers observed on the way, parsed and verbatim,
  with the wall-clock time the window resets;
* **history** - the earliest date of data available per dataset, bounded by a doubling
  walk back through time rather than guessed at.

The command is read-only, costs a bounded number of requests (``--max-requests``), and
never raises for an API answer: a refused endpoint is a line in the report, not a
traceback, so one bad dataset still leaves the other four reported.

``--json`` is the same report as one object, which is what ``fm doctor --probe`` consumes.
Exit codes follow ``fm import``'s ladder: **0** everything the catalog knows is available,
**5** something is unavailable (a scope missing, a dataset refused), **1** nothing is
available, **2** there was no token to ask with.
"""

from __future__ import annotations

import argparse
from datetime import date, timedelta

from ..api.errors import NotAuthenticatedError
from ..api.probe import DEFAULT_MAX_REQUESTS, run_probe
from ..errors import UsageError
from ..timeutil import parse_date_arg
from .common import EXIT_USAGE, Session, is_day_only, print_json

DESCRIPTION = """\
Ask the health API what this account allows: granted scopes, which datasets answer,
the rate limit in force, and the earliest date of data available per dataset.

Read-only. Costs a bounded number of requests (--max-requests). Nothing in the API's
answer, not even a 403 or a body this build does not recognise, is turned into a crash.
"""

EPILOG = """\
examples:
  fm api probe                                    everything, history walked back to signup
  fm api probe --json | jq '.datasets[] | {dataset, status, earliest}'
  fm api probe --no-history                       the cheap version: one request per dataset
  fm api probe --dataset spo2                     just one dataset
  fm api probe --since 2026-05-01 --until 2026-05-14
  fm api probe --fixtures tests/fixtures/api      rehearse it offline

exit codes:
  0  every dataset the catalog knows answered with data
  5  some did not (a missing scope, a refusal, no data) - see the report
  1  nothing answered with data
  2  not authenticated, or a dataset name that does not exist

see also: fm auth status --probe, fm import --dry-run, fm doctor --probe
"""


def register(sub: "argparse._SubParsersAction") -> None:
    api = sub.add_parser(
        "api",
        help="ask the live API what this account allows (probe)",
        description="Inspect the API side of this install.",
    )
    asub = api.add_subparsers(dest="api_command", required=True)

    probe = asub.add_parser(
        "probe",
        help="report granted scopes, dataset availability, rate-limit state, earliest data",
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    probe.add_argument(
        "--dataset",
        action="append",
        metavar="NAME",
        help="only this dataset (repeatable, or comma-separated); default: every dataset in the catalog",
    )
    probe.add_argument(
        "--since",
        default=None,
        help="oldest day to look for data (default: the account's memberSinceDate)",
    )
    probe.add_argument("--until", default=None, help="newest day to look for data (default: today)")
    probe.add_argument(
        "--no-history",
        dest="history",
        action="store_false",
        help="do not walk back through history: one request per dataset, no earliest date",
    )
    probe.set_defaults(history=True)
    probe.add_argument(
        "--max-requests",
        type=int,
        default=DEFAULT_MAX_REQUESTS,
        metavar="N",
        help=f"stop probing after N requests (default {DEFAULT_MAX_REQUESTS}; 0 asks nothing)",
    )
    probe.add_argument(
        "--fixtures",
        default=None,
        metavar="DIR",
        help="replay recorded responses from DIR instead of calling the API (offline rehearsal)",
    )
    probe.add_argument("--json", action="store_true", help="print the report as one JSON object")
    probe.set_defaults(func=cmd_api_probe)


def cmd_api_probe(args: argparse.Namespace, session: Session) -> int:
    client = session.api_client(fixtures=args.fixtures)
    if not client.authenticated:
        return _cannot_probe(args, "no stored credentials")
    since, until = _window(args)
    try:
        report = run_probe(
            client,
            datasets=args.dataset,
            since=since,
            until=until,
            history=args.history,
            max_requests=args.max_requests,
            timezone=session.config.import_.timezone,
        )
    except NotAuthenticatedError as exc:
        return _cannot_probe(args, str(exc))
    if args.json:
        print_json(report.as_dict())
    else:
        for line in report.lines():
            print(line)
    return report.exit_code


def _window(args: argparse.Namespace) -> tuple[date | None, date | None]:
    """``--since``/``--until`` as calendar days, or None to let the probe choose."""
    since = parse_date_arg(args.since).date() if args.since else None
    until = None
    if args.until:
        until = (parse_date_arg(args.until, end_of_day=is_day_only(args.until)) - timedelta(microseconds=1)).date()
    if since and until and until < since:
        raise UsageError(
            f"--until ({until}) must not be before --since ({since})",
            hint="plain dates are inclusive of both ends: --since 2026-05-01 --until 2026-05-14",
        )
    return since, until


def _cannot_probe(args: argparse.Namespace, reason: str) -> int:
    """No token to ask with: say so in the requested shape, and use the auth exit code."""
    hint = "run `fm auth login` first"
    if args.json:
        print_json(
            {
                "tool": "fm api probe",
                "report_version": 1,
                "authenticated": False,
                "exit_code": EXIT_USAGE,
                "error": reason,
                "hint": hint,
            }
        )
    else:
        print(f"cannot probe: {reason}")
        print(f"  {hint}")
    return EXIT_USAGE


__all__ = ["DESCRIPTION", "EPILOG", "cmd_api_probe", "register"]
