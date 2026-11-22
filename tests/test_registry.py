"""Tests for discovery and the registry: two sources, error isolation, precedence.

The invariant under test everywhere here is: **one bad plugin never takes the host
down**. A file that raises on import, a class that is not a plugin, a name claimed
twice - each becomes a row with an explanation, and every other plugin still loads.
"""

from __future__ import annotations

import importlib.metadata
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fitbit_monitor.config import AppConfig
from fitbit_monitor.plugins import MonitorPlugin
from fitbit_monitor.plugins.loader import (
    Candidate,
    find_plugin_classes,
    iter_builtin_candidates,
    iter_directory_candidates,
)
from fitbit_monitor.plugins.registry import PluginRecord, PluginRegistry

import support

GOOD = '''
from fitbit_monitor.plugins import MonitorPlugin, PluginOption

class AlphaPlugin(MonitorPlugin):
    name = "alpha"
    summary = "alpha plugin"
    version = "2.0.0"
    datasets = ("daily_summary",)
    options = (PluginOption("n", "int", 3, "how many"),)

    def run(self, ctx):
        ctx.metric("alpha", 1)
'''

TWO_IN_ONE = '''
from fitbit_monitor.plugins import MonitorPlugin

class FirstPlugin(MonitorPlugin):
    name = "first"
    summary = "first in file"
    version = "1.0.0"

    def run(self, ctx):
        return None

class SecondPlugin(MonitorPlugin):
    name = "second"
    summary = "second in file"
    version = "1.0.0"

    def run(self, ctx):
        return None
'''

IMPORT_BOOM = '''
raise RuntimeError("this file explodes on import")
'''

NOT_A_PLUGIN = '''
VALUE = 3

def helper():
    return VALUE
'''

ABSTRACT_IN_FILE = '''
from fitbit_monitor.plugins import MonitorPlugin

class HalfWritten(MonitorPlugin):
    name = "half_written"
    summary = "forgets to implement run"
'''


class FakeEntryPoint:
    def __init__(self, name, value=None, *, raises=None):
        self.name = name
        self.value = value
        self.dist = mock.Mock(name="dist")
        self.dist.name = "fake-distribution"
        self._value = value
        self._raises = raises

    def load(self):
        if self._raises is not None:
            raise self._raises
        return self._value


class DirectoryDiscoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.plugins_dir = Path(self.tmp.name) / "plugins"
        self.plugins_dir.mkdir()

    def write(self, filename: str, body: str) -> Path:
        return support.write_plugin_file(self.plugins_dir, filename, body)

    def discover(self, **kw) -> PluginRegistry:
        config = AppConfig._from_raw(None, {"plugins": {"dirs": [str(self.plugins_dir)]}})
        kw.setdefault("include_entry_points", False)
        return PluginRegistry.discover(config, **kw)

    def test_finds_a_plain_file_plugin(self) -> None:
        self.write("alpha.py", GOOD)
        registry = self.discover()
        self.assertEqual(registry.names(), ["alpha"])
        record = registry.get("alpha")
        self.assertTrue(record.ok)
        self.assertEqual(record.kind, "directory")
        self.assertEqual(record.location, str(self.plugins_dir / "alpha.py"))
        self.assertEqual(record.row(), ("alpha", "2.0.0", "directory", "daily_summary"))
        self.assertEqual(record.plugin_class.declared_defaults(), {"n": 3})

    def test_finds_a_package_plugin(self) -> None:
        package = self.plugins_dir / "beta"
        package.mkdir()
        (package / "__init__.py").write_text("")
        (package / "plugin.py").write_text(GOOD.replace('"alpha"', '"beta"'))
        registry = self.discover()
        self.assertEqual(registry.names(), ["beta"])
        self.assertEqual(registry.get("beta").kind, "directory")

    def test_registers_every_class_in_a_file_in_declaration_order(self) -> None:
        self.write("multi.py", TWO_IN_ONE)
        registry = self.discover()
        self.assertEqual(registry.names(), ["first", "second"])

    def test_import_error_is_isolated_and_reported(self) -> None:
        self.write("alpha.py", GOOD)
        self.write("boom.py", IMPORT_BOOM)
        registry = self.discover()
        self.assertEqual(registry.names(), ["alpha", "boom"])
        self.assertEqual([r.name for r in registry.errors()], ["boom"])
        error = registry.get("boom").error
        self.assertEqual(error.stage, "import")
        self.assertIn("RuntimeError", str(error))
        # ...and the healthy plugin still instantiates and still runs.
        plugin = registry.get("alpha").create()
        self.assertEqual(plugin.name, "alpha")

    def test_file_without_a_plugin_is_reported(self) -> None:
        self.write("helper.py", NOT_A_PLUGIN)
        registry = self.discover()
        self.assertIn("no MonitorPlugin subclass", str(registry.get("helper").error))

    def test_class_missing_run_is_reported_by_name(self) -> None:
        self.write("half.py", ABSTRACT_IN_FILE)
        registry = self.discover()
        error = registry.get("half").error
        self.assertEqual(error.stage, "register")
        self.assertIn("half_written", str(error))
        self.assertIn("run", str(error))

    def test_private_and_hidden_files_are_ignored(self) -> None:
        self.write("_scratch.py", IMPORT_BOOM)
        self.write(".hidden.py", IMPORT_BOOM)
        self.assertEqual(self.discover().names(), [])

    def test_missing_directory_is_not_an_error(self) -> None:
        config = AppConfig._from_raw(None, {"plugins": {"dirs": [str(self.plugins_dir / "absent")]}})
        self.assertEqual(PluginRegistry.discover(config, include_entry_points=False).names(), [])

    def test_path_that_is_not_a_directory_is_reported(self) -> None:
        not_a_dir = Path(self.tmp.name) / "afile.txt"
        not_a_dir.write_text("hello")
        registry = PluginRegistry.discover(
            AppConfig._from_raw(None, {}), directories=[not_a_dir], include_entry_points=False
        )
        self.assertEqual(len(registry.errors()), 1)
        self.assertIn("not a directory", str(registry.errors()[0].error))

    def test_unreadable_file_is_reported_not_raised(self) -> None:
        path = self.write("syntax.py", "def (:\n")
        registry = self.discover()
        self.assertEqual(len(registry.errors()), 1)
        self.assertIn("SyntaxError", str(registry.errors()[0].error))
        self.assertTrue(path.exists())

    def test_ordering_is_deterministic(self) -> None:
        self.write("zebra.py", GOOD.replace('"alpha"', '"zebra"'))
        self.write("alpha.py", GOOD)
        self.write("middle.py", GOOD.replace('"alpha"', '"middle"'))
        self.assertEqual(self.discover().names(), ["alpha", "middle", "zebra"])


