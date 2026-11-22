"""Tests for the plugin contract: registration validation and the context object.

Two acceptance criteria live here: a plugin that does not implement the required
interface fails to register *with a clear message*, and the context the plugin
receives is read-only with a scoped config.
"""

from __future__ import annotations

import logging
import unittest
from datetime import UTC, datetime, timedelta

from fitbit_monitor.config import PluginConfig
from fitbit_monitor.errors import ConfigError
from fitbit_monitor.plugins import (
    MIN_PLUGIN_API_VERSION,
    PLUGIN_API_VERSION,
    MonitorPlugin,
    PluginContext,
    PluginOption,
    Severity,
    validate_plugin_class,
)
from fitbit_monitor.plugins.errors import PluginRegistrationError, PluginTimeout
from fitbit_monitor.store.reader import ReadOnlyStore

import support

# --- the API version constant ------------------------------------------------
class ApiVersionTest(unittest.TestCase):
    def test_api_version_is_a_frozen_integer_constant(self) -> None:
        self.assertIsInstance(PLUGIN_API_VERSION, int)
        self.assertGreaterEqual(PLUGIN_API_VERSION, 1)
        self.assertLessEqual(MIN_PLUGIN_API_VERSION, PLUGIN_API_VERSION)

    def test_plugins_default_to_the_host_api(self) -> None:
        self.assertEqual(MonitorPlugin.requires_api, PLUGIN_API_VERSION)

    def test_api_version_is_importable_from_the_public_package(self) -> None:
        import fitbit_monitor.plugins as public

        self.assertIs(public.PLUGIN_API_VERSION, PLUGIN_API_VERSION)
        self.assertEqual(public.API_VERSION_STRING, f"1.{PLUGIN_API_VERSION}")


# --- registration ------------------------------------------------------------
class RegistrationTest(unittest.TestCase):
    def test_valid_plugin_passes_through(self) -> None:
        self.assertIs(validate_plugin_class(support.OkPlugin), support.OkPlugin)

    def test_missing_run_is_reported_by_name(self) -> None:
        class Incomplete(MonitorPlugin):
            name = "incomplete"

        with self.assertRaises(PluginRegistrationError) as ctx:
            validate_plugin_class(Incomplete)
        message = str(ctx.exception)
        self.assertIn("incomplete", message)
        self.assertIn("run", message)
        self.assertEqual(ctx.exception.stage, "register")

    def test_not_a_monitor_plugin(self) -> None:
        class NotAPlugin:
            name = "nope"

        with self.assertRaises(PluginRegistrationError) as ctx:
            validate_plugin_class(NotAPlugin)
        self.assertIn("MonitorPlugin", str(ctx.exception))

    def test_random_object_is_rejected(self) -> None:
        with self.assertRaises(PluginRegistrationError):
            validate_plugin_class("just a string")

    def test_missing_name_is_rejected(self) -> None:
        class Nameless(MonitorPlugin):
            def run(self, ctx):
                return None

        with self.assertRaises(PluginRegistrationError) as ctx:
            validate_plugin_class(Nameless)
        self.assertIn("name", str(ctx.exception))

    def test_malformed_name_is_rejected(self) -> None:
        for bad in ("Bad_Name", "1leading", "-dash", ""):
            with self.subTest(bad=bad):
                cls = type("Cls", (MonitorPlugin,), {"name": bad, "version": "1.0.0", "run": lambda self, ctx: None})
                cls.__abstractmethods__ = frozenset()
                with self.assertRaises(PluginRegistrationError):
                    validate_plugin_class(cls)

    def test_missing_version_is_rejected(self) -> None:
        cls = type("Cls", (MonitorPlugin,), {"name": "noversion", "version": "", "run": lambda self, ctx: None})
        with self.assertRaises(PluginRegistrationError):
            validate_plugin_class(cls)

    def test_newer_required_api_is_refused(self) -> None:
        cls = type(
            "Cls",
            (MonitorPlugin,),
            {
                "name": "from_the_future",
                "version": "1.0.0",
                "requires_api": PLUGIN_API_VERSION + 1,
                "run": lambda self, ctx: None,
            },
        )
        with self.assertRaises(PluginRegistrationError) as ctx:
            validate_plugin_class(cls)
        self.assertIn("plugin API", str(ctx.exception))

    def test_ancient_required_api_is_refused(self) -> None:
        cls = type(
            "Cls",
            (MonitorPlugin,),
            {
                "name": "ancient",
                "version": "1.0.0",
                "requires_api": MIN_PLUGIN_API_VERSION - 1,
                "run": lambda self, ctx: None,
            },
        )
        with self.assertRaises(PluginRegistrationError):
            validate_plugin_class(cls)

    def test_duplicate_options_rejected(self) -> None:
        cls = type(
            "Cls",
            (MonitorPlugin,),
            {
                "name": "dupes",
                "version": "1.0.0",
                "options": (PluginOption("a", "int", 1, "first"), PluginOption("a", "int", 2, "second")),
                "run": lambda self, ctx: None,
            },
        )
        with self.assertRaises(PluginRegistrationError):
            validate_plugin_class(cls)

    def test_bad_dataset_declaration(self) -> None:
        cls = type(
            "Cls",
            (MonitorPlugin,),
            {"name": "baddata", "version": "1.0.0", "datasets": (None,), "run": lambda self, ctx: None},
        )
        with self.assertRaises(PluginRegistrationError):
            validate_plugin_class(cls)

    def test_describe_reports_metadata(self) -> None:
        described = support.OkPlugin.describe()
        self.assertEqual(described["name"], "ok_plugin")
        self.assertEqual(described["version"], "1.2.3")
        self.assertEqual(described["datasets"], ["daily_summary"])
        self.assertEqual(described["options"][0]["name"], "threshold")
        self.assertEqual(support.OkPlugin.option_map()["threshold"].default, 60.0)
        self.assertEqual(support.OkPlugin.declared_defaults(), {"threshold": 60.0})


