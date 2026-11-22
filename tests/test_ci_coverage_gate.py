"""The coverage gate is a script, and the workflow is a call to it - both are checked.

ADR-0001 §12 and this repository's AGENTS.md say the same thing from two directions:
the build and test logic lives in ``scripts/ci/*.sh`` and ``.github/workflows/ci.yml``
holds none of it. A workflow that carries its own ``pip install``/``curl``/floor
arithmetic is the failure mode, because it passes while the laptop gate fails. So the
properties below are asserted rather than promised:

* ``scripts/ci/_coverage_report.py`` - the floor arithmetic, tested against synthetic
  coverage reports: one machine-readable ``coverage: NN.N%`` line, a nonzero exit
  below the overall floor, a nonzero exit below a *per-package* floor even when the
  total is fine, and an error (never a green) when there is no report to gate;
* the committed floors themselves are at or above the 80% the ticket promises, and
  live in ``pyproject.toml``, which is the only place they are written down;
* ``ci.yml`` checks out, sets the interpreter up from the committed version file,
  caches dependencies explicitly, calls ``scripts/ci/coverage.sh`` and nothing else,
  uploads both reports as artifacts, needs no secrets, and keeps its matrix in step
  with ``.python-version``;
* ``scripts/ci/coverage.sh`` really forwards its floor and artifact flags to the
  helper (run against a recording stub interpreter, the way ``tests/test_tooling.py``
  runs the ``scripts/dev`` wrappers).
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO / ".github" / "workflows"

if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))


def _load_helper() -> Any:
    spec = importlib.util.spec_from_file_location(
        "_coverage_report", REPO / "scripts" / "ci" / "_coverage_report.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cov = _load_helper()

MACHINE_LINE = re.compile(r"^coverage: \d+\.\d%$")


def _summary(statements: int, covered: int, branches: int = 0, covered_branches: int = 0) -> dict[str, Any]:
    """A per-file ``summary`` block with only what the gate reads, filled consistently."""
    denominator = statements + branches
    numerator = covered + covered_branches
    return {
        "num_statements": statements,
        "covered_lines": covered,
        "missing_lines": statements - covered,
        "num_branches": branches,
        "covered_branches": covered_branches,
        "missing_branches": branches - covered_branches,
        "percent_covered": 100.0 * numerator / denominator if denominator else 100.0,
    }


def _report(files: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """A ``coverage json`` document: per-file summaries plus matching totals."""
    totals = dict.fromkeys(cov.COUNTERS, 0)
    for entry in files.values():
        for key in cov.COUNTERS:
            totals[key] += entry["summary"][key]
    totals["excluded_lines"] = 0
    return {"files": files, "totals": totals}


class FloorGateTest(unittest.TestCase):
    """scripts/ci/_coverage_report.py, tested on reports it was not produced from."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.pyproject = self.root / "pyproject.toml"
        self.pyproject.write_text(
            textwrap.dedent(
                """
                [tool.coverage.report]
                fail_under = 85.0

                [tool.fitbit_monitor.coverage]
                default_package_floor = 80.0
                package_floors = { api = 88.0, cli = 82.0 }
                """
            )
        )

    def _write(self, report: dict[str, Any], name: str = "coverage.json") -> Path:
        path = self.root / name
        path.write_text(json.dumps(report))
        return path

    def _run(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cov.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def _healthy(self) -> dict[str, Any]:
        """90% overall, api 90%, cli 85% - above every floor in the test pyproject."""
        return _report(
            {
                "src/fitbit_monitor/api/http.py": {"summary": _summary(100, 90, 20, 18)},
                "src/fitbit_monitor/cli/main.py": {"summary": _summary(100, 83, 20, 17)},
                "src/fitbit_monitor/config.py": {"summary": _summary(20, 18)},
            }
        )

    def test_prints_exactly_one_machine_readable_line(self) -> None:
        code, out, _ = self._run("--json", str(self._write(self._healthy())), "--pyproject", str(self.pyproject))
        self.assertEqual(code, 0)
        lines = [line for line in out.splitlines() if MACHINE_LINE.match(line)]
        self.assertEqual(len(lines), 1, f"expected one machine-readable line in:\n{out}")
        self.assertEqual(lines[0], "coverage: 86.9%")

    def test_names_the_artifacts_it_wrote(self) -> None:
        code, out, _ = self._run(
            "--json", str(self._write(self._healthy())),
            "--pyproject", str(self.pyproject),
            "--xml-out", "cov.xml",
            "--json-out", "cov.json",
            "--artifact-name", "coverage-xml-3.12",
            "--json-artifact-name", "coverage-json-3.12",
        )
        self.assertEqual(code, 0)
        self.assertIn("artifact coverage-xml-3.12: cov.xml", out)
        self.assertIn("artifact coverage-json-3.12: cov.json", out)

    def test_passes_at_or_above_every_floor(self) -> None:
        code, _, err = self._run("--json", str(self._write(self._healthy())), "--pyproject", str(self.pyproject))
        self.assertEqual(code, 0, err)
        self.assertEqual(err, "")

    def test_fails_below_the_overall_floor(self) -> None:
        report = _report({"src/fitbit_monitor/api/http.py": {"summary": _summary(100, 80, 20, 12)}})
        code, out, err = self._run("--json", str(self._write(report)), "--pyproject", str(self.pyproject))
        self.assertEqual(code, 1, out)
        self.assertIn("overall", err)
        self.assertIn("FAIL", out)

    def test_fails_below_a_package_floor_even_when_the_total_passes(self) -> None:
        """The point of per-package floors: a healthy total hiding a hollow package."""
        report = _report(
            {
                "src/fitbit_monitor/api/http.py": {"summary": _summary(400, 396, 40, 40)},
                "src/fitbit_monitor/cli/main.py": {"summary": _summary(100, 70, 20, 10)},
            }
        )
        code, out, err = self._run("--json", str(self._write(report)), "--pyproject", str(self.pyproject))
        total = next(line for line in out.splitlines() if MACHINE_LINE.match(line))
        self.assertGreaterEqual(float(total.split()[1].rstrip("%")), 88.0, "the total should have looked fine")
        self.assertEqual(code, 1, out)
        self.assertIn("cli", err)
        self.assertNotIn("api ", err, "api passed and should not be named")

    def test_floor_flags_override_the_committed_numbers_for_one_run(self) -> None:
        args = ["--json", str(self._write(self._healthy())), "--pyproject", str(self.pyproject)]
        self.assertEqual(self._run(*args)[0], 0)
        code, out, err = self._run(*args, "--floor", "95")
        self.assertEqual(code, 1, out)
        self.assertIn("95.0", err)
        code, out, err = self._run(*args, "--package-floor", "api=99", "--default-package-floor", "95")
        self.assertEqual(code, 1, out)
        self.assertIn("api", err)

    def test_a_report_that_is_not_there_is_an_error_not_a_green(self) -> None:
        code, _, err = self._run("--json", str(self.root / "missing.json"), "--pyproject", str(self.pyproject))
        self.assertEqual(code, 2)
        self.assertIn("error", err)

    def test_an_empty_report_is_an_error(self) -> None:
        path = self._write(_report({"src/fitbit_monitor/x.py": {"summary": _summary(0, 0)}}), "empty.json")
        code, _, err = self._run("--json", str(path), "--pyproject", str(self.pyproject))
        self.assertEqual(code, 2)
        self.assertIn("error", err)

    def test_a_source_root_matching_nothing_is_an_error(self) -> None:
        code, _, err = self._run(
            "--json",
            str(self._write(self._healthy())),
            "--pyproject",
            str(self.pyproject),
            "--source-root",
            "src/not_here",
        )
        self.assertEqual(code, 2)
        self.assertIn("source-root", err)

    def test_buckets_are_the_directories_under_the_source_root(self) -> None:
        buckets = cov.buckets(self._healthy(), "src/fitbit_monitor")
        self.assertEqual(set(buckets), {"api", "cli", cov.ROOT_BUCKET})

    def test_percent_counts_branches_like_coverage_does(self) -> None:
        # (90 lines + 10 branches) / (100 statements + 20 branches), coverage's formula.
        self.assertAlmostEqual(cov._percent(_summary(100, 90, 20, 10)), 83.33333333333334)
        self.assertEqual(cov._percent(_summary(0, 0)), 100.0)


class CommittedFloorPolicyTest(unittest.TestCase):
    """The numbers the gate will actually hold this repository to."""

    def test_every_committed_floor_is_at_least_eighty(self) -> None:
        overall, default_package, packages = cov.committed_floors(REPO / "pyproject.toml")
        self.assertGreaterEqual(overall, 80.0, "the overall floor is [tool.coverage.report] fail_under")
        self.assertGreaterEqual(default_package, 80.0)
        self.assertTrue(packages, "no per-package floors are committed")
        for name, floor in packages.items():
            self.assertGreaterEqual(floor, 80.0, f"package {name} floor")

    def test_package_floors_are_directories_that_exist(self) -> None:
        _, _, packages = cov.committed_floors(REPO / "pyproject.toml")
        root = REPO / "src" / "fitbit_monitor"
        for name in packages:
            if name == cov.ROOT_BUCKET:
                continue
            self.assertTrue((root / name).is_dir(), f"floor for {name}, which is not a package directory")

    def test_unknown_package_falls_back_to_the_default_floor(self) -> None:
        overall, default_package, packages = cov.committed_floors(REPO / "pyproject.toml")
        self.assertNotIn("no_such_package", packages)
        self.assertGreaterEqual(default_package, 80.0)
        self.assertGreater(overall, 0.0)


class CoverageWorkflowTest(unittest.TestCase):
    """`.github/workflows/ci.yml` may call the gate, nothing else."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.doc = yaml.safe_load((WORKFLOWS / "ci.yml").read_text())
        cls.job = cls.doc["jobs"]["coverage"]

    def _steps(self, job: dict[str, Any]) -> list[dict[str, Any]]:
        return job["steps"]

    def _uses(self, job: dict[str, Any]) -> list[str]:
        return [str(step.get("uses", "")) for step in self._steps(job)]

    def test_checks_out_and_sets_up_python_explicitly(self) -> None:
        uses = self._uses(self.job)
        self.assertTrue(any(use.startswith("actions/checkout") for use in uses), uses)
        self.assertTrue(any(use.startswith("actions/setup-python") for use in uses), uses)

    def test_the_interpreter_comes_from_the_committed_version_file(self) -> None:
        """No version literal in YAML: the band is `.python-version`, and uv reads it too."""
        pinned = (REPO / ".python-version").read_text().split()
        self.assertTrue(pinned, ".python-version is empty")
        for name, job in self.doc["jobs"].items():
            for step in self._steps(job):
                if not str(step.get("uses", "")).startswith("actions/setup-python"):
                    continue
                inputs = step.get("with") or {}
                self.assertTrue(
                    inputs.get("python-version-file"),
                    f"{name}: setup-python should read the committed version file, got {inputs}",
                )
                self.assertTrue(
                    (REPO / str(inputs["python-version-file"])).is_file(),
                    f"{name}: {inputs['python-version-file']} is not committed",
                )
                self.assertNotIn(
                    "python-version",
                    inputs,
                    f"{name}: a literal version here duplicates .python-version",
                )

    def test_dependencies_are_cached_by_an_explicit_step(self) -> None:
        cached = [
            step
            for step in self._steps(self.job)
            if str(step.get("uses", "")).startswith(("astral-sh/setup-uv", "actions/cache"))
        ]
        self.assertTrue(cached, "no dependency cache step")
        self.assertTrue(
            any((step.get("with") or {}).get("enable-cache") for step in cached),
            "the cache step does not enable caching",
        )

    def test_the_only_run_step_calls_the_coverage_script(self) -> None:
        ran = [step["run"] for step in self._steps(self.job) if "run" in step]
        self.assertTrue(ran, "the coverage job runs nothing")
        for command in ran:
            self.assertIn("scripts/ci/coverage.sh", command, command)
            self.assertIn("--no-install", command, command)
            self.assertIn("--locked", command, command)
            self.assertNotIn("pip install", command, command)
            self.assertNotIn("curl", command, command)

    def test_both_reports_are_uploaded_as_artifacts(self) -> None:
        uploads = [
            (step.get("with") or {})
            for step in self._steps(self.job)
            if str(step.get("uses", "")).startswith("actions/upload-artifact")
        ]
        self.assertTrue(uploads, "nothing is uploaded")
        paths = {str(inputs.get("path")) for inputs in uploads}
        self.assertIn("coverage.xml", paths, uploads)
        self.assertIn("coverage.json", paths, uploads)
        for inputs in uploads:
            self.assertTrue(inputs.get("name"), f"an upload step has no artifact name: {inputs}")
            self.assertEqual(inputs.get("if-no-files-found"), "error", f"{inputs}: a missing report must fail")

    def test_the_gate_and_the_uploads_agree_on_the_artifact_names(self) -> None:
        """The names the script is told to call its reports are the names uploaded."""
        command = next(step["run"] for step in self._steps(self.job) if "run" in step)
        uploaded = {
            str((step.get("with") or {}).get("name"))
            for step in self._steps(self.job)
            if str(step.get("uses", "")).startswith("actions/upload-artifact")
        }
        for name in uploaded:
            self.assertTrue(
                f"--artifact-name {name}" in command or f"--json-artifact-name {name}" in command,
                f"the gate is never told about the artifact {name!r}:\n{command}",
            )
        self.assertEqual(command.count("-artifact-name "), 2, "one artifact flag per report")

    def test_no_job_needs_a_secret(self) -> None:
        text = (WORKFLOWS / "ci.yml").read_text()
        self.assertNotIn("secrets.", text)
        self.assertEqual(self.doc.get("permissions"), {"contents": "read"})

    def test_the_matrix_and_the_committed_version_file_agree(self) -> None:
        versions = [str(v) for v in self.doc["jobs"]["coverage"]["strategy"]["matrix"]["python-version"]]
        pinned = (REPO / ".python-version").read_text().split()
        self.assertEqual(sorted(versions), sorted(pinned), ".python-version is the band; the matrix must match it")

    def test_the_benchmark_workflow_delegates_to_its_script_too(self) -> None:
        doc = yaml.safe_load((WORKFLOWS / "benchmark.yml").read_text())
        for job in doc["jobs"].values():
            for step in job["steps"]:
                if "run" in step and "scripts/" in step["run"]:
                    self.assertIn("scripts/ci/benchmarks.sh", step["run"])


class CoverageGateScriptTest(unittest.TestCase):
    """scripts/ci/coverage.sh forwards what it advertises."""

    def _run_stubbed(self, *args: str, exit_code: int = 0) -> tuple[subprocess.CompletedProcess, list[str]]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        trace = Path(tmp.name) / "calls.log"
        stub = Path(tmp.name) / "python"
        stub.write_text(f"#!/usr/bin/env bash\necho \"$*\" >> {trace}\nexit {exit_code}\n")
        stub.chmod(0o755)
        result = subprocess.run(
            [str(REPO / "scripts" / "ci" / "coverage.sh"), "--no-install", *args],
            cwd=REPO,
            capture_output=True,
            text=True,
            env=dict(os.environ, PYTHON=str(stub)),
            timeout=120,
        )
        calls = [line for line in trace.read_text().splitlines() if line.strip()] if trace.exists() else []
        return result, calls

    def test_writes_both_reports_and_calls_the_floor_helper(self) -> None:
        result, calls = self._run_stubbed()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        joined = "\n".join(calls)
        for needle in ("-m unittest", "-m coverage run", "-m coverage report", "-m coverage xml", "-m coverage json"):
            self.assertIn(needle, joined, f"the gate never ran `{needle}`:\n{joined}")
        self.assertIn("scripts/ci/_coverage_report.py", joined)
        self.assertIn("--xml-out coverage.xml", joined)
        self.assertIn("--json-out coverage.json", joined)

    def test_forwards_the_floor_and_artifact_flags(self) -> None:
        result, calls = self._run_stubbed(
            "--floor",
            "97",
            "--package-floor",
            "cli=91",
            "--package-floor",
            "api=94",
            "--artifact-name",
            "coverage-xml-3.12",
            "--json-artifact-name",
            "coverage-json-3.12",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        helper = next(line for line in calls if "_coverage_report.py" in line)
        for needle in ("--floor 97", "--package-floor cli=91", "--package-floor api=94"):
            self.assertIn(needle, helper)
        self.assertIn("--artifact-name coverage-xml-3.12", helper)
        self.assertIn("--json-artifact-name coverage-json-3.12", helper)

    def test_a_failing_gate_fails_the_script(self) -> None:
        result, _ = self._run_stubbed(exit_code=1)
        self.assertEqual(result.returncode, 1)

    def test_help_lists_the_flags_and_unknown_ones_are_refused(self) -> None:
        help_text = subprocess.run(
            [str(REPO / "scripts" / "ci" / "coverage.sh"), "--help"],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(help_text.returncode, 0)
        for flag in ("--floor", "--package-floor", "--xml-out", "--json-out", "--artifact-name", "--json-artifact-name"):
            self.assertIn(flag, help_text.stdout)
        bogus = subprocess.run(
            [str(REPO / "scripts" / "ci" / "coverage.sh"), "--no-install", "--nonsense"],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(bogus.returncode, 2)
        self.assertIn("--nonsense", bogus.stderr)


if __name__ == "__main__":
    unittest.main()
