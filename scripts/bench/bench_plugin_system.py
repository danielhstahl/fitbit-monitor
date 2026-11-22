#!/usr/bin/env python3
"""Benchmark the plugin system, and fail on a regression against the committed baseline.

This is what the benchmark job runs (``scripts/ci/benchmarks.sh``); it is a plain
module with ``main(argv) -> int`` so it can be imported and tested like anything
else, and so a developer runs exactly what CI runs:

    python scripts/bench/bench_plugin_system.py                    # compare to baseline
    python scripts/bench/bench_plugin_system.py --update-baseline   # re-record it
    python scripts/bench/bench_plugin_system.py --days 7 --repeats 1 --json

Measured, in the order a real run pays for them:

==================== ======================================================= ==============
case                 what                                                    reading
==================== ======================================================= ==============
seed_corpus          writing the corpus (the ingest path)                    rows/sec
scan_intraday        the read API iterating every stored point               points/sec
downsample_to_days   bucketing every intraday point by day                   points/sec
plugin_resting_trend the shipped plugin over the whole corpus                 ms  (lower)
discover_plugins     registry discovery, validation and error isolation      ms  (lower)
==================== ======================================================= ==============

Each case runs ``--repeats`` times and the *best* sample is compared with the
baseline (lowest ms, highest throughput - see ``Case.value``);
a case regressing by more than ``--tolerance`` percent fails the job. Runner clocks
vary a lot, so the tolerance is deliberately generous - this gate exists to catch an
accidental O(n²) or a per-point re-query, not a noisy neighbour.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "src") not in sys.path:  # runnable from a bare checkout
    sys.path.insert(0, str(REPO_ROOT / "src"))

from fitbit_monitor.plugins.registry import PluginRegistry  # noqa: E402
from fitbit_monitor.plugins.runner import run_plugins  # noqa: E402
from fitbit_monitor.config import AppConfig  # noqa: E402
from fitbit_monitor.store.downsample import downsample  # noqa: E402
from fitbit_monitor.store.reader import ReadOnlyStore  # noqa: E402
from fitbit_monitor.store.seed import seed_store  # noqa: E402
from fitbit_monitor.store.writer import SeriesStore  # noqa: E402
from fitbit_monitor.timeutil import DateRange  # noqa: E402

DEFAULT_BASELINE = REPO_ROOT / "benchmarks" / "baseline.json"
DEFAULT_TOLERANCE = 30.0
END_DAY = date(2026, 5, 31)


@dataclass
class Case:
    """One benchmark: a name, a callable returning a reading, and which way is good."""

    name: str
    what: str
    unit: str
    fn: Callable[[], float]
    better: str = "higher"  # "higher" = throughput, "lower" = milliseconds
    samples: list[float] = field(default_factory=list)

    @property
    def value(self) -> float:
        """Best sample, not the median: the convention for timing benchmarks.

        ``min`` for milliseconds (the cleanest run is the honest one; the rest were
        interrupted by something else), ``max`` for throughput derived from a
        stopwatch. Best-of-N is also what keeps a shared runner from failing the
        gate because it was busy.
        """
        if not self.samples:
            return 0.0
        return min(self.samples) if self.better == "lower" else max(self.samples)

    def percent_vs(self, reference: float) -> float:
        """Signed improvement versus ``reference``, in the direction that is good."""
        if reference <= 0 or self.value <= 0:
            return 0.0
        if self.better == "higher":
            return (self.value / reference - 1.0) * 100.0
        return (reference / self.value - 1.0) * 100.0


def corpus(days: int, directory: Path) -> tuple[Path, DateRange]:
    """Write the synthetic corpus once so every case reads the same data."""
    path = directory / "bench-store.sqlite3"
    with SeriesStore(path) as store:
        seed_store(store, days=days, seed=1337, end=END_DAY, drop_missing_days=True)
    start = datetime(END_DAY.year, END_DAY.month, END_DAY.day, tzinfo=UTC) - timedelta(days=days - 1)
    return path, DateRange(since=start, until=start + timedelta(days=days + 1))


def build_cases(store_path: Path, window: DateRange, days: int) -> list[Case]:
    store = ReadOnlyStore(store_path)
    config = AppConfig.load(missing_ok=True)  # no file: defaults all the way down
    registry = PluginRegistry.discover(config)
    total_points = store.count("heart_rate", window.since, window.until)

    def seed_throughput() -> float:
        target = store_path.with_name(f"bench-write-{days}.sqlite3")
        started = time.perf_counter()
        with SeriesStore(target) as writer:
            plan = seed_store(writer, days=days, seed=1337, end=END_DAY, drop_missing_days=True)
        elapsed = time.perf_counter() - started
        target.unlink(missing_ok=True)
        return plan.points / elapsed

    def scan() -> float:
        started = time.perf_counter()
        count = sum(1 for _ in store.query("heart_rate", window.since, window.until))
        return count / (time.perf_counter() - started)

    def to_days() -> float:
        started = time.perf_counter()
        buckets = list(downsample(store.query("heart_rate", window.since, window.until), "day"))
        elapsed = time.perf_counter() - started
        if not buckets:
            raise RuntimeError("downsample produced no buckets")
        return total_points / elapsed

    def plugin_run() -> float:
        started = time.perf_counter()
        report = run_plugins(registry, store, window, config=config, names=["resting_trend"])
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        if report.exit_code != 0 or not report.metrics:
            raise RuntimeError("benchmark plugin run produced no metrics")
        return elapsed_ms

    def discover() -> float:
        started = time.perf_counter()
        registry = PluginRegistry.discover(config)
        if not registry.usable():
            raise RuntimeError("discovery found no usable plugin")
        return (time.perf_counter() - started) * 1000.0

    return [
        Case("seed_corpus", "write the synthetic corpus", "rows/sec", seed_throughput),
        Case("scan_intraday", "read API over every stored point", "points/sec", scan),
        Case("downsample_to_days", "bucket every intraday point by day", "points/sec", to_days),
        Case("plugin_resting_trend", "shipped plugin over the corpus", "ms", plugin_run, better="lower"),
        Case("discover_plugins", "discovery, validation, isolation", "ms", discover, better="lower"),
    ]


def measure(cases: list[Case], repeats: int) -> list[Case]:
    for case in cases:
        for _ in range(max(1, repeats)):
            case.samples.append(float(case.fn()))
    return cases


def load_baseline(path: Path) -> dict[str, float]:
    if not path.exists():
        return {}
    raw = json.loads(path.read_text())
    cases = raw.get("cases", raw) or {}
    return {name: float(value) for name, value in cases.items()}


def compare(cases: list[Case], baseline: dict[str, float], tolerance: float) -> list[str]:
    """One line per regressed case; an empty list means the gate passes."""
    problems: list[str] = []
    for case in cases:
        reference = baseline.get(case.name)
        if reference is None or reference <= 0:
            continue
        regression = -case.percent_vs(reference)
        if regression > tolerance:
            problems.append(
                f"{case.name}: {case.value:,.1f} {case.unit} is {regression:.1f}% worse than the "
                f"baseline {reference:,.1f} (tolerance {tolerance:.0f}%)"
            )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark the plugin system against benchmarks/baseline.json."
    )
    parser.add_argument("--days", type=int, default=90, help="days of synthetic data (default 90)")
    parser.add_argument("--repeats", type=int, default=5, help="samples per case; the best is used (default 5)")
    parser.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE, help="allowed regression, percent")
    parser.add_argument("--baseline", default=str(DEFAULT_BASELINE), help="baseline file to compare against")
    parser.add_argument("--update-baseline", action="store_true", help="record these numbers as the baseline")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--no-gate", action="store_true", help="report only; never fail")
    args = parser.parse_args(argv)

    started = time.time()
    workdir = tempfile.TemporaryDirectory(prefix="fm-bench-")
    store_path, window = corpus(args.days, Path(workdir.name))
    cases = measure(build_cases(store_path, window, args.days), args.repeats)
    store_path.unlink(missing_ok=True)
    workdir.cleanup()

    baseline_path = Path(args.baseline)
    baseline = load_baseline(baseline_path)
    gate = not (args.update_baseline or args.no_gate)
    problems = compare(cases, baseline, args.tolerance) if gate else []

    if args.update_baseline:
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "cases": {c.name: round(c.value, 3) for c in cases},
            "units": {c.name: c.unit for c in cases},
            "direction": {c.name: c.better for c in cases},
            "recorded_utc": datetime.now(UTC).isoformat(timespec="seconds"),
            "days": args.days,
        }
        baseline_path.write_text(json.dumps(payload, indent=2) + "\n")
        print(f"wrote {baseline_path}")

    if args.json:
        print(
            json.dumps(
                {
                    "days": args.days,
                    "repeats": args.repeats,
                    "tolerance_percent": args.tolerance,
                    "cases": {c.name: round(c.value, 3) for c in cases},
                    "units": {c.name: c.unit for c in cases},
                    "baseline": baseline,
                    "delta_percent": {c.name: round(c.percent_vs(baseline[c.name]), 2) for c in cases if c.name in baseline},
                    "regressions": problems,
                    "seconds": round(time.time() - started, 1),
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        width = max(len(c.name) for c in cases)
        print(f"plugin-system benchmarks ({args.days} days, best of {max(1, args.repeats)})\n")
        for case in cases:
            delta = ""
            if case.name in baseline:
                delta = f"{case.percent_vs(baseline[case.name]):+.1f}% vs baseline"
            print(f"  {case.name:<{width}}  {case.value:>14,.1f} {case.unit:<10} {delta}".rstrip())
        print()
        for problem in problems:
            print(f"REGRESSION  {problem}")
        if not problems:
            print(
                f"no regressions beyond {args.tolerance:.0f}%"
                if baseline
                else "no baseline recorded yet - run with --update-baseline"
            )
        print(f"\ntook {time.time() - started:.1f}s")

    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