class PluginOptionTest(unittest.TestCase):
    def test_types_are_declared_and_enforced(self) -> None:
        with self.assertRaises(PluginRegistrationError):
            PluginOption("x", "percent", 1, "not a real type")
        with self.assertRaises(PluginRegistrationError):
            PluginOption("Bad Name", "int", 1, "bad name")

    def test_coercion(self) -> None:
        from datetime import timedelta

        self.assertEqual(PluginOption("a", "int", 1, "h").coerce("3"), 3)
        self.assertEqual(PluginOption("a", "float", 1.0, "h").coerce("2.5"), 2.5)
        self.assertTrue(PluginOption("a", "bool", False, "h").coerce("yes"))
        self.assertEqual(PluginOption("a", "str", "x", "h").coerce(7), "7")
        self.assertEqual(PluginOption("a", "duration", "1h", "h").coerce("90m"), timedelta(minutes=90))
        self.assertEqual(PluginOption("a", "list", [], "h").coerce("one"), ["one"])

    def test_choices_are_enforced(self) -> None:
        option = PluginOption("a", "str", "x", "h", choices=("x", "y"))
        self.assertEqual(option.coerce("y"), "y")
        with self.assertRaises(ConfigError):
            option.coerce("z")

    def test_required_option_without_value(self) -> None:
        option = PluginOption("a", "int", None, "h", required=True)
        with self.assertRaises(ConfigError):
            option.coerce(None)

    def test_as_dict_is_serialisable(self) -> None:
        from datetime import timedelta

        option = PluginOption("a", "duration", timedelta(hours=1), "h")
        self.assertEqual(option.as_dict()["default"], 3600.0)


