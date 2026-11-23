"""The benchmark gate is a script, and the workflow is a call to it - both are checked.

``AGENTS.md`` and ADR-0001 §12 forbid a workflow that carries its own build or test
command, and ADR-0001 §9 decides what the perf gate is: five committed cases, a committed
baseline, a tolerance, and a baseline that only ever moves in a diff a human signs. The
properties below are the ones that make those sentences true rather than aspirational:

* ``scripts/bench/bench_plugin_system.py`` - the statistics (``mean`` / ``stddev`` /
  ``iterations`` per case, the best sample that gates), the verdict (a tracked case more
  than **15%** past the baseline fails; an untracked one never does), both refusals (no
  baseline: exit 2, a corpus the baseline did not record: reported but not gated), and
  both reports (a results document with an environment summary, and the markdown a pull
  request is commented with);
* ``scripts/ci/bench.sh`` - run against a recording stub interpreter, the way
  ``tests/test_tooling.py`` runs the ``scripts/dev`` wrappers: it names the harness,
  forwards the report paths and the artifact names, and carries the harness's exit code;
* ``.github/workflows/bench.yml`` - checkout, ``actions/setup-python`` from the committed
  version file, one ``run`` step and it calls ``scripts/ci/bench.sh``, the uploaded
  artifact names are the names the script was told to write, the PR comment posts the
  markdown the script generated, and no secret appears anywhere.

A benchmark test that re-measures the world on every run is slow and flaky, so the
statistical tests work on ``Case`` objects with samples they were handed; the end-to-end
tests run the harness once over the smallest corpus that still exercises the store.
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
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO / ".github" / "workflows"

if str(REPO / "src") not in sys.path:  # the harness imports fitbit_monitor at import time
    sys.path.insert(0, str(REPO / "src"))


def _load_harness() -> Any:
    spec = importlib.util.spec_from_file_location(
        "bench_plugin_system", REPO / "scripts" / "bench" / "bench_plugin_system.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, module)
    spec.loader.exec_module(module)
    return module


bench = _load_harness()

BASELINE = REPO / "benchmarks" / "baseline.json"


def case(name: str, samples: list[float] | None = None, better: str = "higher", factor: int = 1) -> bench.Case:
    """A case with samples already taken - the statistics are what is under test here."""
    unit = "ms" if better == "lower" else "points/sec"
    made = bench.Case(name, f"the {name} case", unit, lambda: 0.0, better=better, sample_factor=factor)
    made.samples = list(samples or [])
    return made


def run_harness(*argv: str) -> tuple[int, str, str]:
    """Run the harness in-process with stdout/stderr captured."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = bench.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class StatisticTest(unittest.TestCase):
    """What one case reports about the samples it was given."""

    def test_best_sample_gates_in_whichever_direction_is_good(self) -> None:
        throughput = case("scan_intraday", [10.0, 12.0, 11.0])
        self.assertEqual(throughput.value, 12.0)
        self.assertEqual(throughput.worst, 10.0)
        milliseconds = case("discover_plugins", [8.0, 6.0, 7.0], better="lower")
        self.assertEqual(milliseconds.value, 6.0)
        self.assertEqual(milliseconds.worst, 8.0)

    def test_mean_stddev_and_iterations_are_reported_separately(self) -> None:
        # The gate reads `value`; the reports read these, so they must be independent.
        made = case("seed_corpus", [10.0, 11.0, 12.0])
        self.assertEqual(made.mean, 11.0)
        self.assertAlmostEqual(made.stddev, 1.0, places=12)
        self.assertEqual(made.iterations, 3)
        self.assertEqual(case("one_shot", [5.0]).stddev, 0.0, "one sample has no spread")

    def test_direction_makes_a_bigger_number_good_or_bad(self) -> None:
        slower = case("discover_plugins", [20.0], better="lower")
        self.assertEqual(slower.percent_vs(10.0), -50.0)
        self.assertEqual(slower.regression_vs(10.0), 50.0)
        faster = case("scan_intraday", [50.0])
        self.assertEqual(faster.percent_vs(100.0), -50.0)
        self.assertEqual(faster.regression_vs(100.0), 50.0)
        self.assertEqual(case("nothing_measured").percent_vs(10.0), 0.0, "no samples, no claim")

    def test_cheap_cases_take_more_samples_and_the_count_is_fixed(self) -> None:
        """The multiplier is a constant per case: an adaptive count would compare
        best-of-N with best-of-M, and the machine's speed would change the bias."""
        self.assertEqual(bench.measure([case("a", [], factor=5)], repeats=3)[0].iterations, 15)
        self.assertEqual(bench.measure([case("b", [])], repeats=3)[0].iterations, 3)

    def test_the_sub_millisecond_cases_declare_the_extra_samples(self) -> None:
        factors = {
            "seed_corpus": 1,
            "scan_intraday": 1,
            "downsample_to_days": 1,
            "hr_daily_features": 1,
            "plugin_resting_trend": 5,
            "discover_plugins": 20,
        }
        with tempfile.TemporaryDirectory() as tmp:
            store_path, window = bench.corpus(2, Path(tmp))
            try:
                built = bench.build_cases(store_path, window, 2)
            finally:
                store_path.unlink(missing_ok=True)
        self.assertEqual([c.name for c in built], list(factors))
        for made in built:
            self.assertEqual(made.sample_factor, factors[made.name], made.name)
            self.assertIn(made.unit, ("rows/sec", "points/sec", "days/sec", "ms"))
            self.assertIn(made.better, ("higher", "lower"))


