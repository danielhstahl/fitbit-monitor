"""``fm dev`` - developer aids. Sample data, and nothing that pretends to be real.

The seed corpus is deterministic (same ``--seed`` and ``--end``, same bytes), which
is what makes it usable from the benchmark job and from plugin test suites as well
as from a terminal. It is **synthetic**: it did not come from an account or a device,
it never leaves the machine, and the abnormal episodes it can inject are labelled as
synthetic in ``dev_annotation``.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime

from .common import EXIT_OK, Session, print_json


def register(sub: "argparse._SubParsersAction") -> None:
    dev = sub.add_parser("dev", help="developer helpers (sample data)")
    dsub = dev.add_subparsers(dest="dev_command", required=True)

    seed = dsub.add_parser("seed", help="fill the store with deterministic sample data")
    seed.add_argument("--days", type=int, default=90, help="days of history to write (default 90)")
    seed.add_argument("--end", default=None, help="last day written, YYYY-MM-DD (default today)")
    seed.add_argument("--seed", type=int, default=1337, help="RNG seed (default 1337)")
    seed.add_argument(
        "--anomalies",
        type=int,
        default=None,
        help="labelled abnormal episodes to inject (default: one every 14 days)",
    )
    seed.add_argument(
        "--no-anomalies",
        action="store_true",
        help="write a clean corpus with no injected episodes",
    )
    seed.add_argument("--no-gaps", action="store_true", help="write every day (default: skip a few, to exercise coverage)")
    seed.add_argument("--fresh", action="store_true", help="delete existing series rows first")
    seed.add_argument("--json", action="store_true")
    seed.set_defaults(func=cmd_dev_seed)

    info = dsub.add_parser("info", help="what is in the store right now")
    info.add_argument("--json", action="store_true")
    info.set_defaults(func=cmd_dev_info)


def cmd_dev_seed(args: argparse.Namespace, session: Session) -> int:
    from ..store.seed import seed_store

    end = datetime.strptime(args.end, "%Y-%m-%d").date() if args.end else datetime.now(UTC).date()
    anomalies = 0 if args.no_anomalies else args.anomalies
    with session.write_store() as store:
        if args.fresh:
            store.clear()
        plan = seed_store(
            store,
            days=args.days,
            seed=args.seed,
            end=end,
            drop_missing_days=not args.no_gaps,
            anomalies=anomalies,
        )
        totals = {dataset: store.count(dataset) for dataset in sorted(store.datasets())}
    payload = {**plan.as_dict(), "store": str(session.store_path), "totals": totals}
    if args.json:
        print_json(payload)
        return EXIT_OK
    print(f"seeded {plan.points} points into {session.store_path}")
    print(f"  {plan.start} .. {plan.end}  (seed {plan.seed}, {plan.days} days)")
    for dataset, count in totals.items():
        print(f"  {dataset:<16} {count}")
    if plan.dropped_days:
        print(f"  deliberately missing days: {', '.join(d.isoformat() for d in plan.dropped_days)}")
    if plan.episodes:
        print(f"  injected labelled episodes ({len(plan.episodes)}):")
        for episode in plan.episodes:
            print(
                f"    {episode.kind:<15} {episode.start.isoformat(timespec='minutes')} "
                f".. {episode.end.isoformat(timespec='minutes')}  mean {episode.mean_hr:g} bpm"
            )
        print("  these are synthetic and labelled as such in dev_annotation - not real episodes")
    print("\nthis is synthetic sample data about nobody; it never leaves this machine")
    print("try: fm plugins run --all --since 30d")
    return EXIT_OK


def cmd_dev_info(args: argparse.Namespace, session: Session) -> int:
    path = session.store_path
    exists = path.exists()
    if not exists:
        payload = {"store": str(path), "exists": False}
        if args.json:
            print_json(payload)
        else:
            print(f"no store at {path}")
            print("  create one with `fm dev seed --days 30` (deterministic sample data)")
        return EXIT_OK
    store = session.read_store()
    try:
        datasets = {d: store.count(d) for d in store.datasets()}
        days = len(store.days_with_data())
    finally:
        store.close()
    payload = {"store": str(path), "exists": True, "datasets": datasets, "days_with_data": days}
    if args.json:
        print_json(payload)
        return EXIT_OK
    print(f"store: {path}")
    for dataset, count in datasets.items():
        print(f"  {dataset:<16} {count} points")
    print(f"  days with data: {days}")
    return EXIT_OK
