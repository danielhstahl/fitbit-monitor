#!/usr/bin/env python3
"""The one combined PASS/FAIL block ``scripts/ci/post_commit.sh`` ends with.

The point of a post-commit runner is a single answer to "may I push?", and that answer
has to carry the two numbers the workflows gate on - the coverage percentage and the
benchmark regression verdict - instead of making whoever just committed scroll back
through a minute of log. This module turns whatever the runner has (the reports the
gates wrote, or, under ``act``/act-mcp, possibly only the log the tool returned) into::

    workspace    /repo  (resolved from scripts/ci/post_commit.sh)
    runner       direct (scripts/ci/coverage.sh, scripts/ci/bench.sh)
    gate         status  detail
    lint         PASS    3 passed, 0 failed, 0 skipped
    coverage     PASS    92.3% covered (floor 85.0%)
    benchmarks   PASS    no case regressed more than 15% (5 tracked cases, worst 2.3%)
    result       PASS    3 passed, 0 failed, 0 skipped

Every verdict is derived, never asserted: the runner reports only which gates ran and
whether their processes exited zero, and this file re-reads the evidence. That matters
under ``act``, where a container can exit 0 around a job that failed a floor, and it is
why a percentage printed here is a percentage that was actually measured.

Nothing is invented either. With no report and no log line to read, a row says so
(``no coverage number reported``) and the block fails rather than guessing a number.

Exit codes: ``0`` when every gate passed or was skipped, ``1`` when one failed, ``2``
on a usage error - the runner ends by ``exec``-ing this file, so this is the
post-commit exit status.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
STATUSES = (PASS, FAIL, SKIP)

#: Column widths of the block, so the status column lines up in a terminal.
NAME_WIDTH = 12
STATUS_WIDTH = 6

#: ``scripts/ci/_coverage_report.py`` owns the percent formula and the committed floors;
#: importing it here is what keeps two files from disagreeing about a coverage number.
_coverage_report_module: Any = None


def coverage_report_module() -> Any:
    global _coverage_report_module
    if _coverage_report_module is None:
        path = Path(__file__).resolve().with_name("_coverage_report.py")
        spec = importlib.util.spec_from_file_location("_coverage_report", path)
        if spec is None or spec.loader is None:  # pragma: no cover - file missing from the tree
            raise RuntimeError(f"cannot load {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _coverage_report_module = module
    return _coverage_report_module


@dataclass
class Gate:
    """One row of the block: a gate, its status, and the measured detail beside it."""

    name: str
    status: str
    detail: str = ""

    def row(self) -> str:
        return f"{self.name:<{NAME_WIDTH}} {self.status:<{STATUS_WIDTH}} {self.detail}".rstrip()


@dataclass
class Verdict:
    """One gate's measured verdict, plus where the evidence came from."""

    ok: bool | None
    detail: str
    source: str

    @property
    def note(self) -> str:
        """`` [coverage.json]`` - only when the evidence is not the obvious thing."""
        return "" if self.source in ("report", "coverage.json", "bench-results.json") else f" [{self.source}]"


# --- coverage -----------------------------------------------------------------
#: The line ``scripts/ci/_coverage_report.py`` prints and everything greps for.
COVERAGE_LINE = re.compile(r"^coverage:\s*([0-9]+(?:\.[0-9]+)?)%", re.MULTILINE)
#: ``coverage overall 92.3% (floor 85.0%) ok`` - the same file, one line later.
COVERAGE_OVERALL = re.compile(
    r"^coverage overall\s+([0-9.]+)%\s*\(floor\s+([0-9.]+)%\)\s*(ok|FAIL)", re.MULTILINE
)


def coverage_from_json(path: Path, pyproject: Path) -> Verdict:
    """The percentage and floor a ``coverage json`` report implies, via the gate's own math."""
    report = json.loads(path.read_text())
    module = coverage_report_module()
    percent = round(module._percent(report["totals"]), 1)
    overall_floor, _default, _packages = module.committed_floors(pyproject)
    ok = percent >= overall_floor
    detail = f"{percent:.1f}% covered (floor {overall_floor:.1f}%){'' if ok else ' BELOW FLOOR'}"
    return Verdict(ok=ok, detail=detail, source=path.name)


def coverage_from_log(text: str) -> Verdict | None:
    """The percentage from a job log, for when the JSON report stayed in a container."""
    overall = COVERAGE_OVERALL.search(text)
    if overall:
        percent, floor, marker = float(overall.group(1)), float(overall.group(2)), overall.group(3)
        ok = marker == "ok" or percent >= floor
        detail = f"{percent:.1f}% covered (floor {floor:.1f}%){'' if ok else ' BELOW FLOOR'}"
        return Verdict(ok=ok, detail=detail, source="job log")
    line = COVERAGE_LINE.search(text)
    if line:
        percent = float(line.group(1))
        return Verdict(ok=None, detail=f"{percent:.1f}% covered (floor not reported)", source="job log")
    return None