class GateVerdictTest(unittest.TestCase):
    """The number the ticket promises: a tracked case more than 15% past the baseline fails."""

    def test_the_committed_tolerance_is_fifteen_percent(self) -> None:
        self.assertEqual(bench.DEFAULT_TOLERANCE, 15.0)
        self.assertIn("--tolerance", bench.build_parser().format_help())

    def test_a_case_past_the_tolerance_is_a_regression_and_exits_nonzero(self) -> None:
        cases = [case("scan_intraday", [100.0])]
        problems = bench.compare(cases, {"scan_intraday": 130.0}, 15.0)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("scan_intraday", problems[0])
        self.assertIn("15%", problems[0])
        self.assertEqual(bench.compare(cases, {"scan_intraday": 110.0}, 15.0), [])

    def test_an_untracked_case_is_reported_but_never_gated(self) -> None:
        rows = bench.case_rows([case("new_thing", [1.0])], {}, 15.0, gated=True)
        self.assertEqual(rows[0]["status"], "untracked")
        self.assertIsNone(rows[0]["baseline"])
        self.assertEqual(bench.compare([case("new_thing", [1.0])], {}, 15.0), [])

    def test_status_distinguishes_a_verdict_from_a_report_only_measurement(self) -> None:
        cases = [case("seed_corpus", [100.0])]
        self.assertEqual(bench.case_rows(cases, {"seed_corpus": 200.0}, 15.0, gated=True)[0]["status"], "regressed")
        self.assertEqual(
            bench.case_rows(cases, {"seed_corpus": 200.0}, 15.0, gated=False)[0]["status"],
            "over_tolerance",
        )

    def test_results_document_carries_the_environment_and_the_gate_verdict(self) -> None:
        cases = [case("scan_intraday", [100.0, 110.0])]
        document = bench.results_document(
            cases,
            {"scan_intraday": 90.0},
            days=90,
            repeats=9,
            tolerance=15.0,
            baseline_path=BASELINE,
            problems=[],
            gated=True,
            skipped_reason=None,
            seconds=1.0,
        )
        self.assertTrue(set(document) >= {"schema", "config", "environment", "cases", "gate", "notes"})
        row = document["cases"][0]
        for key in ("case", "mean", "stddev", "iterations", "best", "worst", "gate_value", "status"):
            self.assertIn(key, row)
        self.assertEqual(row["iterations"], 2)
        self.assertEqual(row["gate_value"], 110.0)
        self.assertEqual(row["baseline"], 90.0)
        self.assertAlmostEqual(row["delta_percent"], 22.22, places=2)
        self.assertTrue(document["gate"]["passed"])
        self.assertEqual(document["gate"]["tracked_cases"], 1)
        environment = document["environment"]
        for key in ("python", "system", "machine", "cpu_count"):
            self.assertIn(key, environment)
        self.assertEqual(document["config"]["baseline"], "benchmarks/baseline.json", "repo-relative")


