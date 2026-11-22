"""The tooling decisions of ADR-0001 are checked, not remembered.

Nothing here tests behaviour. Each test pins one line of the decision so that a later
ticket cannot quietly re-litigate it:

* the interpreter floor and the committed version pins (``.python-version``,
  ``uv.lock``) agree with each other and with ``requires-python``;
* the ``dev`` extra is exactly what the gate scripts call, and the runtime extra is
  still empty - "no dependencies" is a feature (ADR-0001 §2);
* the retired tool stays retired (pyflakes, §10);
* the ``scripts/dev`` entrypoints exist, are executable, and are documented (§12);
* the five gate entrypoints are *wrappers*: they call a ``scripts/ci`` gate with
  ``--no-install`` and carry its exit code, so no logic hides in a wrapper and no
  failure goes unreported (§12).

``uv`` is optional in a test environment, so the lockfile check that needs the binary
skips itself instead of failing a contributor who installed with pip.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

DEV_ENTRYPOINTS = [
    "bootstrap.sh",
    "lint.sh",
    "typecheck.sh",
    "test.sh",
    "coverage.sh",
    "bench.sh",
    "check.sh",
    "docs.sh",
]

#: The five the scaffold ticket names as the developer's surface: each must exist,
#: be executable, and exit nonzero when the gate underneath fails.
GATE_ENTRYPOINTS = ["test.sh", "lint.sh", "typecheck.sh", "coverage.sh", "bench.sh"]


def _pyproject() -> dict:
    return tomllib.loads((REPO / "pyproject.toml").read_text())


def _version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.strip().split(".")[:3] if part.strip().isdigit())


class VersionPinTest(unittest.TestCase):
    """`.python-version` and uv.lock are committed, and they agree with pyproject."""

    def test_python_version_file_lists_the_supported_band(self) -> None:
        raw = _pyproject()
        floor = raw["project"]["requires-python"].replace(">=", "").strip()
        pinned = (REPO / ".python-version").read_text().split()
        self.assertTrue(pinned, ".python-version is empty")
        for line in pinned:
            self.assertGreaterEqual(
                _version_tuple(line),
                _version_tuple(floor),
                f".python-version pins {line}, below requires-python {floor}",
            )

    def test_uv_lock_is_committed_and_agrees_with_pyproject(self) -> None:
        raw = _pyproject()
        lock_path = REPO / "uv.lock"
        self.assertTrue(lock_path.is_file(), "uv.lock is not committed")
        lock = lock_path.read_text()
        self.assertIn(
            f'requires-python = "{raw["project"]["requires-python"]}"',
            lock,
            "uv.lock was resolved for a different requires-python; run `uv lock`",
        )
        self.assertIn('name = "fitbit-monitor"', lock)

    @unittest.skipUnless(shutil.which("uv"), "uv is not installed in this environment")
    def test_uv_lock_is_current(self) -> None:
        result = subprocess.run(
            ["uv", "lock", "--check"],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"uv.lock is stale - run `uv lock` and commit it\n{result.stdout}{result.stderr}",
        )


class DependencyPolicyTest(unittest.TestCase):
    """Runtime stays empty; the dev extra is what the gates call."""

    def test_runtime_dependencies_are_empty(self) -> None:
        raw = _pyproject()
        self.assertEqual(raw["project"].get("dependencies", []), [])

    def test_dev_extra_installs_every_tool_the_gates_call(self) -> None:
        dev = _pyproject()["project"]["optional-dependencies"]["dev"]
        names = [entry.split("[")[0].split(">")[0].split("=")[0].strip() for entry in dev]
        for tool in ("pytest", "pytest-cov", "coverage", "mypy", "ruff", "pyyaml"):
            self.assertIn(tool, names)

    def test_retired_tools_stay_retired(self) -> None:
        dev = " ".join(_pyproject()["project"]["optional-dependencies"]["dev"])
        self.assertNotIn("pyflakes", dev)
        gate = REPO / "scripts" / "ci" / "lint.sh"
        code = [line for line in gate.read_text().splitlines() if not line.lstrip().startswith("#")]
        self.assertNotIn("pyflakes", " ".join(code), "lint.sh may explain pyflakes, not run it")

    def test_ruff_config_is_committed_and_tier_one(self) -> None:
        raw = _pyproject()
        lint = raw["tool"]["ruff"]["lint"]
        self.assertEqual(sorted(lint["select"]), sorted(["E4", "E7", "E9", "W", "F"]))
        self.assertEqual(raw["tool"]["ruff"]["target-version"], "py311")


class BenchmarkRecordTest(unittest.TestCase):
    """benchmarks/baseline.json is the reviewable half of the perf gate (ADR-0001 §9)."""

    def test_baseline_names_a_unit_and_a_direction_for_every_case(self) -> None:
        baseline = json.loads((REPO / "benchmarks" / "baseline.json").read_text())
        for key in ("cases", "units", "direction", "recorded_utc", "days"):
            self.assertIn(key, baseline)
        self.assertEqual(set(baseline["cases"]), set(baseline["units"]))
        self.assertEqual(set(baseline["cases"]), set(baseline["direction"]))
        for name, value in baseline["cases"].items():
            self.assertGreater(value, 0, f"{name}: a baseline of zero can never be beaten")
            self.assertIn(baseline["direction"][name], ("higher", "lower"))

    def test_baseline_records_how_noisy_the_recording_was_and_who_made_it(self) -> None:
        """A single number per case is not enough to review: how many samples, how wide,
        and on what machine, or a re-record is unverifiable (ADR-0001 §9 amendment)."""
        baseline = json.loads((REPO / "benchmarks" / "baseline.json").read_text())
        self.assertIn("stats", baseline)
        self.assertEqual(set(baseline["stats"]), set(baseline["cases"]))
        for name, stats in baseline["stats"].items():
            for key in ("mean", "stddev", "iterations", "best", "worst"):
                self.assertIn(key, stats, f"{name}: {key}")
            self.assertGreaterEqual(stats["iterations"], 1)
            self.assertGreaterEqual(stats["stddev"], 0.0)
        self.assertIn("environment", baseline)
        self.assertRegex(str(baseline["environment"]["python"]), r"^\d+\.\d+")
        self.assertGreater(baseline["tolerance_percent"], 0.0)


class DevEntrypointTest(unittest.TestCase):
    """scripts/dev is the one place a contributor learns the dev commands from."""

    def test_entrypoints_exist_and_are_executable(self) -> None:
        for name in DEV_ENTRYPOINTS:
            path = REPO / "scripts" / "dev" / name
            self.assertTrue(path.is_file(), f"scripts/dev/{name} is missing")
            self.assertTrue(os.access(path, os.X_OK), f"scripts/dev/{name} is not executable")
            self.assertTrue(path.read_text().startswith("#!"), f"scripts/dev/{name} has no shebang")

    def test_every_script_resolves_its_interpreter_in_one_place(self) -> None:
        """No script may guess at `python3`; ADR-0001 §12 routes them all through one file."""
        scripts = sorted((REPO / "scripts" / "ci").glob("*.sh")) + sorted(
            (REPO / "scripts" / "dev").glob("*.sh")
        )
        self.assertGreaterEqual(len(scripts), 10)
        for script in scripts:
            self.assertIn("scripts/_python.sh", script.read_text(), f"{script} picks its own python")

    def test_readme_names_every_dev_entrypoint(self) -> None:
        readme = (REPO / "README.md").read_text()
        for name in DEV_ENTRYPOINTS:
            self.assertIn(f"scripts/dev/{name}", readme, f"README.md does not document scripts/dev/{name}")

    def test_readme_documents_the_two_install_paths(self) -> None:
        readme = (REPO / "README.md").read_text()
        self.assertIn("uv sync", readme)
        self.assertIn('pip install -e ".[dev]"', readme)

    def test_adr_0001_records_the_decision(self) -> None:
        adr = REPO / "docs" / "adr" / "0001-implementation-stack.md"
        self.assertTrue(adr.is_file(), "docs/adr/0001-implementation-stack.md is missing")
        text = adr.read_text()
        self.assertIn("- **Status:** accepted", text)
        for topic in ("uv", "sqlite3", "pytest", "coverage", "ruff", "mypy", "argparse", "Rejected"):
            self.assertIn(topic, text)


class GateWrapperTest(unittest.TestCase):
    """A ``scripts/dev`` gate wrapper has exactly two jobs: call the gate, carry the code.

    The wrapper is run against a stand-in interpreter that just records what it was
    asked to do and exits on command. That checks the two properties a wrapper must
    have - it really delegates to a ``scripts/ci`` gate, and a failing gate fails the
    wrapper - without waiting on the real toolchain, and without trusting the wrapper
    to say so in a comment.
    """

    def _wrapper(self, script: str) -> Path:
        return REPO / "scripts" / "dev" / script

    def _run_stubbed(self, script: str, exit_code: int) -> tuple[subprocess.CompletedProcess, list[str]]:
        """Run scripts/dev/{script} with $PYTHON replaced by a recording stub.

        Returns the process and the list of command lines the gates actually asked the
        interpreter to run - the evidence that the wrapper delegates.
        """
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        trace = Path(tmp.name) / "calls.log"
        stub = Path(tmp.name) / "python"
        stub.write_text(f"#!/usr/bin/env bash\necho \"$*\" >> {trace}\nexit {exit_code}\n")
        stub.chmod(0o755)
        env = dict(os.environ, PYTHON=str(stub))
        result = subprocess.run(
            [str(self._wrapper(script))],
            cwd=REPO,
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )
        calls = [line for line in trace.read_text().splitlines() if line.strip()] if trace.exists() else []
        return result, calls

    def test_gate_entrypoints_delegate_to_a_ci_gate_without_installing(self) -> None:
        """Every gate wrapper names one scripts/ci gate and passes --no-install."""
        for name in GATE_ENTRYPOINTS:
            text = self._wrapper(name).read_text()
            gates = sorted(set(re.findall(r"scripts/ci/[a-z_]+\.sh", text)))
            self.assertEqual(len(gates), 1, f"{name}: expected one gate underneath, found {gates}")
            gate = REPO / gates[0]
            self.assertTrue(gate.is_file(), f"{name} calls {gates[0]}, which does not exist")
            self.assertTrue(os.access(gate, os.X_OK), f"{gates[0]} is not executable")
            self.assertIn("--no-install", text, f"{name} would re-install its own toolchain")

    def test_wrapper_exits_zero_when_the_gate_passes(self) -> None:
        for name in GATE_ENTRYPOINTS:
            with self.subTest(script=name):
                result, calls = self._run_stubbed(name, exit_code=0)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertTrue(calls, f"{name} finished without running a gate at all")

    def test_wrapper_exits_nonzero_when_the_gate_fails(self) -> None:
        """A failing gate has to fail the wrapper - a silent green is the worst outcome."""
        for name in GATE_ENTRYPOINTS:
            with self.subTest(script=name):
                result, _ = self._run_stubbed(name, exit_code=1)
                self.assertNotEqual(result.returncode, 0, f"{name} swallowed a failing gate")

    def test_gate_runs_the_documented_tool_and_nothing_private(self) -> None:
        """The recorded command lines are the tools the ADR names - not a private copy."""
        expected = {
            "typecheck.sh": ["-m mypy"],
            "lint.sh": ["-m mypy", "-m ruff check"],
            "coverage.sh": ["-m unittest", "-m coverage run", "-m coverage report", "-m coverage xml"],
            "test.sh": ["-m unittest", "-m coverage run"],
            "bench.sh": ["scripts/bench/bench_plugin_system.py"],
        }
        for name, needles in expected.items():
            with self.subTest(script=name):
                _, calls = self._run_stubbed(name, exit_code=0)
                joined = "\n".join(calls)
                for needle in needles:
                    self.assertIn(needle, joined, f"{name} did not run `{needle}`:\n{joined}")


if __name__ == "__main__":
    unittest.main()