def coverage_verdict(json_path: Path | None, log_path: Path | None, pyproject: Path) -> Verdict:
    """The report if it exists, the log if it does not, and an honest row if neither does."""
    problems: list[str] = []
    if json_path is not None and Path(json_path).is_file():
        try:
            return coverage_from_json(Path(json_path), pyproject)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            problems.append(f"{json_path}: {exc}")
    if log_path is not None and Path(log_path).is_file():
        try:
            found = coverage_from_log(Path(log_path).read_text(errors="replace"))
        except OSError as exc:
            problems.append(f"{log_path}: {exc}")
        else:
            if found is not None:
                return found
    detail = "no coverage number reported"
    if problems:
        detail += f" ({'; '.join(problems)})"
    return Verdict(ok=None, detail=detail, source="none")


# --- benchmarks ---------------------------------------------------------------
BENCH_MD_VERDICT = re.compile(r"^\*\*(PASS|FAIL|NOT GATED)\*\*\s*-?\s*(.*)$", re.MULTILINE)
BENCH_LOG_PASS = re.compile(r"^no regressions beyond\s+([0-9.]+)%", re.MULTILINE)
BENCH_LOG_FAIL = re.compile(r"^REGRESSION\s+(.+)$", re.MULTILINE)
BENCH_LOG_SKIP = re.compile(r"^NOT GATED\s+(.+)$", re.MULTILINE)


def bench_from_json(path: Path) -> Verdict:
    """The regression verdict a ``bench-results.json`` gate block states."""
    document = json.loads(path.read_text())
    gate = document.get("gate") or {}
    tolerance = float(gate.get("tolerance_percent") or 15.0)
    if not gate.get("gated"):
        reason = gate.get("skipped_reason") or "the run declined to gate itself"
        return Verdict(ok=None, detail=f"not gated - {reason}", source=path.name)
    regressed = [str(name) for name in (gate.get("regressed_cases") or [])]
    tracked_cases = [
        case
        for case in (document.get("cases") or [])
        if case.get("tracked") and isinstance(case.get("regression_percent"), (int, float))
    ]
    tracked = gate.get("tracked_cases")
    count = f"{tracked} tracked case(s)" if tracked is not None else f"{len(tracked_cases)} tracked case(s)"
    if not regressed:
        worst = max((float(case["regression_percent"]) for case in tracked_cases), default=None)
        detail = f"no case regressed more than {tolerance:g}%"
        detail += f" ({count}" + (f", worst {worst:.1f}%" if worst is not None else "") + ")"
        return Verdict(ok=True, detail=detail, source=path.name)
    detail = f"{len(regressed)} case(s) regressed more than {tolerance:g}%: " + ", ".join(regressed)
    return Verdict(ok=False, detail=detail, source=path.name)


def bench_from_markdown(text: str) -> Verdict | None:
    """The headline of ``bench-summary.md`` - the same line a pull request is commented with."""
    match = BENCH_MD_VERDICT.search(text)
    if not match:
        return None
    marker, rest = match.group(1), match.group(2).strip()
    ok = None if marker == "NOT GATED" else marker == "PASS"
    return Verdict(ok=ok, detail=(rest or marker.lower()), source="bench-summary.md")


def bench_from_log(text: str) -> Verdict | None:
    failed = BENCH_LOG_FAIL.findall(text)
    if failed:
        return Verdict(ok=False, detail=f"{len(failed)} case(s) regressed: {failed[0]}", source="job log")
    skipped = BENCH_LOG_SKIP.search(text)
    if skipped:
        return Verdict(ok=None, detail=f"not gated - {skipped.group(1).strip()}", source="job log")
    passed = BENCH_LOG_PASS.search(text)
    if passed:
        return Verdict(ok=True, detail=f"no case regressed more than {float(passed.group(1)):g}%", source="job log")
    return None


def bench_verdict(json_path: Path | None, markdown_path: Path | None, log_path: Path | None) -> Verdict:
    """Report, then markdown, then log - the first piece of evidence that exists wins."""
    candidates = (
        (json_path, "json"),
        (markdown_path, "markdown"),
        (log_path, "log"),
    )
    for path, kind in candidates:
        if path is None or not Path(path).is_file():
            continue
        try:
            if kind == "json":
                found: Verdict | None = bench_from_json(Path(path))
            else:
                text = Path(path).read_text(errors="replace")
                found = bench_from_markdown(text) if kind == "markdown" else bench_from_log(text)
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if found is not None:
            return found
    return Verdict(ok=None, detail="no benchmark verdict reported", source="none")


# --- the block -----------------------------------------------------------------
def overall_status(gates: list[Gate]) -> str:
    """FAIL if any gate failed (or nothing ran); SKIP only when everything was skipped."""
    if not gates:
        return FAIL
    statuses = [gate.status for gate in gates]
    if FAIL in statuses:
        return FAIL
    if all(status == SKIP for status in statuses):
        return SKIP
    return PASS


def header_rows(workspace: str | None, workspace_source: str | None, runner: str | None) -> list[str]:
    rows = []
    if workspace:
        suffix = f"  (resolved from {workspace_source})" if workspace_source else ""
        rows.append(f"{'workspace':<{NAME_WIDTH}} {workspace}{suffix}")
    if runner:
        rows.append(f"{'runner':<{NAME_WIDTH}} {runner}")
    rows.append(f"{'gate':<{NAME_WIDTH}} {'status':<{STATUS_WIDTH}} detail")
    return rows


