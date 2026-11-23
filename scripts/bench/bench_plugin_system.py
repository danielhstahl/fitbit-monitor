#!/usr/bin/env python3
"""Benchmark ingest, the anomaly scan and the plugin system, and fail on a regression
against the committed baseline.

This is what the benchmark job runs (``scripts/ci/bench.sh``); it is a plain module
with ``main(argv) -> int`` so it can be imported and tested like anything else, and so
a developer runs exactly what CI runs:

    ./scripts/ci/bench.sh --no-install                         # gate + both reports
    ./scripts/dev/bench.sh --days 30                           # quick look, no gate
    ./scripts/dev/bench.sh --update-baseline                   # re-record, then git add it
    python scripts/bench/bench_plugin_system.py --json          # same document to stdout

Measured, in the order a real run pays for them:

=====================================================================================
case                 what                                                     reading
=====================================================================================
seed_corpus          writing the corpus (the generator + the writer)         rows/sec
ingest_intraday      a quarter of intraday HR through the ingest pipeline    rows/sec
scan_intraday        the read API iterating every stored point             points/sec
downsample_to_days   bucketing every intraday point by day                 points/sec
hr_daily_features    the heart-rate feature library over the corpus          days/sec
anomaly_detect       the anomaly engine over the whole corpus                days/sec
hr_anomaly_scan      the shipped hr-anomaly plugin over the whole corpus     days/sec
plugin_resting_trend the other shipped plugin over the whole corpus       ms  (lower)
discover_plugins     registry discovery, validation and error isolation   ms  (lower)
=====================================================================================

Every case carries a *budget* as well as a baseline. The two answers are different and
both are needed: the baseline says "this is what the last accepted commit cost, and a
15% slip is a regression", while the budget says what a human is allowed to wait for on
the reference runner - `a full 90-day scan under 10 s`, which is 9 days/sec, and holds
whether or not the last commit was the one that used up the headroom. Budgets are
documented in ``docs/ci.md``, declared once per case in ``BUDGETS`` here, recorded in
the baseline, printed in both reports, and pinned by
``tests/test_ci_bench_gate.py`` (which also fails if a recorded measurement breaks a
documented budget). A breach is *reported* - ``budget_met: false`` in the JSON, a ``✗`` in
the summary table, a ``BUDGET`` line on the console - and is never a second gate, because
a machine slower than the reference runner should not turn a reference promise into a red
build without a human reading it first.

Peak resident set is measured per case where the platform allows it (see
``MemoryProbe``): ``peak_rss_mib`` is the high-water mark while that case ran and
``peak_rss_delta_mib`` is what the case added on top of where it started, which is the
one that catches "the scan now materialises every reading". Where ``/proc`` and
``ru_maxrss`` both fail the reading is ``null`` and the report says so.

Each case runs ``--repeats`` rounds; a sub-millisecond case takes ``Case.sample_factor``
samples per round, because a best-of-nine over a 1 ms reading is noise. The multiplier is
fixed per case, never adaptive, so the best sample is always compared with a best sample
taken over the same number of tries.

The *gate* compares the **best** sample with the
baseline (lowest ms, highest throughput - see ``Case.value``): that is the statistic
the committed baseline was recorded with, it is the one that survives a busy shared
runner, and the tolerance is ``--tolerance`` percent (default 15 - enough to shrug off
a noisy neighbour and still catch an accidental O(n²) or a per-point re-query). Every
sample is kept, so the reports also carry ``mean``, ``stddev`` and ``iterations`` per
case: the best sample says how fast the code can be, those say how much you can trust
it. ADR-0001 §9.

Two reports are written when asked for (``--json-out`` / ``--markdown-out``, and
``scripts/ci/bench.sh`` asks for both):

``bench-results.json``
    one entry per case with ``case``, ``mean``, ``stddev``, ``iterations``, the value
    that gated, the baseline behind it, the delta and a status, plus an ``environment``
    summary (interpreter, platform, CPU, memory, git commit, runner) and the run's
    configuration. Machine-readable: this is the artifact a perf regression is
    investigated from.
``bench-summary.md``
    the same numbers as a table, for a human - the workflow posts it as the PR comment
    and appends it to the job summary, without one line of arithmetic in the YAML.

The baseline is only ever written on purpose: ``--update-baseline`` is never passed by
CI, so no run can quietly re-baseline itself out of its own failure (docs/ci.md).
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

try:  # not portable: Windows has no ru_maxrss, and the probe falls back to saying nothing
    import resource
except ImportError:  # pragma: no cover - the platform without it is not this one
    resource = None  # type: ignore[assignment]

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "src") not in sys.path:  # runnable from a bare checkout
    sys.path.insert(0, str(REPO_ROOT / "src"))

from fitbit_monitor.plugins.registry import PluginRegistry  # noqa: E402
from fitbit_monitor.plugins.runner import run_plugins  # noqa: E402
from fitbit_monitor.plugins.sinks import AlertDispatcher  # noqa: E402
from fitbit_monitor.config import AppConfig  # noqa: E402
from fitbit_monitor.hr import FeatureOptions, daily_features  # noqa: E402
from fitbit_monitor.ingest.pipeline import Ingest  # noqa: E402
from fitbit_monitor.store.downsample import downsample  # noqa: E402
from fitbit_monitor.store.reader import ReadOnlyStore  # noqa: E402
from fitbit_monitor.anomaly import AnomalyConfig, detect  # noqa: E402
from fitbit_monitor.store.seed import seed_store  # noqa: E402
from fitbit_monitor.store.writer import SeriesStore  # noqa: E402
from fitbit_monitor.timeutil import DateRange  # noqa: E402

DEFAULT_BASELINE = REPO_ROOT / "benchmarks" / "baseline.json"
DEFAULT_TOLERANCE = 15.0
#: Nine samples, not five: the cheap cases are sub-millisecond, and a best-of-N over
#: three samples of a 1 ms measurement is noise. The corpus cases dominate the wall
#: clock either way, so the extra samples are free where they matter least and
#: decisive where they matter most.
DEFAULT_REPEATS = 9
DEFAULT_RESULTS = Path("bench-results.json")
DEFAULT_SUMMARY = Path("bench-summary.md")
RESULTS_SCHEMA = 1
END_DAY = date(2026, 5, 31)
#: The reference runner the budgets below are written for. Not a claim about every
#: machine - a laptop with a fan and a shared runner differ - which is why a budget
#: breach is reported rather than gated, and why the baseline carries the environment
#: that produced it.
REFERENCE_RUNNER = "the benchmark job's ubuntu-latest runner, CPython 3.14"
#: The alert plugin timed end to end, spelled once (here, in ``build_cases`` and in docs).
HR_ANOMALY_PLUGIN = "hr-anomaly"


@dataclass(frozen=True)
class Budget:
    """What one case is allowed to cost on the reference runner.

    ``limit`` is in the case's own unit and reads the way the case does: a floor for a
    throughput case (``better == "higher"``), a ceiling for a millisecond one. ``note``
    is the same number as the thing a person waits for, because "9 days/sec" is not a
    budget anyone would write down by hand - "a full 90-day scan under 10 s" is.
    """

    limit: float
    note: str


#: One documented budget per case, and the sentence that makes it a budget. This is the
#: code half of the table in ``docs/ci.md``; ``tests/test_ci_bench_gate.py`` fails when
#: the two disagree, when a case is missing here, or when a re-recorded baseline measures
#: past one of these. Every limit is deliberately far below the current measurement - a
#: budget is a promise to a user, and the 15% baseline gate is the tight ring that
#: catches a bad commit long before a budget is anywhere near being spent.
BUDGETS: dict[str, Budget] = {
    # ~124k rows/sec measured: a quarter of a year of data must not take a minute.
    "seed_corpus": Budget(50_000.0, "writing a 90-day corpus finishes inside 10 s"),
    # ~119k rows/sec measured: a quarter of intraday heart rate through the ingest
    # pipeline (validation, one range query for duplicates, upsert) in under 10 s.
    "ingest_intraday": Budget(
        50_000.0, "ingesting 90 days of intraday heart rate takes under 10 s"
    ),
    # ~546k points/sec measured: reading a quarter of intraday points back in well under
    # a second, so a plugin's first read is never the slow part of its run.
    "scan_intraday": Budget(100_000.0, "scanning 90 days of intraday points takes under 1 s"),
    # ~411k points/sec measured: the daily rollup of a quarter in well under a second.
    "downsample_to_days": Budget(
        100_000.0, "downsampling 90 days of intraday points takes under 1 s"
    ),
    # ~206 days/sec measured: the feature pass a detection plugin pays for per run.
    "hr_daily_features": Budget(50.0, "a 90-day feature pass takes under 2 s"),
    # ~66 days/sec measured, and the reference case: the 90-day scan under 10 s. The
    # engine scans a wider range than it reports on, so this includes the warm-up read.
    "anomaly_detect": Budget(9.0, "a full 90-day anomaly scan finishes under 10 s"),
    # ~64 days/sec measured end to end: engine, alert prose and metrics included, which
    # is what `fm plugins run hr-anomaly --since 90d` actually costs.
    "hr_anomaly_scan": Budget(9.0, "a 90-day hr-anomaly run finishes under 10 s, alerts included"),
    # ~6 ms measured: a trend plugin is a rounding error in a cron job, not a wait.
    "plugin_resting_trend": Budget(500.0, "a 90-day resting-trend run takes under half a second"),
    # ~1.1 ms measured: discovery runs at the start of every command, so it is only
    # invisible while it stays around the cost of opening the store.
    "discover_plugins": Budget(50.0, "plugin discovery stays under a twentieth of a second"),
}


@dataclass
class Case:
    """One benchmark: a name, a callable returning a reading, and which way is good."""

    name: str
    what: str
    unit: str
    fn: Callable[[], float]
    better: str = "higher"  # "higher" = throughput, "lower" = milliseconds
    #: Extra samples for a case too cheap to measure once. A 1 ms reading needs a couple
    #: of hundred samples before "best of N" means anything, and the multiplier is fixed
    #: per case rather than adaptive: the sample count has to be the same when the
    #: baseline is recorded and when it is gated, or best-of-N is compared against
    #: best-of-M and the machine's speed changes the bias.
    sample_factor: int = 1
    samples: list[float] = field(default_factory=list)
    #: The documented budget for this case, from ``BUDGETS``. Reported next to the
    #: measurement in both reports; never a second gate.
    budget: Budget | None = None
    #: Peak resident set while this case ran, in MiB - ``None`` where the platform cannot
    #: say (see ``MemoryProbe``). ``peak_rss_delta_mib`` is the diagnostic one: what this
    #: case put on top of the memory it started with.
    peak_rss_mib: float | None = None
    peak_rss_delta_mib: float | None = None
    #: ``proc_self_statm`` (sampled), ``rusage_maxrss`` (a high-water mark, so a lower
    #: bound for one case) or ``unmeasured``.
    rss_method: str = "unmeasured"

    @property
    def within_budget(self) -> bool | None:
        """Did the measurement keep the documented promise? ``None`` if there is no budget."""
        if self.budget is None or not self.samples:
            return None
        if self.better == "higher":
            return self.value >= self.budget.limit
        return self.value <= self.budget.limit

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

    @property
    def worst(self) -> float:
        """The other end of the spread - how bad a run got."""
        if not self.samples:
            return 0.0
        return max(self.samples) if self.better == "lower" else min(self.samples)

    @property
    def mean(self) -> float:
        return statistics.fmean(self.samples) if self.samples else 0.0

    @property
    def stddev(self) -> float:
        """Sample standard deviation; ``0.0`` for a single sample."""
        return statistics.stdev(self.samples) if len(self.samples) > 1 else 0.0

    @property
    def iterations(self) -> int:
        return len(self.samples)

    def percent_vs(self, reference: float) -> float:
        """Signed improvement versus ``reference``, in the direction that is good."""
        if reference <= 0 or self.value <= 0:
            return 0.0
        if self.better == "higher":
            return (self.value / reference - 1.0) * 100.0
        return (reference / self.value - 1.0) * 100.0

    def regression_vs(self, reference: float) -> float:
        """How much *worse* than ``reference``, as a positive percentage."""
        return -self.percent_vs(reference)


# ---------------------------------------------------------------------------
# Peak memory
# ---------------------------------------------------------------------------
#: How often the probe reads the resident set, in seconds. Two milliseconds is far below
#: the shortest thing here worth a memory number (a corpus case is hundreds of
#: milliseconds) and far above the cost of one ``/proc`` read, so the reading is useful
#: without the sampler being what is being measured.
RSS_SAMPLE_SECONDS = 0.002


def _page_bytes() -> int:
    try:
        page = os.sysconf("SC_PAGE_SIZE")
    except (ValueError, OSError, AttributeError):
        return 4096
    return int(page) if page and int(page) > 0 else 4096


def _rss_bytes() -> tuple[int | None, str]:
    """This process's resident set in bytes, and which way of getting it answered.

    ``proc_self_statm`` is the current resident set, which is what a sample wants.
    ``rusage_maxrss`` is a process-wide high-water mark instead: honest, but it only ever
    grows, so per-case it is a floor rather than a reading. ``(None, "unmeasured")`` is
    the answer on a platform with neither, and the reports print nothing rather than
    an invented zero.
    """
    try:
        fields = Path("/proc/self/statm").read_text().split()
        resident = int(fields[1]) * _page_bytes()
    except (OSError, ValueError, IndexError):
        resident = -1
    if resident > 0:
        return resident, "proc_self_statm"
    if resource is not None:
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if peak:
            # Linux and the BSDs report kibibytes, macOS bytes - the same field, two
            # units, and getting it backwards reads as a 1024x memory regression.
            return int(peak) * (1 if sys.platform == "darwin" else 1024), "rusage_maxrss"
    return None, "unmeasured"


class MemoryProbe:
    """Peak resident set of this process while one case runs, sampled, not guessed.

    A separate sample of "memory the scan used" always measures the wrong moment, so the
    probe runs on a daemon thread alongside the case and keeps the highest reading it
    sees. It is deliberately best-effort: no platform guarantee is being made here, so
    where nothing can be read the result is ``None`` with the reason in ``method``, and
    the reports say "not measurable here" instead of printing a number that means nothing.

    ``delta_mib`` - peak minus where the case started - is the number that answers "did
    this commit make the scan hold the whole corpus in memory?"; ``peak_mib`` is the raw
    high-water mark, which is the one a container's memory limit is compared against.
    """

    def __init__(self, interval: float = RSS_SAMPLE_SECONDS) -> None:
        self.interval = interval
        self.method = "unmeasured"
        self.peak_bytes: int | None = None
        self.start_bytes: int | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def __enter__(self) -> MemoryProbe:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def start(self) -> None:
        before, method = _rss_bytes()
        self.method = method
        self.start_bytes = before
        if method == "unmeasured":
            return
        self.peak_bytes = before
        # A high-water mark needs no sampling - it is already the peak. Only the live
        # reading is worth a thread, which keeps the probe free for the sub-millisecond
        # cases it must not slow down.
        if method == "proc_self_statm":
            self._thread = threading.Thread(target=self._run, name="fm-rss-probe", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        peak = self.peak_bytes or 0
        while not self._stop.wait(self.interval):
            value, _ = _rss_bytes()
            if value is not None and value > peak:
                peak = value
        self.peak_bytes = peak or self.peak_bytes

    def stop(self) -> None:
        if self._thread is not None:
            self._stop.set()
            self._thread.join(timeout=2.0)
            self._thread = None
        if self.method == "rusage_maxrss":
            value, _ = _rss_bytes()
            if value is not None:
                self.peak_bytes = max(value, self.peak_bytes or 0)

    @staticmethod
    def _mib(value: int | None) -> float | None:
        return None if value is None else round(value / 1024**2, 2)

    @property
    def peak_mib(self) -> float | None:
        return self._mib(self.peak_bytes)

    @property
    def delta_mib(self) -> float | None:
        if self.peak_bytes is None or self.start_bytes is None:
            return None
        return self._mib(max(0, self.peak_bytes - self.start_bytes))

    def apply(self, case: Case) -> None:
        """Record what this probe saw onto ``case``."""
        case.rss_method = self.method
        case.peak_rss_mib = self.peak_mib
        case.peak_rss_delta_mib = self.delta_mib

    @staticmethod
    def process_peak_mib() -> float | None:
        """The whole run's high-water mark, from the kernel rather than from the samples."""
        if resource is None:
            return None
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if not peak:
            return None
        return MemoryProbe._mib(int(peak) * (1 if sys.platform == "darwin" else 1024))


