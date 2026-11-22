#!/usr/bin/env python3
"""The coverage floor, decided in one place and printed in one machine-readable line.

Called by ``scripts/ci/coverage.sh`` after ``coverage json`` has written its report.
It reads that report, adds up each package inside ``--source-root``, compares the
total and each package against the committed floors and prints::

    coverage: 92.1%                                     <- the line CI greps for
    coverage overall 92.1% (floor 85.0%) ok
    coverage package api 92.3% (floor 88.0%) ok
    coverage package cli 86.0% (floor 82.0%) ok
    artifact coverage-xml: coverage.xml
    artifact coverage-json: coverage.json

Every number is printed exactly as it was compared: the comparison is made on the
value rounded to the same decimal places it is printed with, so the log never shows
85.0% passing a floor of 85.0% while an unprinted 84.96% failed it.

Exit codes: ``0`` at or above every floor, ``1`` below one (with the offending lines
repeated on stderr), ``2`` when the report is missing, unreadable or empty - an empty
report means the suite never ran, which is not a coverage result.

Floors are read from ``pyproject.toml``, which is the one place they are written down:

* overall: ``[tool.coverage.report] fail_under`` (the number ``coverage report``
  already enforces, so the two gates cannot disagree);
* per package: ``[tool.fitbit_monitor.coverage]`` - ``default_package_floor`` plus a
  ``package_floors`` table keyed by the directory under ``--source-root`` (``"."`` is
  the modules at the root itself).

The ``--floor`` / ``--package-floor`` / ``--default-package-floor`` flags override
those numbers for one run, which is how a ratchet gets tried before it is committed.
They are not a way to sneak a push under the bar: the number that gates a push is the
number in git, and CI passes none of them.
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path
from typing import Any

#: Bucket name for the modules that sit directly in ``--source-root``, written the way
#: git writes "this directory": a single dot.
ROOT_BUCKET = "."

#: The counters summed per bucket; see ``_percent``.
COUNTERS = ("num_statements", "covered_lines", "num_branches", "covered_branches")


def _percent(summary: dict[str, Any]) -> float:
    """Percent covered for one summary, statements and branches together.

    Same formula coverage.py uses once ``--branch`` is on: the denominator is
    statements plus branches, the numerator lines plus branches actually taken.
    """
    denominator = int(summary["num_statements"]) + int(summary["num_branches"])
    if denominator <= 0:
        return 100.0
    numerator = int(summary["covered_lines"]) + int(summary["covered_branches"])
    return 100.0 * numerator / denominator


def load_report(path: Path) -> dict[str, Any]:
    """The ``coverage json`` document, or a hard stop explaining why there is none."""
    if not path.is_file():
        raise ValueError(
            f"{path} does not exist - `coverage json -o {path}` did not produce a report, "
            "so the suite did not run"
        )
    try:
        report = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ValueError(f"{path} is not readable JSON ({exc})") from exc
    if not isinstance(report, dict) or "files" not in report or "totals" not in report:
        raise ValueError(f"{path} is not a coverage JSON report (no 'files'/'totals' keys)")
    totals = report["totals"]
    if int(totals["num_statements"]) + int(totals["num_branches"]) <= 0:
        raise ValueError(
            f"{path} measured nothing at all - check --source-root against what the suite imports"
        )
    return report


def buckets(report: dict[str, Any], source_root: str) -> dict[str, dict[str, int]]:
    """bucket name -> summed counters, from the per-file entries of the report.

    A bucket is the first path component under ``source_root``: a subpackage
    (``api``, ``store``) or ``ROOT_BUCKET`` for the modules at the root. Paths are
    matched by suffix so the check does not care whether coverage recorded
    ``src/fitbit_monitor/api/http.py`` or an absolute path.
    """
    root = source_root.rstrip("/")
    totals: dict[str, dict[str, int]] = {}
    for recorded, entry in report["files"].items():
        normalised = recorded.replace("\\", "/")
        marker = normalised.find(root + "/")
        if marker < 0:
            continue
        remainder = normalised[marker + len(root) + 1 :]
        head, _, tail = remainder.partition("/")
        bucket = ROOT_BUCKET if tail == "" else head
        summary = entry["summary"]
        acc = totals.setdefault(bucket, dict.fromkeys(COUNTERS, 0))
        for key in COUNTERS:
            acc[key] += int(summary[key])
    if not totals:
        raise ValueError(f"no file in the report lives under {root}/ - wrong --source-root?")
    return totals


def committed_floors(pyproject: Path) -> tuple[float, float, dict[str, float]]:
    """(overall, default per-package, explicit per-package) as committed."""
    raw = tomllib.loads(pyproject.read_text())
    overall = float(raw.get("tool", {}).get("coverage", {}).get("report", {}).get("fail_under", 0.0))
    section = raw.get("tool", {}).get("fitbit_monitor", {}).get("coverage", {})
    default = float(section.get("default_package_floor", 0.0))
    floors = {str(name): float(value) for name, value in (section.get("package_floors") or {}).items()}
    return overall, default, floors


def parse_package_floor(text: str) -> tuple[str, float]:
    """``api=90`` -> ``("api", 90.0)``; anything else is a usage error."""
    name, sep, value = text.partition("=")
    if not sep or not name.strip():
        raise argparse.ArgumentTypeError(f"expected NAME=FLOAT, got {text!r}")
    try:
        return name.strip(), float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{text!r} does not end in a number: {exc}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="_coverage_report.py",
        description=__doc__.splitlines()[0],
        epilog="Exit: 0 at or above every floor, 1 below one, 2 no usable report.",
    )
    parser.add_argument("--json", default="coverage.json", help="coverage JSON report to read")
    parser.add_argument("--pyproject", default="pyproject.toml", help="where the committed floors live")
    parser.add_argument("--source-root", default="src/fitbit_monitor", help="root of the measured package")
    parser.add_argument("--floor", type=float, default=None, help="overall floor, overriding pyproject")
    parser.add_argument(
        "--default-package-floor", type=float, default=None, help="floor for packages with no committed entry"
    )
    parser.add_argument(
        "--package-floor",
        action="append",
        default=[],
        type=parse_package_floor,
        metavar="NAME=FLOAT",
        help="one package's floor, overriding pyproject; repeatable",
    )
    parser.add_argument("--xml-out", default="coverage.xml", help="the XML report coverage.sh wrote")
    parser.add_argument("--json-out", default="coverage.json", help="the JSON report coverage.sh wrote")
    parser.add_argument(
        "--artifact-name", default="coverage-xml", help="artifact the workflow uploads for the XML"
    )
    parser.add_argument("--json-artifact-name", default="coverage-json", help="and for the JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = load_report(Path(args.json))
        overall_floor, default_package_floor, committed_package_floors = committed_floors(Path(args.pyproject))
        per_package = buckets(report, args.source_root)
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        print(f"error: no coverage report to gate: {exc}", file=sys.stderr)
        return 2

    overall_floor = overall_floor if args.floor is None else args.floor
    default_package_floor = (
        default_package_floor if args.default_package_floor is None else args.default_package_floor
    )
    package_floors = dict(committed_package_floors)
    package_floors.update(dict(args.package_floor))

    overall = round(_percent(report["totals"]), 1)
    # The one machine-readable line the workflow and a laptop both grep for.
    print(f"coverage: {overall:.1f}%")

    failures: list[str] = []
    overall_ok = overall >= overall_floor
    if not overall_ok:
        failures.append(f"overall {overall:.1f}% is below the floor {overall_floor:.1f}%")
    print(f"coverage overall {overall:.1f}% (floor {overall_floor:.1f}%) {'ok' if overall_ok else 'FAIL'}")

    for bucket, counters in sorted(per_package.items()):
        percent = round(_percent(counters), 1)
        floor = package_floors.get(bucket, default_package_floor)
        ok = percent >= floor
        if not ok:
            failures.append(f"package {bucket} {percent:.1f}% is below its floor {floor:.1f}%")
        print(f"coverage package {bucket} {percent:.1f}% (floor {floor:.1f}%) {'ok' if ok else 'FAIL'}")

    # The names the workflow's upload-artifact steps use; kept here so the file the
    # gate writes and the artifact it becomes are named in the same place.
    print(f"artifact {args.artifact_name}: {args.xml_out}")
    print(f"artifact {args.json_artifact_name}: {args.json_out}")

    if failures:
        print(file=sys.stderr)
        for line in failures:
            print(f"coverage floor failed: {line}", file=sys.stderr)
        print(
            "coverage floor failed: raise the floor in pyproject.toml only in the commit that earns it",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