def parse_gates(specs: list[str]) -> list[Gate]:
    gates: list[Gate] = []
    for spec in specs:
        name, sep, status = spec.partition("=")
        if not sep or not name.strip():
            raise SystemExit(f"error: --gate expects NAME=STATUS, got {spec!r}")
        status = status.strip().upper()
        if status not in STATUSES:
            raise SystemExit(f"error: --gate {name}: status must be one of {', '.join(STATUSES)}")
        gates.append(Gate(name.strip(), status))
    return gates


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="_gate_summary.py",
        description=__doc__.splitlines()[0],
        epilog="Exit: 0 when every gate passed or was skipped, 1 when one failed, 2 on a usage error.",
    )
    parser.add_argument(
        "--gate",
        action="append",
        default=[],
        metavar="NAME=STATUS",
        help="one gate row, printed in the order given; STATUS is PASS, FAIL or SKIP; repeatable",
    )
    parser.add_argument(
        "--coverage-gate", default="coverage", help="which --gate row the coverage percentage decorates"
    )
    parser.add_argument(
        "--bench-gate", default="benchmarks", help="which --gate row the benchmark verdict decorates"
    )
    parser.add_argument("--coverage-json", default=None, help="coverage.json the coverage gate wrote")
    parser.add_argument("--coverage-log", default=None, help="that job's own output, for the no-file case")
    parser.add_argument("--pyproject", default="pyproject.toml", help="where the committed coverage floor lives")
    parser.add_argument("--bench-json", default=None, help="bench-results.json the bench gate wrote")
    parser.add_argument("--bench-summary", default=None, help="bench-summary.md the bench gate wrote")
    parser.add_argument("--bench-log", default=None, help="that job's own output, for the no-file case")
    parser.add_argument("--workspace", default=None, help="the repository root the runner resolved")
    parser.add_argument("--workspace-source", default=None, help="how it resolved it, printed for the log")
    parser.add_argument("--runner", default=None, help="what ran the workflows (direct, act, act-mcp)")
    return parser


#: Every flag that takes exactly one value, so their values can be recognised below.
#: `--gate` is absent on purpose: it is `append`, and a NAME=STATUS never begins with a dash.
VALUE_FLAGS = (
    "--coverage-gate",
    "--bench-gate",
    "--coverage-json",
    "--coverage-log",
    "--pyproject",
    "--bench-json",
    "--bench-summary",
    "--bench-log",
    "--workspace",
    "--workspace-source",
    "--runner",
)


def allow_dashed_values(argv: list[str]) -> list[str]:
    """Rewrite `--flag value` as `--flag=value` when the value itself begins with a dash.

    A real case, not a typo: the runner prints *how* it resolved the workspace, and one of
    the answers it may print is the literal string ``--workspace``. As two tokens argparse
    reads the second as a flag and aborts with "expected one argument" - a usage error for
    a value the runner legitimately produced. Joined with ``=``, argparse takes the rest of
    the token verbatim.
    """
    out: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        following = argv[index + 1] if index + 1 < len(argv) else ""
        if token in VALUE_FLAGS and following.startswith("-") and "=" not in token:
            out.append(f"{token}={following}")
            index += 2
            continue
        out.append(token)
        index += 1
    return out


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(allow_dashed_values(sys.argv[1:] if argv is None else list(argv)))
    gates = parse_gates(args.gate)
    if not gates:
        print("error: no --gate rows to summarise", file=sys.stderr)
        return 2

    for gate in gates:
        if gate.status == SKIP:
            gate.detail = "skipped"
            continue
        verdict: Verdict | None = None
        if gate.name == args.coverage_gate:
            verdict = coverage_verdict(
                Path(args.coverage_json) if args.coverage_json else None,
                Path(args.coverage_log) if args.coverage_log else None,
                Path(args.pyproject),
            )
        elif gate.name == args.bench_gate:
            verdict = bench_verdict(
                Path(args.bench_json) if args.bench_json else None,
                Path(args.bench_summary) if args.bench_summary else None,
                Path(args.bench_log) if args.bench_log else None,
            )
        if verdict is None:
            continue
        gate.detail = verdict.detail + verdict.note
        # A gate whose process exited 0 while its own report says the floor was missed is
        # a FAIL; the reverse is not a rescue - the process that ran is the authority.
        if gate.status == PASS and verdict.ok is False:
            gate.status = FAIL

    overall = overall_status(gates)
    counts = {status: sum(1 for gate in gates if gate.status == status) for status in STATUSES}
    tally = f"{counts[PASS]} passed, {counts[FAIL]} failed"
    if counts[SKIP]:
        tally += f", {counts[SKIP]} skipped"

    for line in header_rows(args.workspace, args.workspace_source, args.runner):
        print(line)
    for gate in gates:
        print(gate.row())
    print(Gate("result", overall, tally).row())
    return 0 if overall in (PASS, SKIP) else 1


if __name__ == "__main__":
    raise SystemExit(main())
