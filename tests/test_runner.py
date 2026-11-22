"""Tests for the runner: isolation, timeouts, statuses and exit codes.

Every "one bad plugin" case in the epic is here: import, configure, run and
timeout failures, each asserted to leave the other plugins having run, and the run
exit code nonzero only for a real failure.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from fitbit_monitor.plugins.registry import PluginRegistry
from fitbit_monitor.plugins.runner import (
    EXIT_PLUGIN_ERROR,
    STATUS_CONFIG_ERROR,
    STATUS_ERROR,
    STATUS_NO_DATA,
    STATUS_OK,
    STATUS_SKIPPED,
    STATUS_TIMEOUT,
    RunReport,
    run_plugins,
)
from fitbit_monitor.store.reader import ReadOnlyStore
from fitbit_monitor.store.writer import SeriesStore
from fitbit_monitor.timeutil import DateRange

import support

PLUGINS = [
    support.OkPlugin,
    support.SilentPlugin,
    support.NoDataPlugin,
    support.BrokenRunPlugin,
    support.BrokenConfigurePlugin,
    support.ConfigurePlugin,
    support.SlowPlugin,
    support.IgnoringSlowPlugin,
    support.ReturningPlugin,
    support.BadModelPlugin,
]


class RunnerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.store_path = Path(cls.tmp.name) / "store.sqlite3"
        with SeriesStore(cls.store_path) as store:
            # Two days inside the test window, one outside it.
            store.put("daily_summary", support.utc(2), {"resting_hr": 55.0})
            store.put("daily_summary", support.utc(3), {"resting_hr": 65.0})
            store.put("daily_summary", support.utc(20), {"resting_hr": 99.0})
        cls.store = ReadOnlyStore(cls.store_path)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.store.close()
        cls.tmp.cleanup()

    def registry(self, *classes) -> PluginRegistry:
        return PluginRegistry._resolve([support.candidate(cls) for cls in (classes or PLUGINS)])

    def run_names(self, names, *, config=None, timeout=10.0, window=None, dispatcher=None):
        return run_plugins(
            self.registry(),
            self.store,
            window or support.window(),
            config=config or support.make_config(),
            names=names,
            timeout=timeout,
            dispatcher=dispatcher,
        )

    def outcome(self, report: RunReport, name: str):
        matches = [o for o in report.outcomes if o.name == name]
        self.assertEqual(len(matches), 1, name)
        return matches[0]

    # --- happy path ---------------------------------------------------------
    def test_plugin_emits_metrics_and_alerts(self) -> None:
        report = self.run_names(["ok_plugin"])
        outcome = self.outcome(report, "ok_plugin")
        self.assertEqual(outcome.status, STATUS_OK)
        self.assertEqual(outcome.version, "1.2.3")
        metric = outcome.metrics[0]
        self.assertEqual(metric.key, "mean_resting_hr")
        # only the two in-window days count: (55 + 65) / 2
        self.assertEqual(metric.value, 60.0)
        self.assertEqual(metric.details["n"], 2)
        self.assertEqual(len(outcome.alerts), 1)
        self.assertIn("Resting HR above threshold", outcome.alerts[0].title)
        self.assertEqual(report.exit_code, 0)

    def test_window_bounds_what_the_plugin_sees(self) -> None:
        narrow = DateRange(since=support.utc(3), until=support.utc(4))
        outcome = self.outcome(self.run_names(["ok_plugin"], window=narrow), "ok_plugin")
        self.assertEqual(outcome.metrics[0].value, 65.0)

    def test_per_plugin_config_changes_behaviour(self) -> None:
        report = self.run_names(["ok_plugin"], config=support.make_config({"ok_plugin": {"threshold": 100.0}}))
        self.assertEqual(self.outcome(report, "ok_plugin").alerts, [])
        report = self.run_names(["ok_plugin"], config=support.make_config({"ok_plugin": {"threshold": 10.0}}))
        self.assertEqual(len(self.outcome(report, "ok_plugin").alerts), 1)

    def test_declared_option_types_are_coerced_before_run(self) -> None:
        report = self.run_names(["ok_plugin"], config=support.make_config({"ok_plugin": {"threshold": "30"}}))
        self.assertEqual(len(self.outcome(report, "ok_plugin").alerts), 1)

    def test_returned_models_are_adopted(self) -> None:
        outcome = self.outcome(self.run_names(["returning_plugin"]), "returning_plugin")
        self.assertEqual(outcome.status, STATUS_OK)
        self.assertEqual(outcome.metrics[0].key, "returned")

    # --- skipping and "nothing to find" ------------------------------------
    def test_disabled_plugin_is_skipped_with_a_reason(self) -> None:
        report = self.run_names(["ok_plugin"], config=support.make_config({"ok_plugin": {"enabled": False}}))
        outcome = self.outcome(report, "ok_plugin")
        self.assertEqual(outcome.status, STATUS_SKIPPED)
        self.assertIn("enabled = false", outcome.skipped_reason)
        self.assertEqual(report.exit_code, 0)

    def test_no_data_is_not_no_findings(self) -> None:
        report = self.run_names(["no_data_plugin", "silent_plugin"])
        no_data = self.outcome(report, "no_data_plugin")
        silent = self.outcome(report, "silent_plugin")
        self.assertEqual(no_data.status, STATUS_NO_DATA)
        self.assertIn("no data", no_data.headline)
        self.assertIn("glucose", no_data.headline)
        self.assertEqual(no_data.coverage["glucose"]["points"], 0)
        self.assertEqual(silent.status, STATUS_OK)
        self.assertEqual(silent.produced, 0)
        self.assertIn("nothing to report", silent.headline)
        self.assertEqual(report.exit_code, 0)

    def test_empty_range_reports_no_data_for_everyone(self) -> None:
        empty = DateRange(since=support.utc(1) - timedelta(days=400), until=support.utc(1) - timedelta(days=390))
        report = self.run_names(["ok_plugin"], window=empty)
        self.assertEqual(self.outcome(report, "ok_plugin").status, STATUS_NO_DATA)
        self.assertEqual(report.exit_code, 0)

    # --- failures -----------------------------------------------------------
    def test_run_error_is_isolated_and_fails_the_run(self) -> None:
        report = self.run_names(["ok_plugin", "broken_run_plugin"])
        self.assertEqual(self.outcome(report, "broken_run_plugin").status, STATUS_ERROR)
        self.assertIn("boom in run", self.outcome(report, "broken_run_plugin").error)
        self.assertEqual(self.outcome(report, "broken_run_plugin").error_stage, "run")
        self.assertEqual(self.outcome(report, "ok_plugin").status, STATUS_OK)
        self.assertEqual(report.exit_code, EXIT_PLUGIN_ERROR)

    def test_configure_error_is_reported_as_misconfiguration(self) -> None:
        report = self.run_names(["broken_configure_plugin", "ok_plugin"])
        outcome = self.outcome(report, "broken_configure_plugin")
        self.assertEqual(outcome.status, STATUS_CONFIG_ERROR)
        self.assertEqual(outcome.error_stage, "configure")
        self.assertEqual(self.outcome(report, "ok_plugin").status, STATUS_OK)
        self.assertEqual(report.exit_code, EXIT_PLUGIN_ERROR)

    def test_plugin_rejecting_its_config_is_misconfigured_not_broken(self) -> None:
        report = self.run_names(
            ["configure_plugin"], config=support.make_config({"configure_plugin": {"mode": "fast"}})
        )
        self.assertEqual(self.outcome(report, "configure_plugin").status, STATUS_CONFIG_ERROR)
        report = self.run_names(
            ["configure_plugin"], config=support.make_config({"configure_plugin": {"mode": "safe"}})
        )
        self.assertEqual(self.outcome(report, "configure_plugin").status, STATUS_OK)

    def test_invalid_model_from_a_plugin_is_a_run_error(self) -> None:
        outcome = self.outcome(self.run_names(["bad_model_plugin"]), "bad_model_plugin")
        self.assertEqual(outcome.status, STATUS_ERROR)
        self.assertIn("ValidationError", outcome.error)

    def test_unknown_plugin_name_is_an_error_outcome(self) -> None:
        report = self.run_names(["ghost_plugin"])
        outcome = self.outcome(report, "ghost_plugin")
        self.assertEqual(outcome.status, STATUS_ERROR)
        self.assertIn("no plugin named", outcome.error)

    def test_a_load_error_becomes_an_error_outcome(self) -> None:
        from fitbit_monitor.plugins.errors import PluginImportError
        from fitbit_monitor.plugins.registry import PluginRecord

        registry = PluginRegistry(
            records=[PluginRecord(name="broken", kind="directory", location="/x/broken.py", error=PluginImportError("nope"))]
        )
        report = run_plugins(registry, self.store, support.window(), config=support.make_config())
        outcome = report.outcomes[0]
        self.assertEqual(outcome.status, STATUS_ERROR)
        self.assertEqual(outcome.error_stage, "import")

    def test_load_errors_from_discovery_are_reported_not_raised(self) -> None:
        class NotAPlugin:
            """Whatever a plugins directory points at that is not a plugin."""

        registry = PluginRegistry.discover(
            candidates=[
                support.candidate(support.OkPlugin),
                support.candidate(NotAPlugin, kind="directory"),
            ]
        )
        report = run_plugins(registry, self.store, support.window(), config=support.make_config())
        statuses = {o.name: o.status for o in report.outcomes}
        self.assertEqual(statuses["ok_plugin"], STATUS_OK)
        self.assertEqual(statuses["NotAPlugin"], STATUS_ERROR)

    # --- timeouts -----------------------------------------------------------
    def test_slow_plugin_times_out_without_stopping_others(self) -> None:
        report = self.run_names(["slow_plugin", "ok_plugin"], timeout=0.2)
        slow = self.outcome(report, "slow_plugin")
        self.assertEqual(slow.status, STATUS_TIMEOUT)
        self.assertIn("budget", slow.error)
        self.assertEqual(slow.error_stage, "timeout")
        self.assertEqual(self.outcome(report, "ok_plugin").status, STATUS_OK)
        self.assertEqual(report.exit_code, EXIT_PLUGIN_ERROR)

    def test_cooperative_plugin_stops_at_its_checkpoint(self) -> None:
        import time

        started = time.monotonic()
        report = self.run_names(["slow_plugin"], timeout=0.15)
        elapsed = time.monotonic() - started
        self.assertEqual(self.outcome(report, "slow_plugin").status, STATUS_TIMEOUT)
        self.assertLess(elapsed, 2.0, "a plugin that checks its deadline should stop promptly")

    def test_plugin_ignoring_the_deadline_is_abandoned_not_awaited(self) -> None:
        import time

        started = time.monotonic()
        report = self.run_names(["ignoring_slow_plugin"], timeout=0.1)
        elapsed = time.monotonic() - started
        self.assertEqual(self.outcome(report, "ignoring_slow_plugin").status, STATUS_TIMEOUT)
        self.assertLess(elapsed, 1.0, "the host must not wait for a runaway plugin")

    def test_per_plugin_timeout_comes_from_config(self) -> None:
        report = self.run_names(
            ["slow_plugin"],
            config=support.make_config({"slow_plugin": {"timeout_seconds": 0.1}}),
            timeout=30.0,
        )
        self.assertEqual(self.outcome(report, "slow_plugin").status, STATUS_TIMEOUT)

    def test_nonsense_timeout_falls_back_to_the_default(self) -> None:
        from fitbit_monitor.plugins.runner import _budget

        config = support.make_config({"slow_plugin": {"timeout_seconds": "ten minutes"}})
        plugin_config = config.plugin_config("slow_plugin")
        self.assertEqual(_budget(plugin_config, 12.0), 12.0)

    # --- reporting ----------------------------------------------------------
    def test_text_report_names_every_status(self) -> None:
        report = self.run_names(
            ["ok_plugin", "silent_plugin", "no_data_plugin", "broken_run_plugin"],
            config=support.make_config({"ok_plugin": {"enabled": False}}),
        )
        text = report.render_text()
        self.assertIn("skipped", text)
        self.assertIn("nothing to report", text)
        self.assertIn("no data", text)
        self.assertIn("ERROR", text)
        self.assertIn("exit 1", text)

    def test_json_report_shape(self) -> None:
        report = self.run_names(["ok_plugin"])
        payload = report.as_dict()
        self.assertEqual(payload["exit_code"], 0)
        self.assertEqual(payload["counts"]["ok"], 1)
        self.assertIn("window", payload)
        self.assertIn("started_at", payload)
        outcome = payload["outcomes"][0]
        self.assertEqual(outcome["metrics"][0]["key"], "mean_resting_hr")
        self.assertEqual(outcome["duration_ms"] >= 0, True)

    def test_runner_never_raises_for_plugin_reasons(self) -> None:
        report = self.run_names(PLUGINS and [p.name for p in PLUGINS], timeout=0.1)
        self.assertEqual(len(report.outcomes), len(PLUGINS))

    def test_run_all_when_no_names_given(self) -> None:
        report = run_plugins(self.registry(), self.store, support.window(), config=support.make_config(), timeout=0.1)
        self.assertEqual(len(report.outcomes), len(PLUGINS))

    def test_duplicate_names_are_deduplicated(self) -> None:
        report = self.run_names(["ok_plugin", "ok_plugin"], timeout=0.1)
        self.assertEqual(len(report.outcomes), 1)

    def test_store_path_is_reported(self) -> None:
        report = self.run_names(["ok_plugin"], timeout=0.1)
        self.assertEqual(report.store_path, str(self.store_path))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
