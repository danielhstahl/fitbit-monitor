"""Tests for ``fm plugin new``: the scaffold must load, run, install, and pass the suite.

A scaffold that does not run on the first try teaches the wrong lesson, so these
tests do exactly what the generated README tells the author to do: point a plugins
directory at the project root, run the plugin, run the generated contract test in a
subprocess the way a terminal would, and check that the entry-point declaration the
scaffold writes is the one a build would accept (declared package on disk, module
and attribute that resolve).
"""

from __future__ import annotations

import importlib
import importlib.util
import io
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

from fitbit_monitor.errors import UsageError
from fitbit_monitor.plugins import ENTRY_POINT_GROUP
from fitbit_monitor.plugins.contract import validate_plugin_class
from fitbit_monitor.plugins.registry import PluginRegistry
from fitbit_monitor.plugins.runner import STATUS_OK, run_plugins
from fitbit_monitor.plugins.scaffold import scaffold_plugin
from fitbit_monitor.plugins.testing import fixture_store, fixture_window
from fitbit_monitor.store.reader import ReadOnlyStore

import support

REPO = Path(__file__).resolve().parents[1]
GENERATED_FILES = ("pyproject.toml", "README.md", "{p}/__init__.py", "{p}/plugin.py", "tests/__init__.py", "tests/test_{p}.py")


def generated_files(name: str = "resting_avg") -> tuple[str, ...]:
    return tuple(template.format(p=name) for template in GENERATED_FILES)


def _env_with_repo() -> dict[str, str]:
    """A subprocess environment that can import this checkout and the scaffold."""
    import os

    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(REPO / "src") + (os.pathsep + existing if existing else "")
    env.pop("FM_CONFIG", None)
    env.pop("FM_PLUGIN_DIRS", None)
    return env


def _forget_package(package: str) -> None:
    """Drop a scaffolded package from the import cache, so the next test is clean.

    These tests import the scaffolded package from a temp directory, twice, from two
    different temp directories. Anything left in ``sys.modules`` would shadow the next
    one, so a leak here reads as "`plugin` is missing from the package".
    """
    for name in [n for n in sys.modules if n == package or n.startswith(package + ".")]:
        sys.modules.pop(name, None)


class ScaffoldTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store_path = fixture_store(self.root)

    def scaffold(self, name="resting_avg", **kw):
        return scaffold_plugin(name, self.root, **kw)

    def read(self, result, relative: str) -> str:
        return (result.directory / Path(relative)).read_text(encoding="utf-8")

    def pyproject(self, result) -> dict:
        return tomllib.loads(self.read(result, "pyproject.toml"))

    # --- writing ------------------------------------------------------------
    def test_writes_the_expected_files(self) -> None:
        result = self.scaffold()
        self.assertEqual(result.files, generated_files())
        for filename in result.files:
            self.assertTrue((result.directory / filename).is_file(), filename)
        self.assertIn("resting_avg", self.read(result, "resting_avg/plugin.py").splitlines()[0])
        self.assertEqual(result.directory, self.root / "resting_avg")
        self.assertEqual(result.package, "resting_avg")

    def test_no_stray_module_at_the_project_root(self) -> None:
        """The project root must hold exactly one plugin candidate: <pkg>/plugin.py.

        Discovery reads every ``*.py`` in a plugins directory, so a stray file here
        (a root-level test module, a conftest) would show up as a broken plugin.
        """
        result = self.scaffold()
        self.assertEqual(sorted(p.name for p in result.directory.glob("*.py")), [])

    def test_dry_run_lists_the_files_and_writes_nothing(self) -> None:
        result = self.scaffold(dry_run=True)
        self.assertEqual(result.files, generated_files())
        self.assertTrue(result.dry_run)
        self.assertTrue(result.as_dict()["dry_run"])
        self.assertFalse(result.directory.exists(), "--dry-run must not touch the disk")
        # ... and it still refuses a name that cannot be an identifier.
        with self.assertRaises(UsageError):
            scaffold_plugin("Bad-Name", self.root, dry_run=True)

    def test_refuses_to_clobber_and_honours_force(self) -> None:
        self.scaffold()
        with self.assertRaises(UsageError):
            self.scaffold()
        again = self.scaffold(force=True)
        self.assertTrue(again.directory.exists())

    def test_an_existing_empty_directory_is_written_into(self) -> None:
        """`--force` exists for a non-empty one; an empty target is never a problem."""
        (self.root / "resting_avg").mkdir()
        result = self.scaffold()
        self.assertTrue((result.directory / "resting_avg" / "plugin.py").is_file())

    def test_rejects_names_that_cannot_be_identifiers(self) -> None:
        for bad in ("Bad-Name", "1nvalid", "a", "with space", "hy-phen"):
            with self.subTest(bad=bad):
                with self.assertRaises(UsageError):
                    scaffold_plugin(bad, self.root)

    def test_class_name_and_options_come_from_the_name(self) -> None:
        result = scaffold_plugin("spo2_low", self.root, dataset="spo2", field="spo2_min")
        text = self.read(result, "spo2_low/plugin.py")
        self.assertIn("class Spo2LowPlugin(MonitorPlugin):", text)
        self.assertIn('name = "spo2_low"', text)
        self.assertIn('datasets = ("spo2",)', text)
        self.assertIn("Spo2LowPlugin", self.read(result, "spo2_low/__init__.py"))

    # --- the entry-point declaration ---------------------------------------
    def test_pyproject_declares_the_entry_point_group(self) -> None:
        raw = self.pyproject(self.scaffold(name="my_metric"))
        group = raw["project"]["entry-points"]["fitbit_monitor.plugins"]
        self.assertEqual(group["my_metric"], "my_metric.plugin:MyMetricPlugin")
        self.assertEqual(raw["build-system"]["build-backend"], "setuptools.build_meta")

    def test_pyproject_names_where_the_host_comes_from(self) -> None:
        """A plugin distribution must say what `fitbit-monitor` it needs."""
        result = self.scaffold()
        raw = self.pyproject(result)
        self.assertIn("dependencies", raw["project"], "no dependency table at all")
        self.assertIn("fitbit-monitor", self.read(result, "pyproject.toml"))

    def test_declared_packages_exist_where_a_build_would_look(self) -> None:
        """`pip install .` fails with "package directory 'x' does not exist" if not."""
        result = self.scaffold(name="my_metric")
        raw = self.pyproject(result)
        for package in raw["tool"]["setuptools"]["packages"]:
            directory = result.directory / Path(*package.split("."))
            self.assertTrue((directory / "__init__.py").is_file(), f"{package} is not a package on disk")

    def test_entry_point_value_resolves_to_the_plugin_class(self) -> None:
        """The same load `importlib.metadata` performs once the project is installed."""
        result = self.scaffold(name="my_metric")
        value = self.pyproject(result)["project"]["entry-points"]["fitbit_monitor.plugins"]["my_metric"]
        module_name, _, attribute = value.partition(":")
        sys.path.insert(0, str(result.directory))
        self.addCleanup(sys.path.remove, str(result.directory))
        self.addCleanup(_forget_package, module_name.split(".")[0])
        loaded = getattr(importlib.import_module(module_name), attribute)
        self.assertIs(validate_plugin_class(loaded, source="scaffold entry point"), loaded)
        self.assertEqual(loaded.name, "my_metric")

    def test_generated_test_file_imports_the_installed_module_path(self) -> None:
        text = self.read(self.scaffold(name="my_metric"), "tests/test_my_metric.py")
        self.assertIn("from my_metric.plugin import MyMetricPlugin", text)
        self.assertIn("PluginContractTestCase", text)

    def test_the_declared_entry_point_is_how_an_installed_plugin_is_found(self) -> None:
        """The pyproject the scaffold writes must survive real metadata discovery.

        A build turns those TOML tables into a `.dist-info`; this writes the same
        metadata by hand - no network, no build backend - and hands it to the real
        discovery code. So a wrong group name, a `<pkg>.<module>:<Class>` value that
        does not resolve, or a package that is not importable from the project root
        each fail here instead of in a stranger's terminal.
        """
        result = self.scaffold(name="my_metric")
        raw = self.pyproject(result)
        groups = raw["project"]["entry-points"]
        self.assertEqual(list(groups), [ENTRY_POINT_GROUP], "wrong entry-point group")
        distribution = raw["project"]["name"]
        site = self.root / "site"
        dist_info = site / f"{distribution.replace('-', '_')}-0.1.0.dist-info"
        dist_info.mkdir(parents=True)
        (dist_info / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: {distribution}\nVersion: 0.1.0\n", encoding="utf-8"
        )
        lines = "\n".join(f"{name} = {value}" for name, value in groups[ENTRY_POINT_GROUP].items())
        (dist_info / "entry_points.txt").write_text(f"[{ENTRY_POINT_GROUP}]\n{lines}\n", encoding="utf-8")
        for path in (site, result.directory):
            sys.path.insert(0, str(path))
            self.addCleanup(sys.path.remove, str(path))
        self.addCleanup(_forget_package, "my_metric")

        registry = PluginRegistry.discover(support.make_config(), directories=[], include_entry_points=True)
        self.assertIn("my_metric", registry.names())
        record = registry.get("my_metric")
        self.assertEqual(record.kind, "entry_point")
        self.assertIsNotNone(record.plugin_class, f"the declared entry point did not load: {record.error}")
        self.assertIs(validate_plugin_class(record.plugin_class, source="installed"), record.plugin_class)
        self.assertEqual(registry.errors(), [])

    def test_readme_documents_both_ship_paths(self) -> None:
        result = self.scaffold()
        text = self.read(result, "README.md")
        self.assertIn("fm plugins list --dir .", text)
        self.assertIn("pip install -e .", text)
        self.assertIn("PluginContractTestCase", text)
        self.assertIn("python -m pytest", text)

    def test_readme_tree_names_every_file_it_wrote(self) -> None:
        """The first thing an author reads has to describe what is on disk."""
        result = self.scaffold(name="my_metric")
        text = self.read(result, "README.md")
        for relative in result.files:
            self.assertIn(Path(relative).name, text, f"README.md does not mention {relative}")

    # --- the scaffolded plugin actually works -------------------------------
    def test_discovered_from_a_plugins_directory_and_runs(self) -> None:
        result = self.scaffold()
        registry = PluginRegistry.discover(
            support.make_config(), directories=[result.directory], include_entry_points=False
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

    def test_single_file_drop_in_works_too(self) -> None:
        """The README promises `plugin.py` is standalone; prove the copy works."""
        result = self.scaffold()
        drop = self.root / "drop"
        drop.mkdir()
        (drop / "resting_avg.py").write_text(self.read(result, "resting_avg/plugin.py"), encoding="utf-8")
        registry = PluginRegistry.discover(
            support.make_config(), directories=[drop], include_entry_points=False
        )
        self.assertEqual(registry.names(), ["resting_avg"])
        self.assertEqual(registry.errors(), [])

    def test_scaffolded_config_options_are_declared_and_typed(self) -> None:
        self.scaffold()
        registry = PluginRegistry.discover(
            support.make_config(), directories=[self.root / "resting_avg"], include_entry_points=False
        )
        options = {opt.name: opt for opt in registry.get("resting_avg").plugin_class.options}
        self.assertEqual(set(options), {"dataset", "field", "min_days"})
        self.assertEqual(options["min_days"].type, "int")

    def test_generated_test_file_passes_the_shared_contract_suite(self) -> None:
        """Run the generated tests the way the author will, inside this process."""
        result = self.scaffold()
        project = result.directory
        sys.path.insert(0, str(project))
        self.addCleanup(sys.path.remove, str(project))
        self.addCleanup(_forget_package, "resting_avg")
        spec = importlib.util.spec_from_file_location(
            "generated_contract_test", project / "tests" / "test_resting_avg.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        self.addCleanup(sys.modules.pop, spec.name, None)
        spec.loader.exec_module(module)
        suite = unittest.TestLoader().loadTestsFromModule(module)
        captured = io.StringIO()
        outcome = unittest.TextTestRunner(stream=captured, verbosity=0).run(suite)
        self.assertGreater(outcome.testsRun, 5, "the generated file should run the whole contract suite")
        self.assertTrue(outcome.wasSuccessful(), captured.getvalue())

    def _run(self, project: Path, *argv: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, *argv],
            cwd=project,
            env=_env_with_repo(),
            capture_output=True,
            text=True,
            timeout=300,
        )

    def test_generated_tests_run_with_pytest_from_the_project_root(self) -> None:
        if importlib.util.find_spec("pytest") is None:  # pragma: no cover - the gate installs it
            self.skipTest("pytest is not installed")
        result = self.scaffold(name="my_metric")
        done = self._run(result.directory, "-m", "pytest", "-q")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("passed", done.stdout)

    def test_generated_tests_run_with_unittest_from_the_project_root(self) -> None:
        result = self.scaffold(name="my_metric")
        done = self._run(result.directory, "-m", "unittest", "discover", "-s", "tests", "-t", ".")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("OK", done.stderr + done.stdout)

    def test_scaffold_result_describes_itself(self) -> None:
        payload = self.scaffold().as_dict()
        self.assertEqual(payload["name"], "resting_avg")
        self.assertEqual(payload["package"], "resting_avg")
        self.assertEqual(payload["files"], list(generated_files()))
        self.assertFalse(payload["dry_run"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