def corpus(days: int, directory: Path) -> tuple[Path, DateRange]:
    """Write the synthetic corpus once so every case reads the same data."""
    path = directory / "bench-store.sqlite3"
    with SeriesStore(path) as store:
        seed_store(store, days=days, seed=1337, end=END_DAY, drop_missing_days=True)
    start = datetime(END_DAY.year, END_DAY.month, END_DAY.day, tzinfo=UTC) - timedelta(days=days - 1)
    return path, DateRange(since=start, until=start + timedelta(days=days + 1))


def _days_analysed(report: Any) -> int | None:
    """How many days the run says it analysed, when it says.

    The plugin publishes the engine's own count in its findings metric; a run that
    declined for lack of coverage does not, and the caller falls back to the window.
    """
    for metric in getattr(report, "metrics", ()) or ():
        scanned = (getattr(metric, "details", None) or {}).get("scanned") or {}
        days = scanned.get("days") if isinstance(scanned, dict) else None
        if isinstance(days, int) and days > 0:
            return days
    return None


def build_cases(store_path: Path, window: DateRange, days: int) -> list[Case]:
    store = ReadOnlyStore(store_path)
    config = AppConfig.load(missing_ok=True)  # no file: defaults all the way down
    registry = PluginRegistry.discover(config)
    total_points = store.count("heart_rate", window.since, window.until)
    #: Read once, outside every timer. The ingest case is about *writing*: paying for a
    #: read inside it would time the read API twice and call the total ingest.
    intraday_rows: list[tuple[datetime, dict[str, float]]] = [
        (point.ts, dict(point.values))
        for point in store.query("heart_rate", window.since, window.until)
    ]
    #: A dispatcher with no sinks and no history. A benchmark that alerted for real would
    #: write to somebody's alert store, and the cost of delivering an alert is the sink's
    #: cost, not the scan's.
    quiet_dispatcher = AlertDispatcher()

    def seed_throughput() -> float:
        target = store_path.with_name(f"bench-write-{days}.sqlite3")
        started = time.perf_counter()
        with SeriesStore(target) as writer:
            plan = seed_store(writer, days=days, seed=1337, end=END_DAY, drop_missing_days=True)
        elapsed = time.perf_counter() - started
        target.unlink(missing_ok=True)
        return plan.points / elapsed

    def ingest_intraday() -> float:
        """A quarter of intraday heart rate through the ingest pipeline, as rows/second.

        This is the write path a real import pays for - every row validated, one range
        query answering "have I got this instant already?" instead of one SELECT per row,
        then an upsert per row - which the generator case above does not cover: seeding
        writes known-good rows and never asks whether the store already has them. A
        per-row existence check, a per-row commit or a validation that re-parses the
        series catalog would show up here as a cliff, and this is the case that says
        "writing a year of intraday data stayed a rounding error".

        Measured into an empty store, because that is the quantity a first import pays;
        what is *not* in here is storing the raw API payload, which is the payload's
        bytes and not the row count.
        """
        target = store_path.with_name(f"bench-ingest-{days}.sqlite3")
        target.unlink(missing_ok=True)
        started = time.perf_counter()
        with SeriesStore(target) as writer:
            ingest = Ingest(writer, source="benchmark")
            stats = ingest.rows("heart_rate", intraday_rows)
            ingest.commit()
        elapsed = time.perf_counter() - started
        target.unlink(missing_ok=True)
        written = stats.rows_inserted + stats.rows_duplicate
        if stats.payloads_quarantined or written < len(intraday_rows):
            raise RuntimeError(
                f"ingest wrote {written} of {len(intraday_rows)} rows "
                f"({stats.payloads_quarantined} quarantined): the case is no longer "
                "measuring a full ingest"
            )
        return written / elapsed

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

    def hr_features() -> float:
        """Every per-day heart-rate feature over the corpus, as days/second.

        This is the case that says "the feature library fits the Epic 6 budget": the
        detection plugin will ask for these rows on every run, so a nine-second feature
        pass would dominate everything else in this file. One store read plus one pass per
        day is the property being asserted - a per-day query version of the same work would
        show up here as a cliff.
        """
        started = time.perf_counter()
        rows = daily_features(store, window.since, window.until, options=FeatureOptions(tz=UTC))
        elapsed = time.perf_counter() - started
        if not rows or not any(row.usable for row in rows):
            raise RuntimeError("heart-rate features produced no usable day")
        return len(rows) / elapsed

    def anomaly_detect() -> float:
        """Everything the anomaly engine finds in the corpus, as days/second.

        The detection plugin pays for one of these per run, and it is the only case here
        doing real numerical work per window - contexts, baselines, scatter, evidence - so
        this is the case that says the engine fits inside a monitoring cycle. The warm-up
        read is included on purpose: the engine scans a wider range than it reports on, and
        a benchmark that hid that would be measuring something no caller ever pays.
        """
        started = time.perf_counter()
        result = detect(store, window.since, window.until, config=AnomalyConfig())
        elapsed = time.perf_counter() - started
        if result.days_scanned < days // 2:
            raise RuntimeError(f"the engine scanned {result.days_scanned} of {days} days")
        return result.days_scanned / elapsed

    def plugin_run() -> float:
        started = time.perf_counter()
        report = run_plugins(registry, store, window, config=config, names=["resting_trend"])
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        if report.exit_code != 0 or not report.metrics:
            raise RuntimeError("benchmark plugin run produced no metrics")
        return elapsed_ms

    def hr_anomaly_scan() -> float:
        """The shipped ``hr-anomaly`` plugin over the corpus, as days/second.

        ``anomaly_detect`` times the engine; this times what a person waits for when they
        run ``fm plugins run hr-anomaly --since 90d``: the same engine pass plus the
        coverage gate, the alerting policy, the prose on every alert and the metrics the
        run reports. It is the case that catches a plugin re-reading the store it was
        handed, or building alert text it then decides not to send.

        Days *analysed* over wall-clock seconds, with the plugin's own count used when it
        reports one, so the reading means the same thing as the engine case. Nothing is
        delivered: see ``quiet_dispatcher``.
        """
        started = time.perf_counter()
        report = run_plugins(
            registry,
            store,
            window,
            config=config,
            names=[HR_ANOMALY_PLUGIN],
            dispatcher=quiet_dispatcher,
        )
        elapsed = time.perf_counter() - started
        outcome = next((o for o in report.outcomes if o.name == HR_ANOMALY_PLUGIN), None)
        if outcome is None or outcome.error is not None:
            why = outcome.error if outcome else "not selected"
            raise RuntimeError(f"{HR_ANOMALY_PLUGIN} did not run: {why}")
        if not report.metrics:
            raise RuntimeError(f"{HR_ANOMALY_PLUGIN} produced no metrics to check the run against")
        scanned = _days_analysed(report) or days
        if scanned < days // 2:
            raise RuntimeError(f"{HR_ANOMALY_PLUGIN} analysed {scanned} of {days} days")
        return scanned / elapsed

    def discover() -> float:
        started = time.perf_counter()
        registry = PluginRegistry.discover(config)
        if not registry.usable():
            raise RuntimeError("discovery found no usable plugin")
        return (time.perf_counter() - started) * 1000.0

    cases = [
        Case("seed_corpus", "write the synthetic corpus", "rows/sec", seed_throughput),
        Case(
            "ingest_intraday",
            "a quarter of intraday heart rate through the ingest pipeline",
            "rows/sec",
            ingest_intraday,
        ),
        Case("scan_intraday", "read API over every stored point", "points/sec", scan),
        Case("downsample_to_days", "bucket every intraday point by day", "points/sec", to_days),
        Case("hr_daily_features", "heart-rate feature library over the corpus", "days/sec", hr_features),
        Case(
            "anomaly_detect",
            "anomaly engine over the corpus (baselines, rules, evidence)",
            "days/sec",
            anomaly_detect,
        ),
        Case(
            "hr_anomaly_scan",
            "the hr-anomaly plugin over the corpus (engine, policy, prose, metrics)",
            "days/sec",
            hr_anomaly_scan,
        ),
        Case(
            "plugin_resting_trend", "shipped plugin over the corpus", "ms", plugin_run,
            better="lower", sample_factor=5,
        ),
        Case(
            "discover_plugins", "discovery, validation, isolation", "ms", discover,
            better="lower", sample_factor=20,
        ),
    ]
    unpriced = [c.name for c in cases if c.name not in BUDGETS]
    if unpriced:
        raise RuntimeError(
            f"no documented budget for {', '.join(unpriced)}: add one to BUDGETS here and to the "
            "budget table in docs/ci.md - a case nobody priced is a case nobody is guarding"
        )
    for case in cases:
        case.budget = BUDGETS[case.name]
    return cases


