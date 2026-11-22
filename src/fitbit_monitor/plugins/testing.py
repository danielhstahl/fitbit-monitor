"""Test-support module published to plugin authors (public API, not internals).

Third-party plugins inherit the same conformance suite the built-ins run in CI, so
"works on my machine" and "passes the contract" mean the same thing:

.. code-block:: python

    from fitbit_monitor.plugins.testing import PluginContractTestCase
    from myplugin import MyPlugin

    class TestMyPlugin(PluginContractTestCase):
        plugin_cls = MyPlugin

The suite needs no fixtures of its own: it builds a deterministic seeded store
(:func:`fixture_store`) and a fixed window, so it behaves the same in CI, in a
wheel, and on a laptop with no Fitbit account.
"""

from __future__ import annotations

import re
import sqlite3
import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

from ..config import AppConfig, PluginConfig
from .contract import PLUGIN_API_VERSION, MonitorPlugin, validate_plugin_class
from .models import Alert, MetricResult, Severity
from .runner import STATUS_NO_DATA, STATUS_OK, PluginOutcome, run_plugins
from .registry import PluginRecord, PluginRegistry
from ..store.reader import ReadOnlyStore
from ..store.seed import seed_store
from ..store.writer import SeriesStore
from ..timeutil import DateRange

#: Fixed end date so a seeded fixture and its expected numbers never drift.
FIXTURE_END: date = date(2026, 5, 31)
FIXTURE_DAYS: int = 45
FIXTURE_SEED: int = 20260531

_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")


# --- fixtures ---------------------------------------------------------------
def fixture_store(
    directory: Path | str | None = None,
    *,
    days: int = FIXTURE_DAYS,
    seed: int = FIXTURE_SEED,
    end: date = FIXTURE_END,
    filename: str = "store.sqlite3",
) -> Path:
    """Create a seeded store in ``directory`` and return its path."""
    base = Path(directory or tempfile.mkdtemp(prefix="fm-fixture-"))
    base.mkdir(parents=True, exist_ok=True)
    path = base / filename
    with SeriesStore(path) as store:
        store.clear()
        seed_store(store, days=days, seed=seed, end=end)
    return path


def fixture_window(*, days: int = FIXTURE_DAYS, end: date = FIXTURE_END) -> DateRange:
    """A window covering exactly what :func:`fixture_store` wrote."""
    end_day = datetime(end.year, end.month, end.day, tzinfo=UTC)
    return DateRange(since=end_day - timedelta(days=days - 1), until=end_day + timedelta(days=1))


def empty_window(*, end: date = date(1999, 1, 1)) -> DateRange:
    """A window with nothing stored in it - the "no data" path."""
    return DateRange(
        since=datetime(end.year, end.month, end.day, tzinfo=UTC) - timedelta(days=7),
        until=datetime(end.year, end.month, end.day, tzinfo=UTC),
    )


def make_config(plugin_name: str, values: Mapping[str, Any] | None = None) -> AppConfig:
    """An in-memory AppConfig carrying one plugin's section - no file needed."""
    return AppConfig._from_raw(None, {"plugins": {plugin_name: dict(values or {})}})


def run_fixture_plugin(
    plugin_cls: type[MonitorPlugin],
    *,
    store_path: Path | str,
    window: DateRange | None = None,
    config_values: Mapping[str, Any] | None = None,
    timeout: float = 30.0,
) -> PluginOutcome:  # noqa: D401
    """Run ``plugin_cls`` exactly the way the CLI does, and return its outcome."""
    record = PluginRecord(
        name=plugin_cls.name,
        kind="test",
        location=plugin_cls.__module__,
        plugin_class=plugin_cls,
    )
    registry = PluginRegistry(records=[record])
    store = ReadOnlyStore(Path(store_path))
    try:
        report = run_plugins(
            registry,
            store,
            window or fixture_window(),
            config=make_config(plugin_cls.name, config_values),
            timeout=timeout,
        )
    finally:
        store.close()
    return report.outcomes[0] if report.outcomes else PluginOutcome(name=plugin_cls.name, status="error")