# --- the context -------------------------------------------------------------
class ContextTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = __import__("tempfile").TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        from fitbit_monitor.store.writer import SeriesStore

        self.store_path = __import__("pathlib").Path(self.tmp.name) / "store.sqlite3"
        with SeriesStore(self.store_path) as store:
            store.put("daily_summary", support.utc(1), {"resting_hr": 60.0})
        self.store = ReadOnlyStore(self.store_path)
        self.addCleanup(self.store.close)
        self.ctx = PluginContext(plugin_name="probe", store=self.store, window=support.window())

    def test_exposes_only_reads_config_clock_logger_and_emission(self) -> None:
        self.assertIs(self.ctx.store, self.store)
        self.assertIsInstance(self.ctx.config, PluginConfig)
        self.assertIsInstance(self.ctx.log, logging.LoggerAdapter)
        self.assertEqual(self.ctx.log.logger.name, "fitbit_monitor.plugin.probe")
        self.assertIsInstance(self.ctx.now(), datetime)
        self.assertTrue(self.ctx.now().tzinfo is not None)
        for forbidden in ("execute", "put", "commit", "writer", "connection", "host"):
            self.assertFalse(hasattr(self.ctx, forbidden), forbidden)

    def test_store_from_context_cannot_write(self) -> None:
        import sqlite3

        with self.assertRaises(sqlite3.OperationalError):
            self.ctx.store.conn.execute("INSERT INTO series VALUES ('x','y','{}',NULL)")

    def test_metric_fills_plugin_window_and_timestamp(self) -> None:
        metric = self.ctx.metric("m", 1.5, unit="x")
        self.assertEqual(metric.plugin, "probe")
        self.assertEqual(metric.window_start, self.ctx.window.since)
        self.assertEqual(metric.window_end, self.ctx.window.until)
        self.assertIsNotNone(metric.produced_at)
        self.assertEqual(self.ctx.metrics, (metric,))

    def test_alert_fills_window(self) -> None:
        alert = self.ctx.alert("a", "title", "message", occurred_at=support.utc(3))
        self.assertEqual(alert.window_start, self.ctx.window.since)
        self.assertEqual(alert.severity, Severity.WARNING)
        self.assertEqual(self.ctx.alerts, (alert,))

    def test_invalidation_surfaces_at_the_emit_call(self) -> None:
        from fitbit_monitor.plugins.models import ValidationError

        with self.assertRaises(ValidationError):
            self.ctx.metric("bad", 1.0, confidence=2.0)

    def test_adopts_returned_models_only(self) -> None:
        from fitbit_monitor.plugins.models import Alert, MetricResult

        self.ctx.adopt([MetricResult(plugin="p", key="k", value=1, window_start=support.utc(1), window_end=support.utc(2))])
        self.ctx.adopt([Alert(plugin="p", key="k", title="t", message="m", occurred_at=support.utc(1))])
        self.assertEqual(len(self.ctx.metrics), 1)
        self.assertEqual(len(self.ctx.alerts), 1)
        with self.assertRaises(TypeError):
            self.ctx.adopt(["not a model"])

    def test_deadline_and_cancellation(self) -> None:
        past = PluginContext(
            plugin_name="probe",
            store=self.store,
            window=support.window(),
            deadline=datetime.now(UTC) - timedelta(seconds=1),
        )
        self.assertIsNotNone(past.deadline)
        self.assertLess(past.time_remaining(), 0)
        with self.assertRaises(PluginTimeout):
            past.check_timeout()

        cancelled = PluginContext(plugin_name="probe", store=self.store, window=support.window())
        self.assertIsNone(cancelled.time_remaining())
        self.assertFalse(cancelled.cancelled)
        cancelled.request_cancel()
        self.assertTrue(cancelled.cancelled)
        with self.assertRaises(PluginTimeout):
            cancelled.check_timeout()

    def test_clock_is_injectable(self) -> None:
        fixed = support.utc(4, 5)
        ctx = PluginContext(plugin_name="probe", store=self.store, window=support.window(), clock=lambda: fixed)
        self.assertEqual(ctx.now(), fixed)

    def test_as_dict_describes_the_context(self) -> None:
        payload = self.ctx.as_dict()
        self.assertEqual(payload["plugin"], "probe")
        self.assertEqual(payload["api_version"], PLUGIN_API_VERSION)
        self.assertIn("window", payload)

    def test_context_config_is_scoped_to_the_plugin(self) -> None:
        config = support.make_config({"mine": {"threshold": 42.0}, "theirs": {"threshold": 99.0}})
        mine = PluginContext(
            plugin_name="mine",
            store=self.store,
            window=support.window(),
            config=config.plugin_config("mine", {"threshold": 0.0}),
        )
        self.assertEqual(mine.config.get_float("threshold", 0.0), 42.0)
        self.assertIsNone(mine.config.get("theirs", None))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