def measure(cases: list[Case], repeats: int) -> list[Case]:
    """``repeats`` rounds per case, extra samples per round for the cheap cases.

    Each case's rounds run inside one ``MemoryProbe``, so the peak resident set reported
    against a case is the one that was resident while that case ran - not a snapshot of
    wherever the process happened to be when someone thought to look.
    """
    for case in cases:
        with MemoryProbe() as probe:
            for _ in range(max(1, repeats * case.sample_factor)):
                case.samples.append(float(case.fn()))
        probe.apply(case)
    return cases


def load_baseline(path: Path) -> dict[str, float]:
    """The committed baseline as ``case -> gate value``.

    Accepts the plain ``{"cases": {...}}`` shape, the older flat
    ``{"scan_intraday": 580789.9}`` shape, and the current one (which carries
    ``stats``, ``units``, ``direction`` and an ``environment`` alongside
    ``cases`` - all of it informative, none of it compared).
    """
    if not path.exists():
        return {}
    raw = json.loads(path.read_text())
    cases = raw.get("cases", raw) or {}
    return {name: float(value) for name, value in cases.items()}


def memory_summary(cases: list[Case]) -> dict[str, Any]:
    """What memory was measured, how, and how much of it there was.

    One block for the whole run: the method every case's number came from (the first one
    that was not ``unmeasured``, since a run either has ``/proc`` or it does not), the
    highest per-case peak, and ``process_peak_rss_mib`` - the kernel's own high-water mark
    from ``ru_maxrss``, kept independently of the sampling. The two agree to within a few
    hundred KiB and the sampled one is the bigger, because a kernel refreshes its recorded
    maximum less eagerly than it accounts for a fault; where they disagree, the sampled
    reading is the one that describes a case. ``None`` everywhere means "this platform
    cannot say", which is printed as such rather than as a zero.
    """
    methods = {c.rss_method for c in cases} - {"unmeasured"}
    peaks = [c.peak_rss_mib for c in cases if c.peak_rss_mib is not None]
    return {
        "method": next(iter(methods), "unmeasured"),
        "peak_rss_mib": round(max(peaks), 2) if peaks else None,
        "process_peak_rss_mib": MemoryProbe.process_peak_mib(),
    }