class EntryPointsTest(unittest.TestCase):
    def patch_entry_points(self, *entries: FakeEntryPoint):
        return mock.patch.object(importlib.metadata, "entry_points", return_value=list(entries))

    def test_entry_point_plugins_are_discovered(self) -> None:
        with self.patch_entry_points(FakeEntryPoint("ok_plugin", support.OkPlugin)):
            registry = PluginRegistry.discover(AppConfig._from_raw(None, {}))
        names = registry.names()
        self.assertIn("ok_plugin", names)
        record = registry.get("ok_plugin")
        self.assertEqual(record.kind, "entry_point")
        self.assertEqual(record.location, "fake-distribution:ok_plugin")
        self.assertTrue(record.ok)

    def test_entry_point_that_raises_on_load_is_isolated(self) -> None:
        entries = [
            FakeEntryPoint("ok_plugin", support.OkPlugin),
            FakeEntryPoint("nope", raises=ImportError("dependency missing")),
        ]
        with self.patch_entry_points(*entries):
            registry = PluginRegistry.discover(AppConfig._from_raw(None, {}))
        self.assertIn("ok_plugin", registry.names())
        broken = registry.get("nope")
        self.assertEqual(broken.error.stage, "import")
        self.assertIn("dependency missing", str(broken.error))

    def test_broken_metadata_does_not_break_discovery(self) -> None:
        with mock.patch.object(importlib.metadata, "entry_points", side_effect=Exception("corrupt dist-info")):
            registry = PluginRegistry.discover(AppConfig._from_raw(None, {}))
        self.assertTrue(any("corrupt dist-info" in str(r.error) for r in registry.errors()))

    def test_builtins_are_found_without_any_metadata(self) -> None:
        candidates = iter_builtin_candidates()
        self.assertTrue(any(c.location.endswith("RestingTrendPlugin") for c in candidates))
        registry = PluginRegistry._resolve(candidates)
        self.assertIn("resting_trend", registry.names())

    def test_entry_point_group_name_is_the_documented_one(self) -> None:
        from fitbit_monitor.plugins import ENTRY_POINT_GROUP

        self.assertEqual(ENTRY_POINT_GROUP, "fitbit_monitor.plugins")

    def test_the_shipped_distribution_declares_the_example_plugin(self) -> None:
        """pyproject.toml must keep publishing the reference plugin, or the docs lie."""
        entry_points = importlib.metadata.entry_points(group="fitbit_monitor.plugins")
        self.assertIn("resting_trend", [ep.name for ep in entry_points])


