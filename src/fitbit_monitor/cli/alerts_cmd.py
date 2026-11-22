"""``fm alerts`` - read the alert history back.

Alerts are written by the sinks during any run (``fm plugins run``, and after each
import once the ingest epic wires the dispatcher in); this is how you look at them
later, from a shell or a script.
"""

from __future__ import annotations

import argparse

from ..plugins.models import Severity
from ..store.alerts import AlertHistory
from ..timeutil import parse_date_arg
from .common import EXIT_NO_STORE, EXIT_OK, Session, is_day_only, print_json, render_table


def register(sub: "argparse._SubParsersAction") -> None:
    alerts = sub.add_parser("alerts", help="query the stored alert history")
    asub = alerts.add_subparsers(dest="alerts_command", required=True)

    listing = asub.add_parser("list", help="list alerts, newest first")
    listing.add_argument("--since", default=None, help="only alerts seen at or after this time (default: all)")
    listing.add_argument("--until", default=None, help="only alerts seen before this time")
    listing.add_argument("--severity", default=None, choices=["info", "warning", "critical"],
                         help="this severity or higher")
    listing.add_argument("--plugin", default=None, help="only this plugin's alerts")
    listing.add_argument("--limit", type=int, default=50)
    listing.add_argument("--json", action="store_true")
    listing.set_defaults(func=cmd_alerts_list)
    return alerts


def cmd_alerts_list(args: argparse.Namespace, session: Session) -> int:
    path = session.store_path
    if not path.exists():
        print(f"no store at {path}; nothing has run yet")
        return EXIT_NO_STORE
    history = AlertHistory.open(path)
    try:
        since = parse_date_arg(args.since) if args.since else None
        until = parse_date_arg(args.until, end_of_day=is_day_only(args.until)) if args.until else None
        alerts = history.list(
            since=since,
            until=until,
            severity=Severity.parse(args.severity) if args.severity else None,
            plugin=args.plugin,
            limit=args.limit,
        )
        if args.json:
            print_json({"count": len(alerts), "alerts": [a.as_dict() for a in alerts]})
            return EXIT_OK
        if not alerts:
            print("no alerts" + (f" since {args.since}" if args.since else ""))
            return EXIT_OK
        rows = []
        for alert in alerts:
            repeat = f" x{alert.occurrences}" if alert.occurrences > 1 else ""
            rows.append(
                (
                    alert.last_seen_at.strftime("%Y-%m-%d %H:%M") if alert.last_seen_at else "-",
                    alert.severity.value,
                    f"{alert.plugin}/{alert.key}{repeat}",
                    alert.title,
                )
            )
        print(render_table(("SEEN", "SEVERITY", "PLUGIN/KEY", "TITLE"), rows))
    finally:
        history.close()
    return EXIT_OK