def budget_breaches(cases: list[Case]) -> list[str]:
    """One line per case that cost more than its documented budget says it may.

    Reported, never gated: the budget is written for the reference runner, and a build
    on slower hardware is information for a human rather than a red X from a machine that
    was never promised to meet the reference. The suite fails if a *recorded* baseline
    breaks a budget, so the promise cannot quietly become false.
    """
    problems: list[str] = []
    for case in cases:
        if case.budget is None or case.within_budget is not False:
            continue
        comparison = "below" if case.better == "higher" else "above"
        problems.append(
            f"{case.name}: {case.value:,.1f} {case.unit} is {comparison} the documented budget of "
            f"{format_number(case.budget.limit)} {case.unit} - {case.budget.note}"
        )
    return problems


def baseline_document(cases: list[Case], days: int, tolerance: float) -> dict[str, Any]:
    """The baseline to commit: one reviewable block per case.

    ``cases`` holds the value the gate compares against; ``stats`` holds the rest of
    the measurement (mean, stddev, iterations, best, worst) so the reviewer of a
    re-record can see how noisy the recording was, ``budgets`` repeats the documented
    budget each case is expected to fit inside (the measured number and its budget
    belong in the same reviewed file), ``memory`` is the peak resident set, and
    ``environment`` says which machine produced these numbers.
    """
    return {
        "cases": {c.name: round(c.value, 3) for c in cases},
        "units": {c.name: c.unit for c in cases},
        "direction": {c.name: c.better for c in cases},
        "budgets": {c.name: c.budget.limit for c in cases if c.budget is not None},
        "budget_notes": {c.name: c.budget.note for c in cases if c.budget is not None},
        "stats": {
            c.name: {
                "mean": round(c.mean, 3),
                "stddev": round(c.stddev, 3),
                "iterations": c.iterations,
                "best": round(c.value, 3),
                "worst": round(c.worst, 3),
            }
            for c in cases
        },
        "memory": {
            **memory_summary(cases),
            "peak_rss_mib_per_case": {c.name: c.peak_rss_mib for c in cases},
            "peak_rss_delta_mib_per_case": {c.name: c.peak_rss_delta_mib for c in cases},
        },
        "recorded_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "days": days,
        "tolerance_percent": tolerance,
        "environment": environment_summary(),
    }