class BaselineTest(unittest.TestCase):
    """The baseline is reviewable JSON that only the explicit command writes."""

    def test_the_committed_baseline_names_exactly_the_measured_cases(self) -> None:
        recorded = json.loads(BASELINE.read_text())
        with tempfile.TemporaryDirectory() as tmp:
            store_path, window = bench.corpus(2, Path(tmp))
            try:
                measured = [c.name for c in bench.build_cases(store_path, window, 2)]
            finally:
                store_path.unlink(missing_ok=True)
        self.assertEqual(sorted(recorded["cases"]), sorted(measured))
        self.assertEqual(recorded["tolerance_percent"], bench.DEFAULT_TOLERANCE)

    def test_recorded_stats_survive_a_round_trip(self) -> None:
        cases = [case("scan_intraday", [90.0, 100.0, 110.0]), case("discover_plugins", [2.0, 1.0], better="lower")]
        payload = bench.baseline_document(cases, days=90, tolerance=15.0)
        self.assertEqual(set(payload["cases"]), {"scan_intraday", "discover_plugins"})
        self.assertEqual(set(payload["stats"]), set(payload["cases"]))
        stats = payload["stats"]["scan_intraday"]
        self.assertEqual(stats["iterations"], 3)
        self.assertEqual(stats["best"], 110.0)
        self.assertEqual(stats["mean"], 100.0)
        self.assertIn("environment", payload)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "baseline.json"
            path.write_text(json.dumps(payload))
            self.assertEqual(bench.load_baseline(path), {"scan_intraday": 110.0, "discover_plugins": 1.0})

    def test_the_old_flat_baseline_still_reads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "baseline.json"
            path.write_text(json.dumps({"scan_intraday": 123.5}))
            self.assertEqual(bench.load_baseline(path), {"scan_intraday": 123.5})
            self.assertEqual(bench.load_baseline(Path(tmp) / "nowhere.json"), {})

    def test_no_baseline_is_a_hard_stop_not_a_green(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, _, err = run_harness("--days", "2", "--repeats", "1", "--baseline", f"{tmp}/missing.json")
        self.assertEqual(code, 2)
        self.assertIn("--update-baseline", err)

    def test_a_corpus_the_baseline_did_not_record_is_reported_but_not_gated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            results = Path(tmp) / "results.json"
            inflated = json.loads(BASELINE.read_text())
            inflated["cases"] = {name: value * 10 for name, value in inflated["cases"].items()}
            fake = Path(tmp) / "baseline.json"
            fake.write_text(json.dumps(inflated))
            # --days 2 against a 90-day baseline: every case would "regress", and that
            # would be a comparison of two different quantities rather than a result.
            code, _, err = run_harness(
                "--days", "2", "--repeats", "1", "--baseline", str(fake), "--json-out", str(results)
            )
            self.assertEqual(code, 0, err)
            document = json.loads(results.read_text())
        self.assertFalse(document["gate"]["passed"])
        self.assertIn("--days", document["gate"]["skipped_reason"])

    def test_an_inflated_baseline_fails_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inflated = json.loads(BASELINE.read_text())
            inflated["days"] = 2  # so the corpus mismatch is not the reason it fails
            inflated["cases"] = {name: value * 10 for name, value in inflated["cases"].items()}
            fake = Path(tmp) / "baseline.json"
            fake.write_text(json.dumps(inflated))
            code, out, _ = run_harness("--days", "2", "--repeats", "1", "--baseline", str(fake))
        self.assertEqual(code, 1)
        self.assertIn("REGRESSION", out)


class ReportTest(unittest.TestCase):
    """Both reports, end to end, from one real (tiny) run."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.results = Path(cls.tmp.name) / "bench-results.json"
        cls.summary = Path(cls.tmp.name) / "bench-summary.md"
        code, _, err = run_harness(
            "--days",
            "2",
            "--repeats",
            "1",
            "--json-out",
            str(cls.results),
            "--markdown-out",
            str(cls.summary),
            "--artifact-name",
            "bench-results-json",
            "--markdown-artifact-name",
            "bench-summary-md",
        )
        assert code == 0, err
        cls.document = json.loads(cls.results.read_text())
        cls.markdown = cls.summary.read_text()

    def test_the_json_is_the_document_the_ticket_names(self) -> None:
        self.assertTrue(self.results.is_file())
        for row in self.document["cases"]:
            for key in ("case", "mean", "stddev", "iterations"):
                self.assertIn(key, row)
            self.assertGreaterEqual(row["iterations"], 1)
        self.assertIn("environment", self.document)
        self.assertEqual(self.document["gate"]["tracked_cases"], len(self.document["cases"]))

    def test_the_markdown_is_a_table_of_the_same_numbers(self) -> None:
        self.assertIn("## Benchmarks", self.markdown)
        self.assertIn("| case | value | mean", self.markdown)
        self.assertRegex(self.markdown, r"\*\*(PASS|FAIL|NOT GATED)\*\*")
        for row in self.document["cases"]:
            self.assertIn(f"`{row['case']}`", self.markdown)
            self.assertIn(f"{row['iterations']} |", self.markdown)
        self.assertIn("never refreshed automatically", self.markdown)

    def test_a_failing_gate_says_so_in_the_markdown(self) -> None:
        document = dict(self.document)
        document["gate"] = dict(
            self.document["gate"],
            gated=True,
            passed=False,
            skipped_reason=None,
            regressed_cases=["scan_intraday"],
        )
        markdown = bench.render_markdown(document)
        self.assertIn("**FAIL**", markdown)
        self.assertIn("`scan_intraday`", markdown)
        failing = bench.render_markdown(
            dict(document, regressions=["scan_intraday: 1.0 points/sec is 30.0% worse than the baseline 1.4"])
        )
        self.assertIn("### Regressions", failing)

    def test_a_passing_gate_says_so_too(self) -> None:
        document = dict(self.document)
        document["gate"] = dict(self.document["gate"], gated=True, passed=True, skipped_reason=None)
        self.assertIn("**PASS**", bench.render_markdown(document))

    def test_the_step_summary_is_appended_only_when_there_is_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "step-summary.md"
            target.write_text("before\n")
            os.environ["GITHUB_STEP_SUMMARY"] = str(target)
            try:
                bench.append_to_step_summary("## Benchmarks\n")
                self.assertEqual(target.read_text(), "before\n## Benchmarks\n")
            finally:
                os.environ.pop("GITHUB_STEP_SUMMARY", None)
            bench.append_to_step_summary("nothing happens now\n")
            self.assertEqual(target.read_text(), "before\n## Benchmarks\n")


class BenchGateScriptTest(unittest.TestCase):
    """scripts/ci/bench.sh: names the harness, forwards the flags, carries the code."""

    def _stubbed(self, *args: str, exit_code: int = 0) -> tuple[subprocess.CompletedProcess, list[str]]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        trace = Path(tmp.name) / "calls.log"
        stub = Path(tmp.name) / "python"
        stub.write_text(f"#!/usr/bin/env bash\necho \"$*\" >> {trace}\nexit {exit_code}\n")
        stub.chmod(0o755)
        result = subprocess.run(
            [str(REPO / "scripts" / "ci" / "bench.sh"), "--no-install", *args],
            cwd=REPO,
            capture_output=True,
            text=True,
            env=dict(os.environ, PYTHON=str(stub)),
            timeout=120,
        )
        calls = [line for line in trace.read_text().splitlines() if line.strip()] if trace.exists() else []
        return result, calls

    def test_runs_the_committed_harness_with_both_reports(self) -> None:
        result, calls = self._stubbed()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        joined = "\n".join(calls)
        self.assertIn("scripts/bench/bench_plugin_system.py", joined)
        self.assertIn("--json-out bench-results.json", joined)
        self.assertIn("--markdown-out bench-summary.md", joined)
        self.assertIn("--artifact-name bench-results-json", joined)
        self.assertIn("--markdown-artifact-name bench-summary-md", joined)
        self.assertIn("--step-summary", joined)

    def test_forwards_every_documented_flag(self) -> None:
        result, calls = self._stubbed(
            "--json-out",
            "r.json",
            "--markdown-out",
            "r.md",
            "--artifact-name",
            "art-json",
            "--markdown-artifact-name",
            "art-md",
            "--baseline",
            "b.json",
            "--tolerance",
            "7",
            "--days",
            "30",
            "--repeats",
            "3",
            "--update-baseline",
            "--no-gate",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        joined = "\n".join(calls)
        for needle in (
            "--json-out r.json",
            "--markdown-out r.md",
            "--artifact-name art-json",
            "--markdown-artifact-name art-md",
            "--baseline b.json",
            "--tolerance 7",
            "--days 30",
            "--repeats 3",
            "--update-baseline",
            "--no-gate",
        ):
            self.assertIn(needle, joined)

    def test_a_failing_harness_fails_the_gate(self) -> None:
        result, _ = self._stubbed(exit_code=1)
        self.assertEqual(result.returncode, 1)

    def test_unknown_flags_are_refused_and_help_is_self_documenting(self) -> None:
        unknown = subprocess.run(
            [str(REPO / "scripts" / "ci" / "bench.sh"), "--wat"],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(unknown.returncode, 2)
        self.assertIn("--help", unknown.stderr)
        help_text = subprocess.run(
            [str(REPO / "scripts" / "ci" / "bench.sh"), "--help"],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(help_text.returncode, 0)
        for needle in ("--no-install", "--json-out", "--markdown-out", "--update-baseline"):
            self.assertIn(needle, help_text.stdout)


class BenchWorkflowTest(unittest.TestCase):
    """bench.yml is a call to the gate, an upload, and a comment - in that order."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.path = WORKFLOWS / "bench.yml"
        cls.doc = yaml.safe_load(cls.path.read_text())

    def _steps(self) -> list[dict]:
        return self.doc["jobs"]["benchmark"]["steps"]

    def _run_commands(self) -> list[str]:
        return [step["run"] for step in self._steps() if "run" in step]

    def test_the_old_names_are_gone(self) -> None:
        """One gate, one name: a second copy under the old name is how drift starts."""
        self.assertFalse((WORKFLOWS / "benchmark.yml").exists())
        self.assertFalse((REPO / "scripts" / "ci" / "benchmarks.sh").exists())

    def test_the_interpreter_comes_from_the_committed_version_file(self) -> None:
        setups = [step for step in self._steps() if str(step.get("uses", "")).startswith("actions/setup-python")]
        self.assertTrue(setups, "no explicit language setup action")
        for step in setups:
            with_block = step.get("with") or {}
            self.assertEqual(with_block.get("python-version-file"), ".python-version")
            self.assertNotIn("python-version", with_block, "no version literal in the YAML")

    def test_the_only_run_step_is_the_gate(self) -> None:
        ran = self._run_commands()
        self.assertEqual(len(ran), 1, "the workflow is allowed exactly one command")
        for command in ran:
            self.assertIn("scripts/ci/bench.sh", command)
            self.assertIn("--no-install", command)
            self.assertIn("--locked", command)
            for forbidden in ("pip install", "curl", "python", "jq", "|", "if ", "for "):
                self.assertNotIn(forbidden, command, f"logic in the YAML: {forbidden}")

    def test_the_reports_are_uploaded_under_the_names_the_gate_prints(self) -> None:
        command = self._run_commands()[0]
        uploads = {
            str((step.get("with") or {}).get("name")): step
            for step in self._steps()
            if str(step.get("uses", "")).startswith("actions/upload-artifact")
        }
        self.assertEqual(set(uploads), {"bench-results-json", "bench-summary-md"})
        for name, step in uploads.items():
            named = f"--artifact-name {name}" in command or f"--markdown-artifact-name {name}" in command
            self.assertTrue(named, f"the gate is never told about the artifact {name!r}:\n{command}")
            self.assertEqual((step["with"] or {}).get("if-no-files-found"), "error")
        self.assertEqual(str(uploads["bench-results-json"]["with"]["path"]), "bench-results.json")
        self.assertEqual(str(uploads["bench-summary-md"]["with"]["path"]), "bench-summary.md")

    def test_the_comment_is_the_scripts_markdown_and_only_on_a_pull_request(self) -> None:
        command = self._run_commands()[0]
        markdown = re.search(r"--markdown-out (\S+)", command)
        assert markdown is not None
        comments = [step for step in self._steps() if "create-or-update-comment" in str(step.get("uses", ""))]
        self.assertEqual(len(comments), 1, "exactly one results comment")
        step = comments[0]
        self.assertEqual((step.get("with") or {}).get("body-file"), markdown.group(1))
        self.assertIn("pull_request", str(step.get("if", "")), "only a PR has somewhere to comment")

    def test_uploads_and_comment_survive_a_failing_gate(self) -> None:
        """The red run is the one worth reading, so its reports must still be posted."""
        for step in self._steps():
            interesting = "upload-artifact" in str(step.get("uses", "")) or "comment" in str(step.get("name", ""))
            if interesting:
                condition = str(step.get("if", "")).lower()
                self.assertTrue(
                    "always" in condition or "cancelled" in condition,
                    f"{step.get('name')}: skipped when the gate fails",
                )

    def test_no_job_needs_a_secret(self) -> None:
        text = self.path.read_text()
        self.assertNotIn("secrets.", text)
        self.assertEqual(self.doc.get("permissions"), {"contents": "read", "pull-requests": "write"})


if __name__ == "__main__":
    unittest.main()