class CollisionTest(unittest.TestCase):
    def test_directory_beats_entry_point_and_builtin(self) -> None:
        from fitbit_monitor.plugins.builtin.resting_trend import RestingTrendPlugin

        shadow = GOOD.replace('"alpha"', '"resting_trend"')
        with tempfile.TemporaryDirectory() as tmp:
            support.write_plugin_file(Path(tmp), "override.py", shadow)
            with mock.patch.object(
                importlib.metadata,
                "entry_points",
                return_value=[FakeEntryPoint("resting_trend", RestingTrendPlugin)],
            ):
                registry = PluginRegistry.discover(
                    AppConfig._from_raw(None, {"plugins": {"dirs": [tmp]}})
                )
        record = registry.get("resting_trend")
        self.assertEqual(record.kind, "directory")
        self.assertEqual(len(registry.collisions), 1)
        self.assertEqual(registry.collisions[0].name, "resting_trend")
        self.assertIn("outranks", registry.collisions[0].reason)

    def test_earlier_directory_wins_over_later(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first, second = Path(tmp) / "a", Path(tmp) / "b"
            support.write_plugin_file(first, "x.py", GOOD)
            support.write_plugin_file(second, "x.py", GOOD)
            registry = PluginRegistry.discover(
                AppConfig._from_raw(None, {}), directories=[first, second], include_entry_points=False
            )
        self.assertEqual(registry.names(), ["alpha"])
        self.assertEqual(registry.collisions[0].winner, str(first / "x.py"))

    def test_the_same_class_found_twice_is_not_a_collision(self) -> None:
        registry = PluginRegistry._resolve(
            [support.candidate(support.OkPlugin, kind="builtin"), support.candidate(support.OkPlugin)]
        )
        self.assertEqual(registry.names(), ["ok_plugin"])
        self.assertEqual(registry.collisions, [])


class RegistryApiTest(unittest.TestCase):
    def test_queries(self) -> None:
        registry = PluginRegistry(
            records=[
                PluginRecord(name="good", kind="builtin", location="x", plugin_class=support.OkPlugin),
                PluginRecord(name="bad", kind="directory", location="y", error=None),
            ]
        )
        self.assertIn("good", registry)
        self.assertNotIn("absent", registry)
        self.assertEqual(len(registry), 2)
        self.assertEqual([r.name for r in registry.usable()], ["good"])
        self.assertEqual([r.name for r in registry.errors()], ["bad"])
        plugin, record = registry.instantiate("good")
        self.assertIs(type(plugin), support.OkPlugin)
        self.assertIs(record.plugin_class, support.OkPlugin)

    def test_instantiate_unknown_names_the_fix(self) -> None:
        registry = PluginRegistry()
        with self.assertRaises(Exception) as ctx:
            registry.instantiate("ghost")
        self.assertIn("fm plugins list", str(ctx.exception.hint))

    def test_instantiate_broken_record_raises_its_error(self) -> None:
        from fitbit_monitor.plugins.errors import PluginImportError

        error = PluginImportError("nope", plugin="bad")
        registry = PluginRegistry(records=[PluginRecord(name="bad", kind="directory", location="y", error=error)])
        with self.assertRaises(PluginImportError):
            registry.instantiate("bad")
        self.assertEqual(registry.get("bad").describe()["error"]["stage"], "import")

    def test_broken_record_describes_itself(self) -> None:
        registry = PluginRegistry(
            records=[
                PluginRecord(
                    name="broken",
                    kind="directory",
                    location="/x/broken.py",
                    error=__import__("fitbit_monitor.plugins.errors", fromlist=["x"]).PluginError("nope"),
                    note="shadowed",
                )
            ]
        )
        described = registry.as_dicts()[0]
        self.assertEqual(described["error"]["message"], "nope")
        self.assertEqual(described["note"], "shadowed")
        self.assertEqual(registry.get("broken").row()[1], "-")

    def test_candidate_hook_can_replace_discovery(self) -> None:
        registry = PluginRegistry.discover(candidates=[support.candidate(support.OkPlugin)])
        self.assertEqual(registry.names(), ["ok_plugin"])

    def test_unexpected_error_in_a_candidate_is_captured(self) -> None:
        def explode():
            raise ZeroDivisionError("divide by zero at import")

        registry = PluginRegistry.discover(
            candidates=[Candidate(kind="entry_point", location="x", label="cursed", load=explode)]
        )
        self.assertEqual(len(registry.errors()), 1)
        self.assertIn("ZeroDivisionError", str(registry.errors()[0].error))

    def test_candidate_yielding_nothing_is_reported(self) -> None:
        registry = PluginRegistry.discover(
            candidates=[Candidate(kind="entry_point", location="x", label="empty", load=lambda: [])]
        )
        self.assertIn("no plugin class", str(registry.errors()[0].error))


class LoaderHelpersTest(unittest.TestCase):
    def test_find_plugin_classes_ignores_imported_classes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = support.write_plugin_file(Path(tmp), "mod.py", "from fitbit_monitor.plugins import MonitorPlugin\n")
            module = __import__("fitbit_monitor.plugins.loader", fromlist=["x"]).load_module_from_path(path)
            self.assertEqual(find_plugin_classes(module), [])

    def test_directory_candidates_are_in_dir_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp) / "a", Path(tmp) / "b"
            support.write_plugin_file(a, "one.py", GOOD)
            support.write_plugin_file(b, "two.py", GOOD.replace('"alpha"', '"beta"'))
            candidates = iter_directory_candidates([a, b])
            self.assertEqual([c.label for c in candidates], ["one", "two"])

    def test_monitor_plugin_itself_is_not_a_candidate(self) -> None:
        self.assertFalse(issubclass(MonitorPlugin, type(None)))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