def _read_first_line(path: Path, marker: str) -> str | None:
    """First ``KEY="value"`` (or ``KEY value``) line for ``marker`` in a text file."""
    try:
        for line in path.read_text().splitlines():
            if line.startswith(marker):
                value = line.partition("=")[2] or line.partition(":")[2]
                return value.strip().strip('"') or None
    except (OSError, UnicodeDecodeError):
        return None
    return None


def _memory_gib() -> float | None:
    """Physical memory in GiB, or ``None`` where sysconf cannot say."""
    try:
        page = os.sysconf("SC_PAGE_SIZE")
        pages = os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):
        return None
    if page is None or pages is None or page <= 0 or pages <= 0:
        return None
    return round(page * pages / 1024**3, 1)


def _git(*args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, timeout=10, check=True
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def environment_summary() -> dict[str, Any]:
    """What the machine that made these numbers was - the honesty part of a benchmark.

    A perf number without its environment is a rumour: the same commit reads 2× apart
    on a laptop and a busy runner, so both reports say which machine they came from
    (interpreter, OS, CPU, memory, git commit, runner environment).
    """
    summary: dict[str, Any] = {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "platform": platform.platform(),
        "hostname": platform.node(),
        "cpu_count": os.cpu_count(),
        "memory_gib": _memory_gib(),
    }
    cpu_model = _read_first_line(Path("/proc/cpuinfo"), "model name")
    if cpu_model:
        summary["cpu_model"] = cpu_model
    os_release = _read_first_line(Path("/etc/os-release"), "PRETTY_NAME")
    if os_release:
        summary["os_release"] = os_release
    runner = {
        key: os.environ[key]
        for key in ("CI", "RUNNER_OS", "RUNNER_ARCH", "RUNNER_INSTANCE_TYPE")
        if os.environ.get(key)
    }
    if runner:
        summary["runner"] = runner
    commit = _git("rev-parse", "HEAD")
    if commit:
        summary["git"] = {
            "commit": commit,
            "short": _git("rev-parse", "--short", "HEAD") or commit[:7],
            "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
            "dirty": bool(_git("status", "--porcelain")),
        }
    return summary


def display_path(path: Path) -> str:
    """``path`` as a human saw it: relative to the repository when it lives inside it."""
    try:
        return str(Path(path).resolve().relative_to(REPO_ROOT))
    except (ValueError, OSError):
        return str(path)


def case_rows(
    cases: list[Case], baseline: dict[str, float], tolerance: float, *, gated: bool
) -> list[dict[str, Any]]:
    """One report row per case, with the comparison already made.

    ``status`` is ``ok``, ``regressed`` (past ``tolerance``, and the gate was live),
    ``over_tolerance`` (past it on a report-only run, which is information rather than
    a verdict) or ``untracked`` (this run measured a case the baseline does not name,
    so there is nothing to compare it with).

    The budget is its own column because it answers its own question: ``status`` asks
    "did this commit slip away from the last accepted one?", ``budget_met`` asks "does it
    still cost what the documentation promises a person". ``peak_rss_mib`` is what was
    resident while that case ran - ``null``, with ``rss_method`` saying so, on a platform
    that cannot say.
    """
    rows: list[dict[str, Any]] = []
    for case in cases:
        reference = baseline.get(case.name)
        tracked = reference is not None and reference > 0
        regression = case.regression_vs(reference) if tracked else 0.0
        past = tracked and regression > tolerance
        status = (
            "untracked" if not tracked else ("regressed" if past and gated else "ok")
        )
        if past and not gated:
            status = "over_tolerance"
        rows.append(
            {
                "case": case.name,
                "what": case.what,
                "unit": case.unit,
                "direction": case.better,
                "iterations": case.iterations,
                "mean": round(case.mean, 3),
                "stddev": round(case.stddev, 3),
                "best": round(case.value, 3),
                "worst": round(case.worst, 3),
                "samples": [round(float(sample), 3) for sample in case.samples],
                "gate_value": round(case.value, 3),
                "baseline": round(reference, 3) if tracked else None,
                "delta_percent": round(case.percent_vs(reference), 2) if tracked else None,
                "regression_percent": round(regression, 2) if tracked else None,
                "tracked": tracked,
                "status": status,
                "budget": case.budget.limit if case.budget else None,
                "budget_note": case.budget.note if case.budget else None,
                "budget_met": case.within_budget,
                "peak_rss_mib": case.peak_rss_mib,
                "peak_rss_delta_mib": case.peak_rss_delta_mib,
                "rss_method": case.rss_method,
            }
        )
    return rows


def compare(cases: list[Case], baseline: dict[str, float], tolerance: float) -> list[str]:
    """One line per regressed case; an empty list means the gate passes."""
    problems: list[str] = []
    for case in cases:
        reference = baseline.get(case.name)
        if reference is None or reference <= 0:
            continue
        regression = case.regression_vs(reference)
        if regression > tolerance:
            problems.append(
                f"{case.name}: {case.value:,.1f} {case.unit} is {regression:.1f}% worse than the "
                f"baseline {reference:,.1f} (tolerance {tolerance:.0f}%)"
            )
    return problems


def results_document(
    cases: list[Case],
    baseline: dict[str, float],
    *,
    days: int,
    repeats: int,
    tolerance: float,
    baseline_path: Path,
    problems: list[str],
    gated: bool,
    skipped_reason: str | None,
    seconds: float,
    notes: list[str] | None = None,
) -> dict[str, Any]:
    """The whole ``bench-results.json`` document, in one place.

    Two verdicts are kept apart on purpose. ``gate`` is the one that can fail a build: a
    tracked case more than ``tolerance`` away from the committed baseline. ``budgets`` is
    the documented-cost contract, reported and never gated, because it was written for the
    reference runner rather than for whichever machine happened to be free. ``memory``
    says what was resident and how well that could be measured.
    """
    rows = case_rows(cases, baseline, tolerance, gated=gated)
    regressed = [row for row in rows if row["status"] == "regressed"]
    breaches = budget_breaches(cases)
    return {
        "schema": RESULTS_SCHEMA,
        "generated_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "harness": "scripts/bench/bench_plugin_system.py",
        "duration_seconds": round(seconds, 1),
        "config": {
            "days": days,
            "repeats": repeats,
            "statistic": "best of repeats",
            "tolerance_percent": tolerance,
            "baseline": display_path(baseline_path),
            "baseline_days": None,  # filled in by main(), which is the one that read it
        },
        "environment": environment_summary(),
        "notes": list(notes or []),
        "memory": memory_summary(cases),
        "cases": rows,
        "regressions": problems,
        "budgets": {
            "documented": sum(1 for row in rows if row["budget"] is not None),
            "met": sum(1 for row in rows if row["budget_met"] is True),
            "breached": [row["case"] for row in rows if row["budget_met"] is False],
            "breaches": breaches,
            "gated": False,
            "reference_runner": REFERENCE_RUNNER,
        },
        "gate": {
            "gated": gated,
            "passed": gated and not problems,
            "skipped_reason": skipped_reason,
            "tracked_cases": sum(1 for row in rows if row["tracked"]),
            "untracked_cases": [row["case"] for row in rows if not row["tracked"]],
            "regressed_cases": [row["case"] for row in regressed],
            "tolerance_percent": tolerance,
        },
    }


def format_number(value: float) -> str:
    """Thousands-separated, and no ``.0`` on a whole number: "50,000", not "50,000.0"."""
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.1f}"


