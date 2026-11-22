"""Tests for ``fm plugin new``: the scaffold must load, run, and pass the suite.

A scaffold that does not run on the first try teaches the wrong lesson, so these
tests do exactly what the generated README tells the author to do: point a plugins
directory at it, run it, and run the generated contract test.
"""

from __future__ import annotations

import importlib.util
import io
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

from fitbit_monitor.errors import UsageError
from fitbit_monitor.plugins.registry import PluginRegistry
from fitbit_monitor.plugins.runner import STATUS_OK, run_plugins
from fitbit_monitor.plugins.scaffold import scaffold_plugin
from fitbit_monitor.plugins.testing import fixture_store, fixture_window
from fitbit_monitor.store.reader import ReadOnlyStore

import support


class ScaffoldTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store_path = fixture_store(self.root)

    def scaffold(self, name="resting_avg", **kw):
        return scaffold_plugin(name, self.root, **kw)

    # --- writing ------------------------------------------------------------
    def test_writes_the_expected_files(self) -> None:
        result = self.scaffold()
        self.assertEqual(
            result.files, ("README.md", "plugin.py", "pyproject.toml", "test_resting_avg.py")
        )
        for filename in result.files:
            self.assertTrue((result.directory / filename).exists(), filename)
        self.assertIn("resting_avg", (result.directory / "plugin.py").read_text().splitlines()[0])

    def test_refuses_to_clobber_and_honours_force(self) -> None:
        self.scaffold()
        with self.assertRaises(UsageError):
            self.scaffold()
        again = self.scaffold(force=True)
        self.assertTrue(again.directory.exists())

    def test_rejects_names_that_cannot_be_identifiers(self) -> None:
        for bad in ("Bad-Name", "1nvalid", "a", "with space"):
            with self.subTest(bad=bad):
                with self.assertRaises(UsageError):
                    scaffold_plugin(bad, self.root)

    def test_class_name_and_options_come_from_the_name(self) -> None:
        result = scaffold_plugin("spo2_low", self.root, dataset="spo2", field="spo2_min")
        text = (result.directory / "plugin.py").read_text()
        self.assertIn("class Spo2LowPlugin(MonitorPlugin):", text)
        self.assertIn('name = "spo2_low"', text)
        self.assertIn('datasets = ("spo2",)', text)

    def test_pyproject_declares_the_entry_point_group(self) -> None:
        result = self.scaffold(name="my_metric")
        raw = tomllib.loads((result.directory / "pyproject.toml").read_text())
        group = raw["project"]["entry-points"]["fitbit_monitor.plugins"]
        self.assertEqual(group["my_metric"], "my_metric.plugin:MyMetricPlugin")
        self.assertIn("fitbit-monitor", raw["project"]["dependencies"])

    def test_readme_documents_both_ship_paths(self) -> None:
        text = (self.scaffold().directory / "README.md").read_text()
        self.assertIn("fm plugins list --dir", text)
        self.assertIn("pip install -e .", text)
        self.assertIn("PluginContractTestCase", text)

    # --- the scaffolded plugin actually works -------------------------------
    def test_discovered_from_a_plugins_directory_and_runs(self) -> None:
        self.scaffold()
        registry = PluginRegistry.discover(
            support.make_config(), directories=[self.root], include_entry_points=False
        )
        self.assertEqual(registry.names(), ["resting_avg"])
        self.assertEqual(registry.errors(), [])
        store = ReadOnlyStore(self.store_path)
        self.addCleanup(store.close)
        report = run_plugins(
            registry,
            store,
            fixture_window(),
            config=support.make_config({"resting_avg": {"field": "resting_hr"}}),
        )
        outcome = report.outcomes[0]
        self.assertEqual(outcome.status, STATUS_OK, outcome.error)
        self.assertEqual(outcome.metrics[0].key, "resting_avg")
        self.assertEqual(outcome.metrics[0].details["field"], "resting_hr")

    def test_scaffolded_config_options_are_declared_and_typed(self) -> None:
        self.scaffold()
        registry = PluginRegistry.discover(
            support.make_config(), directories=[self.root], include_entry_points=False
        )
        options = {opt.name: opt for opt in registry.get("resting_avg").plugin_class.options}
        self.assertEqual(set(options), {"dataset", "field", "min_days"})
        self.assertEqual(options["min_days"].type, "int")

    def test_generated_test_file_passes_the_shared_contract_suite(self) -> None:
        """Run the generated tests the way the author will, inside this process."""
        result = self.scaffold()
        plugin_dir = result.directory
        sys.path.insert(0, str(plugin_dir))
        self.addCleanup(sys.path.remove, str(plugin_dir))
        spec = importlib.util.spec_from_file_location("generated_contract_test", plugin_dir / "test_resting_avg.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        self.addCleanup(sys.modules.pop, spec.name, None)
        spec.loader.exec_module(module)
        suite = unittest.TestLoader().loadTestsFromModule(module)
        captured = io.StringIO()
        outcome = unittest.TextTestRunner(stream=captured, verbosity=0).run(suite)
        self.assertGreater(outcome.testsRun, 5, "the generated file should run the whole contract suite")
        self.assertTrue(outcome.wasSuccessful(), captured.getvalue())

    def test_scaffold_result_describes_itself(self) -> None:
        payload = self.scaffold().as_dict()
        self.assertEqual(payload["name"], "resting_avg")
        self.assertIn("plugin.py", payload["files"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
