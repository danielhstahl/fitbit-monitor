"""The tooling decisions of ADR-0001 are checked, not remembered.

Nothing here tests behaviour. Each test pins one line of the decision so that a later
ticket cannot quietly re-litigate it:

* the interpreter floor and the committed version pins (``.python-version``,
  ``uv.lock``) agree with each other and with ``requires-python``;
* the ``dev`` extra is exactly what the gate scripts call, and the runtime extra is
  still empty - "no dependencies" is a feature (ADR-0001 §2);
* the retired tool stays retired (pyflakes, §10);
* the ``scripts/dev`` entrypoints exist, are executable, and are documented (§12).

``uv`` is optional in a test environment, so the lockfile check that needs the binary
skips itself instead of failing a contributor who installed with pip.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tomllib
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

DEV_ENTRYPOINTS = ["bootstrap.sh", "lint.sh", "test.sh", "bench.sh", "check.sh", "docs.sh"]


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


class DevEntrypointTest(unittest.TestCase):
    """scripts/dev is the one place a contributor learns the dev commands from."""

    def test_entrypoints_exist_and_are_executable(self) -> None:
        for name in DEV_ENTRYPOINTS:
            path = REPO / "scripts" / "dev" / name
            self.assertTrue(path.is_file(), f"scripts/dev/{name} is missing")
            self.assertTrue(os.access(path, os.X_OK), f"scripts/dev/{name} is not executable")
            self.assertTrue(path.read_text().startswith("#!"), f"scripts/dev/{name} has no shebang")

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


if __name__ == "__main__":
    unittest.main()
