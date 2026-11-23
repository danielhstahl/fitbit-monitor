"""``fm api`` - asking the API side of this install two different questions.

``fm api probe`` asks **what this account allows**: granted scopes, which datasets answer,
the rate limit in force, and the earliest date of data available per dataset.

``fm api source`` asks **whether there is an API to ask**: which host ``[api]`` points at,
whether that host is still a service at all, and - with ``--verify`` - what it says today,
without credentials.

## fm api probe

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

from ..api.errors import NotAuthenticatedError, SourceRetiredError
from ..api.probe import DEFAULT_MAX_REQUESTS, run_probe
from ..api.source import (
    DECIDED_BASE_URL,
    DECIDED_NAME,
    DECISION_DOC,
    UNREACHABLE,
    WORKING_TODAY,
    check_alive,
)
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
        help="ask the API side of this install: what it allows (probe), whether it is alive (source)",
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

    source = asub.add_parser(
        "source",
        help="report which health API this build dials, and whether that host is still a service",
        description=SOURCE_DESCRIPTION,
        epilog=SOURCE_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    source.add_argument(
        "--verify",
        action="store_true",
        help="ask each host live, with no credentials (three requests, no token and no client id needed)",
    )
    source.add_argument("--json", action="store_true", help="print the report as one JSON object")
    source.set_defaults(func=cmd_api_source)


def cmd_api_probe(args: argparse.Namespace, session: Session) -> int:
    client = session.api_client(fixtures=args.fixtures)
    try:
        client.guard_source()
    except SourceRetiredError as exc:
        # A retired host is not an API answer, so the "never raise" rule of the probe does
        # not cover it: asking it four questions would produce four 401s and no diagnosis.
        return _cannot_probe(args, exc.message, hint=exc.hint, code=exc.exit_code)
    if not client.authenticated:
        return _cannot_probe(args, "no stored credentials", authenticated=False)
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


def _cannot_probe(
    args: argparse.Namespace, reason: str, *, hint: str | None = None, code: int = EXIT_USAGE, authenticated: bool = True
) -> int:
    """Nothing could be asked: say so in the requested shape, with the code that fits."""
    hint = hint or "run `fm auth login` first"
    if args.json:
        print_json(
            {
                "tool": "fm api probe",
                "report_version": 1,
                "authenticated": authenticated,
                "exit_code": code,
                "error": reason,
                "hint": hint,
            }
        )
    else:
        print(f"cannot probe: {reason}")
        print(f"  {hint}")
    return code


SOURCE_DESCRIPTION = """\
Report which upstream health API this build dials, and whether that host is still a service.

The default [api] configuration points at the Fitbit Web API, which Google switched off on
2026-11-02. It still answers - with 401s that look like a bad login - which is why every
other command refuses to dial it instead of reporting a dead service as a credential
problem. `fm import` and `fm api probe` call this same question before they spend anything.

--verify asks each host what it is today. It needs no credentials, no client id and no
token, and compares the answers with the dated measurements in ADR-0002: a host that has
started answering 410, or a data endpoint that answers 200 to a stranger, are both events
that re-open that decision, and both are flagged here.
"""

SOURCE_EPILOG = """\
examples:
  fm api source                                  what is configured, and the verdict
  fm api source --verify                         ask the hosts now (3 requests, no credentials)
  fm api source --json | jq '.live[] | {label, status, verdict}'

exit codes:
  0  the configured source is not a retired one
  6  it is retired: nothing this build does against it can work
  2  --verify reached nothing at all (check DNS/proxy), or an offline transport was asked

see also: fm import --source csv, fm doctor, fm auth status
"""


def cmd_api_source(args: argparse.Namespace, session: Session) -> int:
    """``fm api source``: the configured source, its verdict, and optionally the live check."""
    client = session.api_client()
    state = client.source_state()
    observations: tuple = ()
    offline_note = None
    if args.verify:
        if not client.live:
            offline_note = "the transport in use is an offline replay (--fixtures); nothing was asked"
        else:
            observations = check_alive(client.transport)
    code = _source_exit_code(state, observations)
    if args.json:
        print_json(
            {
                "tool": "fm api source",
                "report_version": 1,
                "exit_code": code,
                **state.as_dict(),
                "verified": bool(observations),
                "live": [observation.as_dict() for observation in observations],
                "note": offline_note,
            }
        )
        return code
    print(f"configured source   {state.short()}")
    for name, url in state.endpoints:
        mark = "   <- retired" if any(name == retired_name for retired_name, _url in state.retired) else ""
        print(f"  {name:<18} {url}{mark}")
    print(f"  decided instead    {DECIDED_NAME}  {DECIDED_BASE_URL}")
    print(f"  decision           {DECISION_DOC} (section 10 is the port)")
    print(f"  works today        {WORKING_TODAY}")
    if args.verify:
        if offline_note:
            print(f"\nlive check          {offline_note}")
        else:
            print(f"\nlive check (no credentials, {len(observations)} requests)")
            for observation in observations:
                print(f"  {observation.line()}")
            print(
                "  verdicts describe what answered, not what works: 'alive' means it wants a\n"
                "  credential, which the retired Fitbit host still does while issuing no tokens."
            )
            drifted = [o for o in observations if not o.agrees_with_adr]
            if drifted:
                print(f"  {len(drifted)} of {len(observations)} differ from ADR-0002's measurements: re-read it")
    if state.is_retired:
        print(f"\nrefused: {state.reason()}")
    return code


def _source_exit_code(state, observations: tuple) -> int:
    """6 retired, 2 if a --verify reached nothing, otherwise 0."""
    if state.is_retired:
        return SourceRetiredError.exit_code
    if observations and all(observation.verdict == UNREACHABLE for observation in observations):
        return EXIT_USAGE
    return 0


__all__ = [
    "DESCRIPTION",
    "EPILOG",
    "SOURCE_DESCRIPTION",
    "SOURCE_EPILOG",
    "cmd_api_probe",
    "cmd_api_source",
    "register",
]