def budget_cell(row: dict[str, Any]) -> str:
    """The budget column for one row: the documented bar, with a mark if it was missed."""
    if row.get("budget") is None:
        return "-"
    bar = f"{'≥' if row['direction'] == 'higher' else '≤'} {format_number(row['budget'])}"
    return bar if row.get("budget_met") is not False else f"{bar} ✗"


def memory_lines(document: dict[str, Any]) -> list[str]:
    """The peak-RSS block: a table where it was measurable, one sentence where it was not.

    Informative and never a gate. How much memory a case needs is a property of the
    machine as much as of the code - a container limit is a deployment fact, not a
    contract with a user - so what this section is for is the honest look at where a jump
    came from, next to the numbers that say when it happened.
    """
    memory = document.get("memory") or {}
    method = memory.get("method") or "unmeasured"
    if method == "unmeasured":
        return [
            "### Peak RSS",
            "",
            "- not measurable here: neither `/proc/self/statm` nor `ru_maxrss` answered, "
            "so no memory number is invented.",
        ]
    lines = [
        "### Peak RSS",
        "",
        f"Sampled from `{method}` while each case ran; Δ is what the case added over where "
        "it started. Measured, not gated.",
        "",
        "| case | peak RSS | Δ during case |",
        "| --- | --- | --- |",
    ]
    for row in document["cases"]:
        peak = f"{row['peak_rss_mib']:,.1f} MiB" if row.get("peak_rss_mib") is not None else "-"
        delta = (
            f"+{row['peak_rss_delta_mib']:,.1f} MiB"
            if row.get("peak_rss_delta_mib") is not None
            else "-"
        )
        lines.append(f"| `{row['case']}` | {peak} | {delta} |")
    lines.append("")
    if memory.get("peak_rss_mib") is not None:
        sentence = f"Busiest case: {memory['peak_rss_mib']:,.1f} MiB."
        if memory.get("process_peak_rss_mib") is not None:
            sentence += (
                f" `ru_maxrss` puts the kernel's own high-water mark at "
                f"{memory['process_peak_rss_mib']:,.1f} MiB for the whole run."
            )
        if method == "rusage_maxrss":
            sentence += " That mark is process-wide, so per case it is a floor, not a reading."
        lines.append(sentence)
    return lines