# --- the suite --------------------------------------------------------------
class PluginContractTestCase(unittest.TestCase):
    """Conformance tests for a plugin class. Set ``plugin_cls`` in a subclass."""

    #: Subclasses must set this; leaving it unset skips the suite instead of failing.
    plugin_cls: type[MonitorPlugin]
    #: Options to configure the plugin with in the default fixture run.
    config_values: Mapping[str, Any] | None = None
    #: Set False for a plugin that legitimately produces nothing on the fixture data.
    expect_results_on_fixture: bool = True

    # Set by setUpClass.
    _tmp: tempfile.TemporaryDirectory
    store_path: Path

    def setUp(self) -> None:
        if getattr(self, "plugin_cls", None) is None:
            self.skipTest("PluginContractTestCase is a base class: subclass it and set plugin_cls")

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="fm-contract-")
        cls.store_path = fixture_store(cls._tmp.name)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def run_fixture_plugin(self, **kw: Any) -> PluginOutcome:
        merged = dict(self.config_values or {})
        merged.update(dict(kw.pop("config_values", {}) or {}))
        return run_fixture_plugin(self.plugin_cls, store_path=self.store_path, config_values=merged, **kw)

    # --- metadata -----------------------------------------------------------
    def test_is_a_valid_plugin_class(self) -> None:
        self.assertIs(validate_plugin_class(self.plugin_cls, source="contract suite"), self.plugin_cls)

    def test_name_is_stable_and_valid(self) -> None:
        self.assertRegex(self.plugin_cls.name, _NAME_RE)
        self.assertEqual(
            self.plugin_cls.name,
            self.plugin_cls.name.lower(),
            "plugin names are keys in config.toml; keep them lowercase",
        )

    def test_declares_version_and_summary(self) -> None:
        self.assertTrue(self.plugin_cls.version.strip(), "version must not be empty")
        self.assertTrue(
            self.plugin_cls.summary.strip(),
            "summary is what `fm plugins list` shows the user; write one line",
        )

    def test_declared_datasets_are_strings(self) -> None:
        for dataset in self.plugin_cls.datasets:
            self.assertIsInstance(dataset, str)
            self.assertTrue(dataset.strip())

    def test_targets_supported_api_version(self) -> None:
        self.assertLessEqual(self.plugin_cls.requires_api, PLUGIN_API_VERSION)

    def test_declared_options_are_valid_and_unique(self) -> None:
        names = [opt.name for opt in self.plugin_cls.options]
        self.assertEqual(len(names), len(set(names)), "duplicate option names")
        for option in self.plugin_cls.options:
            self.assertTrue(option.help, f"option {option.name!r} needs help text for `fm plugins show`")
            option.coerce(option.default)  # must not raise

    # --- configuration ------------------------------------------------------
    def test_configures_with_defaults_only(self) -> None:
        plugin = self.plugin_cls()
        config = make_config(self.plugin_cls.name, {}).plugin_config(
            self.plugin_cls.name, self.plugin_cls.declared_defaults()
        )
        plugin.configure(config)
        self.assertIsInstance(plugin.config, PluginConfig)

    def test_config_scope_is_limited_to_its_own_section(self) -> None:
        config = make_config(self.plugin_cls.name, {"lookback_weeks": 3})
        section = config.plugin_config(self.plugin_cls.name)
        self.assertEqual(section.get("lookback_weeks"), 3)
        other = config.plugin_config("some_other_plugin")
        self.assertIsNone(other.get("lookback_weeks", None))

    # --- running ------------------------------------------------------------
    def test_runs_cleanly_on_fixture_data(self) -> None:
        outcome = self.run_fixture_plugin()
        self.assertIn(outcome.status, (STATUS_OK, STATUS_NO_DATA), getattr(outcome, "error", ""))
        if outcome.status == STATUS_NO_DATA:
            self.skipTest("plugin declares datasets that the standard fixture does not satisfy")
        if self.expect_results_on_fixture:
            self.assertGreater(outcome.produced, 0, "plugin ran but produced nothing on seeded fixture data")

    def test_emitted_models_are_valid(self) -> None:
        outcome = self.run_fixture_plugin()
        for metric in outcome.metrics:
            self.assertIsInstance(metric, MetricResult)
            self.assertEqual(metric.plugin, self.plugin_cls.name)
            self.assertTrue(metric.key.strip())
            self.assertIsNotNone(metric.window_start.tzinfo)
            self.assertIsNotNone(metric.window_end.tzinfo)
            self.assertGreaterEqual(metric.window_end, metric.window_start)
            if metric.confidence is not None:
                self.assertGreaterEqual(metric.confidence, 0.0)
                self.assertLessEqual(metric.confidence, 1.0)
        for alert in outcome.alerts:
            self.assertIsInstance(alert, Alert)
            self.assertIsInstance(alert.severity, Severity)
            stamps = [e.at for e in alert.evidence]
            self.assertEqual(stamps, sorted(stamps), "evidence must be chronological")

    def test_reports_no_data_rather_than_failing(self) -> None:
        outcome = self.run_fixture_plugin(window=empty_window())
        self.assertNotIn(
            outcome.status,
            ("error", "timeout", "config_error"),
            f"an empty range must not crash the plugin: {outcome.error}",
        )

    def test_run_is_deterministic_on_the_same_data(self) -> None:
        first = self.run_fixture_plugin()
        second = self.run_fixture_plugin()
        self.assertEqual(
            [(m.key, m.value) for m in first.metrics],
            [(m.key, m.value) for m in second.metrics],
            "same data and config must give the same metrics - no wall-clock or RNG leakage",
        )

    # --- the read-only guarantee -------------------------------------------
    def test_store_is_read_only(self) -> None:
        store = ReadOnlyStore(self.store_path)
        try:
            for forbidden in ("put", "put_many", "execute", "executescript", "commit", "clear", "delete_range"):
                self.assertFalse(
                    hasattr(store, forbidden),
                    f"the plugin-facing store must not expose {forbidden!r}",
                )
            with self.assertRaises(sqlite3.OperationalError):
                store.conn.execute("INSERT INTO series (dataset, ts_utc, payload) VALUES ('x','y','{}')")
        finally:
            store.close()

    def test_context_exposes_nothing_writable(self) -> None:
        from .contract import PluginContext

        allowed = {
            "adopt",
            "alert",
            "alerts",
            "api_version",
            "as_dict",
            "cancelled",
            "check_timeout",
            "clock",
            "config",
            "deadline",
            "log",
            "metric",
            "metrics",
            "now",
            "plugin_name",
            "request_cancel",
            "store",
            "time_remaining",
            "window",
        }
        ctx = PluginContext(plugin_name="probe", store=ReadOnlyStore(self.store_path), window=fixture_window())
        exposed = {n for n in dir(ctx) if not n.startswith("_")}
        self.assertEqual(
            exposed - allowed,
            set(),
            f"PluginContext grew new public members: {sorted(exposed - allowed)}",
        )
        ctx.store.close()


__all__ = [
    "FIXTURE_DAYS",
    "FIXTURE_END",
    "FIXTURE_SEED",
    "PluginContractTestCase",
    "empty_window",
    "fixture_store",
    "fixture_window",
    "make_config",
    "run_fixture_plugin",
]
