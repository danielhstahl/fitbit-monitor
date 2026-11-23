#!/usr/bin/env python3
"""Benchmark the plugin system, and fail on a regression against the committed baseline.

This is what the benchmark job runs (``scripts/ci/bench.sh``); it is a plain module
with ``main(argv) -> int`` so it can be imported and tested like anything else, and so
a developer runs exactly what CI runs:

    ./scripts/ci/bench.sh --no-install                         # gate + both reports
    ./scripts/dev/bench.sh --days 30                           # quick look, no gate
    ./scripts/dev/bench.sh --update-baseline                   # re-record, then git add it
    python scripts/bench/bench_plugin_system.py --json          # same document to stdout

Measured, in the order a real run pays for them:

==================== ======================================================= ==============
case                 what                                                    reading
==================== ======================================================= ==============
seed_corpus          writing the corpus (the ingest path)                    rows/sec
scan_intraday        the read API iterating every stored point               points/sec
downsample_to_days   bucketing every intraday point by day                   points/sec
hr_daily_features    the heart-rate feature library over the whole corpus    days/sec
plugin_resting_trend the shipped plugin over the whole corpus                 ms  (lower)
discover_plugins     registry discovery, validation and error isolation       ms  (lower)
==================== ======================================================= ==============

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
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "src") not in sys.path:  # runnable from a bare checkout
    sys.path.insert(0, str(REPO_ROOT / "src"))

from fitbit_monitor.plugins.registry import PluginRegistry  # noqa: E402
from fitbit_monitor.plugins.runner import run_plugins  # noqa: E402
from fitbit_monitor.config import AppConfig  # noqa: E402
from fitbit_monitor.hr import FeatureOptions, daily_features  # noqa: E402
from fitbit_monitor.store.downsample import downsample  # noqa: E402
from fitbit_monitor.store.reader import ReadOnlyStore  # noqa: E402
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
        Case("hr_daily_features", "heart-rate feature library over the corpus", "days/sec", hr_features),
        Case(
            "plugin_resting_trend", "shipped plugin over the corpus", "ms", plugin_run,
            better="lower", sample_factor=5,
        ),
        Case(
            "discover_plugins", "discovery, validation, isolation", "ms", discover,
            better="lower", sample_factor=20,
        ),
    ]


def measure(cases: list[Case], repeats: int) -> list[Case]:
    """``repeats`` rounds per case, extra samples per round for the cheap cases."""
    for case in cases:
        for _ in range(max(1, repeats * case.sample_factor)):
            case.samples.append(float(case.fn()))
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


def baseline_document(cases: list[Case], days: int, tolerance: float) -> dict[str, Any]:
    """The baseline to commit: one reviewable block per case.

    ``cases`` holds the value the gate compares against; ``stats`` holds the rest of
    the measurement (mean, stddev, iterations, best, worst) so the reviewer of a
    re-record can see how noisy the recording was, and ``environment`` says which
    machine produced these numbers.
    """
    return {
        "cases": {c.name: round(c.value, 3) for c in cases},
        "units": {c.name: c.unit for c in cases},
        "direction": {c.name: c.better for c in cases},
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
    """The whole ``bench-results.json`` document, in one place."""
    rows = case_rows(cases, baseline, tolerance, gated=gated)
    regressed = [row for row in rows if row["status"] == "regressed"]
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
        "cases": rows,
        "regressions": problems,
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
    lines.append(
        f"{gate['tracked_cases']} tracked case(s) over a {config['days']}-day corpus; the best "
        f"sample of each case gates (sample counts are the iterations column), tolerance "
        f"{gate['tolerance_percent']:.0f}%, took {document['duration_seconds']:.1f}s."
    )
    lines.append("")
    lines.append("| case | value | mean ± stddev | iterations | baseline | Δ vs baseline | status |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for row in document["cases"]:
        baseline = f"{row['baseline']:,.1f}" if row["baseline"] is not None else "-"
        delta = f"{row['delta_percent']:+.1f}%" if row["delta_percent"] is not None else "-"
        lines.append(
            f"| `{row['case']}` | {row['gate_value']:,.1f} {row['unit']} | "
            f"{row['mean']:,.1f} ± {row['stddev']:,.1f} | {row['iterations']} | "
            f"{baseline} | {delta} | {row['status']} |"
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
    untracked = gate["untracked_cases"]
    if untracked:
        lines += ["", f"Untracked (measured, not gated): {', '.join(f'`{c}`' for c in untracked)}."]
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
        description="Benchmark the plugin system against benchmarks/baseline.json.",
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
            print(
                f"  {row['case']:<{width}}  {row['gate_value']:>14,.1f} {row['unit']:<10} "
                f"{delta:<22} {noise}".rstrip()
            )
        print()
        for problem in problems:
            print(f"REGRESSION  {problem}")
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
        print(f"\ntook {time.time() - started:.1f}s")

    # The last lines, as in the coverage gate: the names the workflow's upload steps use
    # are printed by the thing that wrote the files, so they cannot disagree.
    for artifact_name, path in written:
        print(f"artifact {artifact_name}: {path}")

    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
