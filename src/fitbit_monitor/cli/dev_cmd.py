"""``fm dev`` - developer aids. Sample data, and nothing that pretends to be real.

The seed corpus is deterministic (same ``--seed`` and ``--end``, same bytes), which
is what makes it usable from the benchmark job and from plugin test suites as well
as from a terminal. It is **synthetic**: it did not come from an account or a device,
it never leaves the machine, and the abnormal episodes it can inject are labelled as
synthetic in ``dev_annotation``.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from .common import EXIT_ERROR, EXIT_OK, EXIT_USAGE, Session, print_json


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

    accuracy = dsub.add_parser(
        "accuracy",
        help="score the anomaly detector against the labelled fixtures, as a confusion matrix",
    )
    accuracy.add_argument(
        "--fixture",
        default=None,
        help="labelled fixture to score against (default: the checked-in one)",
    )
    accuracy.add_argument(
        "--wearers",
        type=int,
        default=0,
        help="score only the first N wearers in the fixture (0 = all of them)",
    )
    accuracy.add_argument("--json", action="store_true")
    accuracy.set_defaults(func=cmd_dev_accuracy)


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


#: The checked-in fixture, when this module is running from a source checkout. A packaging
#: layout without a ``tests/`` tree is not an error - it just means ``--fixture`` is required.
DEFAULT_FIXTURE = (
    Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "anomaly" / "hr_anomaly_labels.json"
)


def cmd_dev_accuracy(args: argparse.Namespace, session: Session) -> int:
    """The accuracy gate, printed: labelled fixtures in, confusion matrix and floors out.

    This is the same scoring ``tests/test_anomaly_accuracy_gate.py`` does on every CI run,
    for a human who wants to see the matrix or wants it in a script. It seeds the fixture's
    corpora into a temporary store - never the caller's - so it cannot disturb real data, and
    it exits non-zero when a stated floor is missed.
    """
    import tempfile
    from datetime import timedelta

    from ..anomaly.accuracy import SHIPPED_FLOORS, LabelledSuite, format_report, score_suite
    from ..anomaly.engine import detect

    fixture = Path(args.fixture).expanduser() if args.fixture else DEFAULT_FIXTURE
    if not fixture.exists():
        print(
            f"error: no labelled fixture at {fixture}\n"
            "       pass --fixture PATH, or record one with scripts/dev/record_accuracy_fixtures.py",
            file=sys.stderr,
        )
        return EXIT_USAGE
    suite = LabelledSuite.load(fixture)
    if args.wearers:
        suite = LabelledSuite(
            corpora=suite.corpora[: int(args.wearers)], tolerance=suite.tolerance, detail=suite.detail
        )
    results: dict = {}
    with tempfile.TemporaryDirectory(prefix="fm-accuracy-") as tmp:
        root = Path(tmp)
        built = {corpus.wearer: corpus.build_store(root) for corpus in suite.corpora}
        try:
            for name, corpus in built.items():
                if corpus.plan is None:
                    print(f"error: corpus {name} has no generation plan to scope the run by", file=sys.stderr)
                    return EXIT_USAGE
                plan = corpus.plan
                since = datetime(plan.start.year, plan.start.month, plan.start.day, tzinfo=UTC)
                until = datetime(plan.end.year, plan.end.month, plan.end.day, tzinfo=UTC) + timedelta(days=1)
                results[name] = detect(corpus.store, since, until)
        finally:
            for corpus in built.values():
                corpus.close()
    report = score_suite(suite, results)
    failures = SHIPPED_FLOORS.failures(report)
    if args.json:
        print_json({**report.as_dict(), "floors_failed": failures, "fixture": str(fixture)})
    else:
        print(format_report(report))
        print(
            f"\n{len(suite.corpora)} wearer(s), {len(report.outcomes)} labels, fixture {fixture}"
        )
        if failures:
            print("accuracy gate: FAILED")
            for line in failures:
                print(f"  - {line}")
        else:
            print("accuracy gate: passed")
    return EXIT_ERROR if failures else EXIT_OK