def render_markdown(document: dict[str, Any]) -> str:
    """The PR-comment / job-summary view of a results document.

    Generated here rather than in ``.github/workflows/bench.yml`` so the workflow
    posts *exactly* what the script wrote and holds no arithmetic of its own
    (AGENTS.md, ADR-0001 §12).
    """
    config = document["config"]
    gate = document["gate"]
    env = document["environment"]
    lines: list[str] = ["## Benchmarks", ""]

    if gate["skipped_reason"]:
        lines.append(f"**NOT GATED** - {gate['skipped_reason']}")
    elif gate["passed"]:
        lines.append(
            f"**PASS** - no case regressed more than {gate['tolerance_percent']:.0f}% "
            "against the committed baseline"
        )
    else:
        lines.append(
            f"**FAIL** - {len(gate['regressed_cases'])} case(s) regressed more than "
            f"{gate['tolerance_percent']:.0f}%: {', '.join(f'`{c}`' for c in gate['regressed_cases'])}"
        )
    lines.append("")
    budgets = document.get("budgets") or {}
    lines.append(
        f"{gate['tracked_cases']} tracked case(s) over a {config['days']}-day corpus; the best "
        f"sample of each case gates (sample counts are the iterations column), tolerance "
        f"{gate['tolerance_percent']:.0f}%, took {document['duration_seconds']:.1f}s."
    )
    if budgets.get("documented"):
        lines.append(
            f"Every case also carries a documented budget ({budgets['documented']} of them "
            "here, in `docs/ci.md`); that is the cost promised on the reference runner, "
            "reported here and never gated."
        )
    lines.append("")
    lines.append(
        "| case | value | mean ± stddev | iterations | baseline | Δ vs baseline | budget | status |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for row in document["cases"]:
        baseline = f"{row['baseline']:,.1f}" if row["baseline"] is not None else "-"
        delta = f"{row['delta_percent']:+.1f}%" if row["delta_percent"] is not None else "-"
        lines.append(
            f"| `{row['case']}` | {row['gate_value']:,.1f} {row['unit']} | "
            f"{row['mean']:,.1f} ± {row['stddev']:,.1f} | {row['iterations']} | "
            f"{baseline} | {delta} | {budget_cell(row)} | {row['status']} |"
        )
    regressions = document["regressions"]
    if regressions:
        lines += ["", "### Regressions", ""]
        lines += [f"- {problem}" for problem in regressions]
        lines += [
            "",
            "If this is a deliberate, understood cost, refresh the baseline on purpose:",
            "`./scripts/dev/bench.sh --update-baseline && git add benchmarks/baseline.json` "
            "(see `docs/ci.md`).",
        ]
    breaches = budgets.get("breaches") or []
    if breaches:
        lines += ["", "### Budget breaches", ""]
        lines += [f"- {breach}" for breach in breaches]
        lines += [
            "",
            "A budget is written for "
            f"{budgets.get('reference_runner') or 'the reference runner'} and is reported, "
            "not gated - see `docs/ci.md`.",
        ]
    untracked = gate["untracked_cases"]
    if untracked:
        lines += ["", f"Untracked (measured, not gated): {', '.join(f'`{c}`' for c in untracked)}."]
    lines += ["", *memory_lines(document)]
    git = env.get("git") or {}
    environment = (
        f"{env.get('implementation')} {env.get('python')} on {env.get('os_release') or env.get('system')} "
        f"{env.get('release')} ({env.get('machine')}, {env.get('cpu_count')} CPUs"
    )
    if env.get("cpu_model"):
        environment += f", {env['cpu_model']}"
    environment += ")"
    if git:
        dirty = ", dirty tree" if git.get("dirty") else ""
        environment += f" @ `{git.get('short')}` ({git.get('branch')}{dirty})"
    lines += ["", f"- harness: `{document['harness']}`", f"- environment: {environment}"]
    for note in document.get("notes") or []:
        lines.append(f"- note: {note}")
    lines += ["- the baseline is never refreshed automatically; see `docs/ci.md`."]
    lines.append("")
    return "\n".join(lines)


def write_document(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` and say so, for the log and the artifact name line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    print(f"wrote {path}")


def append_to_step_summary(markdown: str) -> None:
    """Add the summary to `$GITHUB_STEP_SUMMARY` when running in Actions.

    Doing it here is what lets the workflow stay free of formatting logic; a laptop has
    no such variable, so nothing happens there.
    """
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if not target:
        return
    try:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(markdown)
    except OSError as exc:  # a summary is never a reason to fail a gate
        print(f"note: could not append to GITHUB_STEP_SUMMARY ({exc})", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bench_plugin_system.py",
        description=(
            "Benchmark ingest, the anomaly scan and the plugin system against "
            "benchmarks/baseline.json."
        ),
        epilog=(
            "Exit: 0 no tracked case past the tolerance, 1 a case regressed, "
            "2 nothing to compare against (no baseline) or a corpus mismatch."
        ),
    )
    parser.add_argument("--days", type=int, default=90, help="days of synthetic data (default 90)")
    parser.add_argument(
        "--repeats",
        type=int,
        default=DEFAULT_REPEATS,
        help=f"rounds per case (default {DEFAULT_REPEATS}); a sub-millisecond case takes "
        "more samples than this, see Case.sample_factor",
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=DEFAULT_TOLERANCE,
        help=f"allowed regression per case, percent (default {DEFAULT_TOLERANCE:g}, what the gate uses)",
    )
    parser.add_argument("--baseline", default=str(DEFAULT_BASELINE), help="baseline file to compare against")
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="record these numbers as the baseline (never passed by CI; commit the diff)",
    )
    parser.add_argument("--json", action="store_true", help="print the results document as JSON")
    parser.add_argument("--json-out", default=None, metavar="PATH", help=f"write the results document (e.g. {DEFAULT_RESULTS})")
    parser.add_argument(
        "--markdown-out", default=None, metavar="PATH", help=f"write the markdown summary (e.g. {DEFAULT_SUMMARY})"
    )
    parser.add_argument(
        "--step-summary",
        action="store_true",
        help="also append the markdown to $GITHUB_STEP_SUMMARY when it is set",
    )
    parser.add_argument(
        "--artifact-name", default="bench-results-json", help="name of the artifact --json-out becomes"
    )
    parser.add_argument(
        "--markdown-artifact-name", default="bench-summary-md", help="and the one --markdown-out becomes"
    )
    parser.add_argument("--no-gate", action="store_true", help="report only; never fail")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    baseline_path = Path(args.baseline)

    # Refused before the run is paid for: with nothing to compare against, a green would
    # be a green measured against nothing.
    if not baseline_path.is_file() and not (args.update_baseline or args.no_gate):
        print(
            f"error: no baseline to compare against at {display_path(baseline_path)}\n"
            "  fix: record one on purpose with `./scripts/dev/bench.sh --update-baseline`,\n"
            "       then commit benchmarks/baseline.json; `--no-gate` reports without gating",
            file=sys.stderr,
        )
        return 2

    started = time.time()
    workdir = tempfile.TemporaryDirectory(prefix="fm-bench-")
    store_path, window = corpus(args.days, Path(workdir.name))
    cases = measure(build_cases(store_path, window, args.days), args.repeats)
    store_path.unlink(missing_ok=True)
    workdir.cleanup()

    baseline = load_baseline(baseline_path)
    try:
        header = json.loads(baseline_path.read_text()) if baseline_path.is_file() else {}
    except (OSError, ValueError):
        header = {}
    if not isinstance(header, dict):
        header = {}
    baseline_days = header.get("days")
    baseline_python = ((header.get("environment") or {}).get("python")) or None

    notes: list[str] = []
    if baseline_python:
        if tuple(baseline_python.split(".")[:2]) != tuple(platform.python_version().split(".")[:2]):
            notes.append(
                f"the baseline was recorded on {baseline_python} and this run is "
                f"{platform.python_implementation()} {platform.python_version()}; interpreters "
                "do run this code at different speeds, so read the deltas with that in mind"
            )

    gate = not (args.update_baseline or args.no_gate)
    skipped_reason: str | None = None
    if args.update_baseline:
        skipped_reason = "recording a new baseline (--update-baseline); nothing is compared"
    elif args.no_gate:
        skipped_reason = "report only (--no-gate)"
    if gate and baseline_days not in (None, args.days):
        # Throughput on a 7-day corpus and on a 90-day corpus are different quantities;
        # failing a quick look would be wrong and passing it as a comparison would be
        # worse, so the gate declines and says why, loudly. CI runs the baseline's days.
        skipped_reason = (
            f"this run used --days {args.days} but {display_path(baseline_path)} recorded "
            f"{baseline_days} days; compare like with like (see --help)"
        )
        print(f"note: not gated - {skipped_reason}", file=sys.stderr)
        gate = False
    problems = compare(cases, baseline, args.tolerance) if gate else []

    if args.update_baseline:
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(
            json.dumps(baseline_document(cases, args.days, args.tolerance), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"wrote {baseline_path}")

    document = results_document(
        cases,
        baseline,
        days=args.days,
        repeats=max(1, args.repeats),
        tolerance=args.tolerance,
        baseline_path=baseline_path,
        problems=problems,
        gated=gate,
        skipped_reason=skipped_reason,
        seconds=time.time() - started,
        notes=notes,
    )
    document["config"]["baseline_days"] = baseline_days
    payload = json.dumps(document, indent=2, sort_keys=True)

    written: list[tuple[str, str]] = []
    if args.json:
        print(payload)
    if args.json_out:
        write_document(Path(args.json_out), payload + "\n")
        written.append((args.artifact_name, args.json_out))

    markdown = render_markdown(document) if (args.markdown_out or args.step_summary) else None
    if markdown is not None:
        if args.markdown_out:
            write_document(Path(args.markdown_out), markdown)
            written.append((args.markdown_artifact_name, args.markdown_out))
        if args.step_summary:
            append_to_step_summary(markdown)

    if not args.json:
        width = max(len(c.name) for c in cases)
        print(f"plugin-system benchmarks ({args.days} days, {max(1, args.repeats)} rounds; best sample gates)\n")
        for row in document["cases"]:
            delta = f"{row['delta_percent']:+.1f}% vs baseline" if row["delta_percent"] is not None else ""
            noise = f"(mean {row['mean']:,.1f} ± {row['stddev']:,.1f}, n={row['iterations']})"
            budget = ""
            if row["budget"] is not None:
                arrow = "≥" if row["direction"] == "higher" else "≤"
                budget = f"budget {arrow} {format_number(row['budget'])}"
            rss = f"rss {row['peak_rss_mib']:,.0f} MiB" if row["peak_rss_mib"] is not None else ""
            print(
                f"  {row['case']:<{width}}  {row['gate_value']:>14,.1f} {row['unit']:<10} "
                f"{delta:<22} {noise:<44} {budget:<24} {rss}".rstrip()
            )
        print()
        for problem in problems:
            print(f"REGRESSION  {problem}")
        for breach in document["budgets"]["breaches"]:
            print(f"BUDGET      {breach}")
        if skipped_reason:
            print(f"NOT GATED   {skipped_reason}")
        elif not problems:
            print(
                f"no regressions beyond {args.tolerance:.0f}%"
                if baseline
                else "no baseline recorded yet - run with --update-baseline"
            )
        for note in notes:
            print(f"NOTE        {note}")
        memory = document["memory"]
        if memory["method"] == "unmeasured":
            print("MEMORY      peak RSS is not measurable on this platform; nothing was invented")
        else:
            print(
                f"MEMORY      {memory['peak_rss_mib']:,.1f} MiB while the busiest case ran "
                f"(sampled from {memory['method']}, reported not gated)"
            )
        print(f"\ntook {time.time() - started:.1f}s")

    # The last lines, as in the coverage gate: the names the workflow's upload steps use
    # are printed by the thing that wrote the files, so they cannot disagree.
    for artifact_name, path in written:
        print(f"artifact {artifact_name}: {path}")

    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
