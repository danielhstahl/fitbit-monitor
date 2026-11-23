"""End-to-end tests for the ``fm`` command line.

``main(argv)`` returns an exit code instead of calling ``sys.exit``, so the whole
surface is testable in-process. The exit-code contract is what the epic specifies:
nonzero when a plugin errored, zero when it merely found nothing, and a distinct
code when there is no store to read.
"""

from __future__ import annotations

import io
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from fitbit_monitor.cli import build_parser, main
from fitbit_monitor.cli.common import (
    EXIT_ERROR,
    EXIT_NOT_IMPLEMENTED,
    EXIT_NO_STORE,
    EXIT_OK,
    EXIT_USAGE,
)
from fitbit_monitor.store.writer import SeriesStore

import support

BROKEN_PLUGIN = """
raise RuntimeError("nope")
"""

ALERTING_PLUGIN = """
from fitbit_monitor.plugins import MonitorPlugin, PluginOption

class NightlyDipPlugin(MonitorPlugin):
    name = \"nightly_dip\"
    summary = \"alerts when overnight HRV drops below a floor\"
    version = \"1.0.0\"
    datasets = (\"hrv\",)
    options = (PluginOption(\"floor\", \"float\", 40.0, \"alert below this RMSSD\"),)

    def run(self, ctx):
        floor = self.config.get_float(\"floor\", 40.0)
        for point in ctx.store.query(\"hrv\", ctx.window.since, ctx.window.until, fields=[\"rmssd\"]):
            if point[\"rmssd\"] < floor:
                ctx.alert(
                    \"hrv_low\",
                    \"HRV below floor\",
                    f\"{point['rmssd']:.0f} ms on {point.ts.date()}\",
                    occurred_at=point.ts,
                    evidence=[(point.ts, point[\"rmssd\"])],
                )
"""


class CliTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.env = {
            "XDG_DATA_HOME": str(self.home / "data"),
            "XDG_CONFIG_HOME": str(self.home / "config"),
            "HOME": str(self.home),
        }
        for name, value in self.env.items():
            self._setenv(name, value)
        self.store_path = self.home / "data" / "fitbit-monitor" / "store.sqlite3"
        self.plugins_dir = self.home / "plugins"
        self.plugins_dir.mkdir()

    def _setenv(self, name, value):
        import os

        original = os.environ.get(name)
        os.environ[name] = value
        self.addCleanup(
            lambda: os.environ.__setitem__(name, original) if original is not None else os.environ.pop(name, None)
        )

    def fm(self, *argv, expect=None):
        """Run the CLI, capture stdout, and return (code, stdout)."""
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(list(argv))
        text = out.getvalue() + err.getvalue()
        if expect is not None:
            self.assertEqual(code, expect, f"fm {' '.join(argv)} ->\n{text}")
        return code, text

    def seed(self, days: int = 45, end: date = None) -> None:
        end = end or date(2026, 5, 31)
        with SeriesStore(self.store_path) as store:
            support_seed(store, days=days, end=end)


def support_seed(store, days: int, end: date) -> None:
    from fitbit_monitor.store.seed import seed_store

    seed_store(store, days=days, seed=4242, end=end)


class SeedAndInfoTest(CliTestCase):
    def test_dev_seed_creates_the_store(self) -> None:
        _, text = self.fm("dev", "seed", "--days", "20", "--end", "2026-05-31", expect=0)
        self.assertTrue(self.store_path.exists())
        self.assertIn("seeded", text)
        self.assertIn("heart_rate", text)

    def test_dev_seed_json(self) -> None:
        _, text = self.fm("dev", "seed", "--days", "5", "--end", "2026-05-31", "--json", expect=0)
        payload = json.loads(text)
        self.assertEqual(payload["days"], 5)
        self.assertGreater(payload["totals"]["hrv"], 0)

    def test_dev_info_before_and_after(self) -> None:
        _, text = self.fm("dev", "info", expect=0)
        self.assertIn("no store", text)
        self.fm("dev", "seed", "--days", "5", "--end", "2026-05-31", expect=0)
        _, text = self.fm("dev", "info", "--json", expect=0)
        self.assertTrue(json.loads(text)["exists"])

    def test_version_flag(self) -> None:
        _, text = self.fm("--version", expect=0)
        self.assertIn("fitbit-monitor", text)

    def test_no_arguments_prints_help(self) -> None:
        code, text = self.fm()
        self.assertEqual(code, 2)
        self.assertIn("usage", text.lower())


class PluginsListTest(CliTestCase):
    def test_lists_the_built_in_example(self) -> None:
        _, text = self.fm("plugins", "list", expect=0)
        self.assertIn("resting_trend", text)
        self.assertIn("SOURCE", text)

    def test_json_lists_sources_and_collisions(self) -> None:
        _, text = self.fm("plugins", "list", "--json", expect=0)
        payload = json.loads(text)
        names = [p["name"] for p in payload["plugins"]]
        self.assertIn("resting_trend", names)
        self.assertEqual(payload["collisions"], [])
        self.assertEqual(payload["plugin_api_version"], 1)

    def test_broken_plugin_is_listed_with_its_error_and_the_rest_survive(self) -> None:
        support.write_plugin_file(self.plugins_dir, "bad.py", BROKEN_PLUGIN)
        support.write_plugin_file(self.plugins_dir, "good.py", ALERTING_PLUGIN)
        _, text = self.fm("plugins", "list", "--dir", str(self.plugins_dir), expect=0)
        self.assertIn("nightly_dip", text)
        self.assertIn("failed to load", text)
        self.assertIn("RuntimeError", text)

    def test_a_broken_plugin_carries_its_error_on_its_own_row(self) -> None:
        """The one question `fm plugins list` answers is *which* plugin is broken."""
        support.write_plugin_file(self.plugins_dir, "bad.py", BROKEN_PLUGIN)
        support.write_plugin_file(self.plugins_dir, "good.py", ALERTING_PLUGIN)
        _, text = self.fm("plugins", "list", "--dir", str(self.plugins_dir), expect=0)
        rows = [line for line in text.splitlines() if line.startswith("bad")]
        self.assertEqual(len(rows), 1, text)
        self.assertIn("directory", rows[0])
        self.assertIn("error(import):", rows[0])
        self.assertIn("RuntimeError", rows[0])
        # the healthy plugin's row carries no error, and the hint is readable below
        healthy = [line for line in text.splitlines() if line.startswith("nightly_dip")]
        self.assertEqual(len(healthy), 1, text)
        self.assertNotIn("error(", healthy[0])
        self.assertIn("hint", text)

    def test_list_json_names_the_dirs_searched_and_the_reason_each_plugin_is_broken(self) -> None:
        support.write_plugin_file(self.plugins_dir, "bad.py", BROKEN_PLUGIN)
        _, text = self.fm("plugins", "list", "--dir", str(self.plugins_dir), "--json", expect=0)
        payload = json.loads(text)
        self.assertIn(str(self.plugins_dir), payload["plugin_dirs"])
        broken = [p for p in payload["plugins"] if "error" in p]
        self.assertEqual(len(broken), 1)
        self.assertEqual(broken[0]["name"], "bad")
        self.assertEqual(broken[0]["source"], "directory")
        self.assertEqual(broken[0]["error"]["stage"], "import")
        self.assertEqual(broken[0]["error"]["type"], "PluginImportError")
        self.assertIn("RuntimeError", broken[0]["error"]["message"])
        self.assertIsNone(broken[0]["version"])
        self.assertEqual(broken[0]["datasets"], [])

    def test_long_error_messages_are_cut_to_fit_the_table(self) -> None:
        from fitbit_monitor.cli import plugins_cmd
        from fitbit_monitor.plugins.errors import PluginError

        self.assertEqual(plugins_cmd.error_cell(None), "")
        cell = plugins_cmd.error_cell(PluginError("boom " * 60, plugin="long_winded"))
        self.assertTrue(cell.startswith("error(discover): boom"), cell[:40])
        self.assertTrue(cell.endswith("\u2026"), cell[-20:])
        self.assertLessEqual(len(cell), plugins_cmd.ERROR_CELL_WIDTH + len("error(discover): "))
        self.assertNotIn("\n", cell, "a cell must stay on one line or the table skews")

    def test_no_plugins_found_says_where_it_looked(self) -> None:
        from unittest import mock

        from fitbit_monitor.plugins import registry as registry_module

        empty = self.home / "nothing-here"
        empty.mkdir()
        nothing = mock.patch.object(registry_module, "iter_builtin_candidates", return_value=[])
        no_metadata = mock.patch.object(registry_module, "iter_entry_point_candidates", return_value=[])
        with nothing, no_metadata:
            _, text = self.fm("plugins", "list", "--dir", str(empty), expect=0)
            self.assertIn("no plugins found", text)
            self.assertIn(str(empty), text)
            _, text = self.fm("plugins", "list", expect=0)
            self.assertIn("(none configured)", text)

    def test_show_reports_options_and_effective_config(self) -> None:
        _, text = self.fm("plugins", "show", "resting_trend", expect=0)
        self.assertIn("lookback_weeks", text)
        self.assertIn("enabled:", text)
        _, text = self.fm("plugins", "show", "resting_trend", "--json", expect=0)
        payload = json.loads(text)
        self.assertEqual(payload["name"], "resting_trend")
        self.assertTrue(payload["enabled"])

    def test_show_prints_effective_values_and_names_undeclared_keys(self) -> None:
        """The human form has to answer "what did my config change, and what did I typo"."""
        config = self.home / "config" / "fitbit-monitor" / "config.toml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text('[plugins.resting_trend]\nlookback_weeks = 3\ntimeout_seconds = 5\nhrv_feild = "sdnn"\n')
        _, text = self.fm("plugins", "show", "resting_trend", expect=0)
        self.assertIn("config:", text)
        self.assertIn("lookback_weeks = 3", text)
        self.assertIn("timeout_seconds = 5 (no declared default)", text)
        self.assertIn("unused:", text)
        self.assertIn("hrv_feild", text)
        _, text = self.fm("plugins", "show", "resting_trend", "--json", expect=0)
        self.assertEqual(json.loads(text)["unknown_config_keys"], ["hrv_feild"])
        _, text = self.fm("plugins", "show", "resting_trend", "--json", expect=0)
        self.assertEqual(json.loads(text)["config"]["lookback_weeks"], 3)

    def test_show_unknown_plugin_is_a_usage_error(self) -> None:
        _, text = self.fm("plugins", "show", "nope", expect=2)
        self.assertIn("no plugin named", text)

    def test_show_broken_plugin_exits_nonzero(self) -> None:
        support.write_plugin_file(self.plugins_dir, "bad.py", BROKEN_PLUGIN)
        _, text = self.fm("plugins", "show", "bad", "--dir", str(self.plugins_dir), expect=1)
        self.assertIn("ERROR", text)

    def test_api_flag(self) -> None:
        _, text = self.fm("plugins", "--api", expect=0)
        self.assertIn("plugin API version 1", text)


class ValidateTest(CliTestCase):
    def test_all_ok(self) -> None:
        _, text = self.fm("plugins", "validate", expect=0)
        self.assertIn("resting_trend", text)
        self.assertIn("cleanly", text)

    def test_broken_and_unknown_are_reported(self) -> None:
        support.write_plugin_file(self.plugins_dir, "bad.py", BROKEN_PLUGIN)
        code, text = self.fm("plugins", "validate", "bad", "ghost", "--dir", str(self.plugins_dir))
        self.assertEqual(code, 1)
        self.assertIn("no such plugin", text)
        self.assertIn("FAILED", text)

    def test_json_output(self) -> None:
        _, text = self.fm("plugins", "validate", "--json", expect=0)
        payload = json.loads(text)
        self.assertTrue(payload["results"][0]["ok"])

    def test_misconfigured_plugin_is_flagged(self) -> None:
        config = self.home / "config" / "fitbit-monitor" / "config.toml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text('[plugins.resting_trend]\nhrv_field = "nonsense"\nlookback_weeks = "many"\n')
        _, text = self.fm("plugins", "validate", "resting_trend", expect=1)
        self.assertIn("FAILED", text)

    def test_host_options_are_not_reported_as_unused(self) -> None:
        """`timeout_seconds` and `enabled` belong to the host; a clean section stays clean."""
        config = self.home / "config" / "fitbit-monitor" / "config.toml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text("[plugins.resting_trend]\nenabled = true\ntimeout_seconds = 5\n")
        _, text = self.fm("plugins", "validate", "resting_trend", expect=0)
        self.assertNotIn("unused config keys", text)

    def test_undeclared_key_is_named_without_failing_validation(self) -> None:
        config = self.home / "config" / "fitbit-monitor" / "config.toml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text('[plugins.resting_trend]\nhrv_feild = "sdnn"\n')
        _, text = self.fm("plugins", "validate", "resting_trend", expect=0)
        self.assertIn("unused config keys: hrv_feild", text)


class RunTest(CliTestCase):
    def setUp(self) -> None:
        super().setUp()
        # seeded up to today so the relative ranges below contain data
        self.fm("dev", "seed", "--days", "45", expect=0)

    def test_run_all_over_a_range(self) -> None:
        _, text = self.fm("plugins", "run", "--all", "--since", "30d", expect=0)
        self.assertIn("resting_trend: ok", text)
        self.assertIn("exit 0", text)

    def test_run_by_name_and_json(self) -> None:
        code, text = self.fm("plugins", "run", "resting_trend", "--since", "30d", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(text)
        self.assertEqual(payload["outcomes"][0]["name"], "resting_trend")
        self.assertEqual(payload["exit_code"], 0)
        self.assertTrue(payload["outcomes"][0]["metrics"])

    def test_run_without_a_store_is_exit_4(self) -> None:
        self.store_path.unlink()
        _, text = self.fm("plugins", "run", "--all", expect=4)
        self.assertIn("no store", text)

    def test_range_in_the_past_reports_no_data_but_exits_zero(self) -> None:
        _, text = self.fm("plugins", "run", "resting_trend", "--since", "2020-01-01", "--until", "2020-02-01", expect=0)
        self.assertIn("no data", text)

    def test_empty_range_is_refused(self) -> None:
        _, text = self.fm("plugins", "run", "--since", "2026-05-31", "--until", "2026-05-01", expect=2)
        self.assertIn("must be after", text)

    def test_unparsable_date_is_a_usage_error(self) -> None:
        _, text = self.fm("plugins", "run", "--since", "whenever", expect=2)
        self.assertIn("cannot parse date", text)

    def test_set_override_reaches_the_plugin(self) -> None:
        _, text = self.fm(
            "plugins",
            "run",
            "resting_trend",
            "--since", "20d",
            "--set",
            "resting_trend.lookback_weeks=2",
            "--json",
            expect=0,
        )
        weeks = {m["labels"]["iso_week"] for m in json.loads(text)["outcomes"][0]["metrics"]}
        self.assertLessEqual(len(weeks), 2)

    def test_bad_set_syntax_is_a_usage_error(self) -> None:
        _, text = self.fm("plugins", "run", "--set", "just_a_word", expect=2)
        self.assertIn("name.key=value", text)

    def test_disabled_plugin_is_skipped(self) -> None:
        config = self.home / "config" / "fitbit-monitor" / "config.toml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text("[plugins.resting_trend]\nenabled = false\n")
        _, text = self.fm("plugins", "run", "--all", "--since", "30d", expect=0)
        self.assertIn("skipped", text)

    def test_a_key_the_plugin_does_not_declare_is_named_and_costs_nothing(self) -> None:
        """`hrv_feild` is a typo, not a failure: the run says so and still exits 0."""
        config = self.home / "config" / "fitbit-monitor" / "config.toml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text('[plugins.resting_trend]\nhrv_feild = "sdnn"\n')
        _, text = self.fm("plugins", "run", "resting_trend", "--since", "30d", expect=0)
        self.assertIn("unused config keys: hrv_feild", text)
        _, text = self.fm("plugins", "run", "resting_trend", "--since", "30d", "--json", expect=0)
        self.assertEqual(json.loads(text)["outcomes"][0]["unknown_config_keys"], ["hrv_feild"])

    def test_broken_plugin_exits_nonzero_but_the_others_run(self) -> None:
        support.write_plugin_file(self.plugins_dir, "bad.py", BROKEN_PLUGIN)
        code, text = self.fm("plugins", "run", "--all", "--since", "30d", "--dir", str(self.plugins_dir))
        self.assertEqual(code, 1)
        self.assertIn("resting_trend: ok", text)
        self.assertIn("ERROR", text)

    def test_timeout_flag_reports_named_error_and_exits_nonzero(self) -> None:
        support.write_plugin_file(
            self.plugins_dir,
            "slow.py",
            "import time\nfrom fitbit_monitor.plugins import MonitorPlugin\n"
            "class Slow(MonitorPlugin):\n"
            "    name = 'slow'\n"
            "    summary = 'sleeps'\n"
            "    datasets = ('hrv',)\n"
            "    def run(self, ctx):\n        time.sleep(5)\n",
        )
        code, text = self.fm(
            "plugins", "run", "slow", "--since", "30d", "--timeout", "0.1", "--dir", str(self.plugins_dir)
        )
        self.assertEqual(code, 1)
        self.assertIn("TIMEOUT", text)

    def test_run_help_documents_exit_codes_and_no_watch(self) -> None:
        """The scope statement is part of the acceptance criteria, so test it where users read it."""
        subparsers = build_parser()._subparsers._group_actions[0].choices
        run_parser = subparsers["plugins"]._subparsers._group_actions[0].choices["run"]
        help_text = run_parser.format_help()
        self.assertIn("not a daemon", help_text)
        self.assertIn("exit codes", help_text.lower())
        self.assertIn("--watch", help_text, "the epilog names the flags it deliberately does not offer")
        usage = help_text.splitlines()[0]
        self.assertNotIn("--watch", usage, "but no --watch option may exist")


class AlertDeliveryTest(CliTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.fm("dev", "seed", "--days", "20", expect=0)
        support.write_plugin_file(self.plugins_dir, "nightly_dip.py", ALERTING_PLUGIN)
        self.config_path = self.home / "config" / "fitbit-monitor" / "config.toml"
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(
            '[plugins.nightly_dip]\nfloor = 200.0\n\n[alerts]\ncooldown = "36h"\njsonl = true\n'
        )

    def test_run_delivers_alerts_and_alerts_list_shows_them(self) -> None:
        _, text = self.fm("plugins", "run", "nightly_dip", "--since", "20d", "--dir", str(self.plugins_dir), expect=0)
        self.assertIn("HRV below floor", text)
        _, text = self.fm("alerts", "list", expect=0)
        self.assertIn("nightly_dip/hrv_low", text)
        self.assertIn("warning", text)

    def test_alert_history_file_is_written(self) -> None:
        self.fm("plugins", "run", "nightly_dip", "--since", "20d", "--dir", str(self.plugins_dir), expect=0)
        log = self.home / "data" / "fitbit-monitor" / "alerts.jsonl"
        self.assertTrue(log.exists())
        lines = log.read_text().strip().splitlines()
        self.assertTrue(lines)
        self.assertEqual(json.loads(lines[0])["key"], "hrv_low")

    def test_re_running_the_same_range_notifies_once(self) -> None:
        """Idempotence: the same range twice must not add rows or count episodes twice."""
        args = ("plugins", "run", "nightly_dip", "--since", "20d", "--dir", str(self.plugins_dir))
        self.fm(*args, expect=0)
        _, text = self.fm("alerts", "list", "--json", expect=0)
        after_first = json.loads(text)
        self.assertGreaterEqual(after_first["count"], 1)
        self.assertGreaterEqual(
            sum(a["occurrences"] for a in after_first["alerts"]),
            2,
            "a 20-day stretch of low HRV collapses into fewer alerts than days",
        )

        self.fm(*args, expect=0)
        _, text = self.fm("alerts", "list", "--json", expect=0)
        after_second = json.loads(text)
        self.assertEqual(after_second["count"], after_first["count"], "a replay must not add rows")
        self.assertEqual(
            [a["occurrences"] for a in after_second["alerts"]],
            [a["occurrences"] for a in after_first["alerts"]],
            "a replay must not count the same episodes twice",
        )

    def test_no_deliver_computes_without_writing_history(self) -> None:
        self.fm(
            "plugins", "run", "nightly_dip", "--since", "20d", "--no-deliver", "--dir", str(self.plugins_dir), expect=0
        )
        _, text = self.fm("alerts", "list", expect=0)
        self.assertIn("no alerts", text)

    def test_alerts_list_filters_and_json(self) -> None:
        self.fm("plugins", "run", "nightly_dip", "--since", "20d", "--dir", str(self.plugins_dir), expect=0)
        _, text = self.fm("alerts", "list", "--severity", "critical", expect=0)
        self.assertIn("no alerts", text)
        _, text = self.fm("alerts", "list", "--plugin", "nightly_dip", "--limit", "3", "--json", expect=0)
        self.assertTrue(json.loads(text)["alerts"])

    def test_alerts_list_limit_zero_is_everything(self) -> None:
        """`--limit 1` is a page; `--limit 0` is the whole history, not an empty one."""
        self.fm("plugins", "run", "nightly_dip", "--since", "20d", "--dir", str(self.plugins_dir), expect=0)
        _, one = self.fm("alerts", "list", "--limit", "1", "--json", expect=0)
        _, everything = self.fm("alerts", "list", "--limit", "0", "--json", expect=0)
        self.assertEqual(len(json.loads(one)["alerts"]), 1)
        self.assertGreater(len(json.loads(everything)["alerts"]), 1)

    def test_alerts_list_without_a_store(self) -> None:
        self.store_path.unlink()
        _, text = self.fm("alerts", "list", expect=4)
        self.assertIn("nothing has run yet", text)


class AlertWebhookTest(CliTestCase):
    """The one outbound path, driven through the real command line.

    The webhook is the only place health-derived data may leave the machine, so the
    gate - HTTPS unless the user explicitly overrides it, the body no larger than
    the alert, a dead endpoint costing nothing but the delivery - is tested here and
    not only at the sink level. Everything is loopback: no test here reaches the
    internet.
    """

    #: The whole webhook body: the alert, and nothing that identifies this machine.
    ALERT_BODY_KEYS = {
        "plugin",
        "key",
        "title",
        "message",
        "severity",
        "occurred_at",
        "confidence",
        "evidence",
        "dedup_key",
    }

    def setUp(self) -> None:
        super().setUp()
        self.fm("dev", "seed", "--days", "20", expect=0)
        support.write_plugin_file(self.plugins_dir, "nightly_dip.py", ALERTING_PLUGIN)
        self.config_path = self.home / "config" / "fitbit-monitor" / "config.toml"
        self.config_path.parent.mkdir(parents=True, exist_ok=True)

    def configure(self, url: str, *, enabled: bool = True) -> None:
        self.config_path.write_text(
            "[plugins.nightly_dip]\nfloor = 200.0\n\n[alerts]\ncooldown = \"36h\"\njsonl = false\n\n"
            f"[alerts.webhook]\nurl = \"{url}\"\nenabled = {'true' if enabled else 'false'}\n"
        )

    def run_hook(self, *extra: str, **kw) -> tuple[int, str]:
        return self.fm(
            "plugins", "run", "nightly_dip", "--since", "20d", "--dir", str(self.plugins_dir), *extra, **kw
        )

    def test_plaintext_url_is_refused_and_nothing_is_sent(self) -> None:
        with LoopbackWebhook() as hook:
            self.configure(hook.url)
            _, text = self.run_hook(expect=2)
            self.assertIn("must be https://", text)
            self.assertIn("allow_insecure", text, "the refusal names the way out")
            self.assertEqual(hook.requests, [], "a refused URL must not be contacted at all")

    def test_a_configured_but_disabled_webhook_is_not_used(self) -> None:
        with LoopbackWebhook() as hook:
            self.configure(hook.url, enabled=False)
            self.run_hook(expect=0)
            self.assertEqual(hook.requests, [])

    def test_the_insecure_flag_is_what_allows_a_loopback_url(self) -> None:
        with LoopbackWebhook() as hook:
            self.configure(hook.url)
            self.run_hook("--allow-insecure-webhook", expect=0)
            self.assertTrue(hook.requests, "the flag is what lets the loopback endpoint be used")
            for request in hook.requests:
                body = json.loads(request["body"])
                self.assertEqual(set(body), self.ALERT_BODY_KEYS, "the body is the alert and nothing else")
                self.assertEqual(body["plugin"], "nightly_dip")
                self.assertNotIn("store", request["body"])

    def test_a_dead_endpoint_is_logged_and_costs_the_run_nothing(self) -> None:
        """HTTPS needs no flag; an unreachable endpoint is a warning, never a failed run."""
        self.configure(f"https://127.0.0.1:{closed_port()}/hook")
        with self.assertLogs("fitbit_monitor.sinks", level="WARNING") as captured:
            self.run_hook(expect=0)
        self.assertTrue(
            any("webhook POST" in line and "still stored locally" in line for line in captured.output),
            captured.output,
        )
        _, listing = self.fm("alerts", "list", "--json", expect=0)
        self.assertGreaterEqual(json.loads(listing)["count"], 1, "the alert is kept even though delivery failed")


class LoopbackWebhook:
    """A throwaway HTTP endpoint on 127.0.0.1 that records what it was POSTed."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                outer.requests.append(
                    {"path": self.path, "body": self.rfile.read(length).decode(), "type": self.headers.get("Content-Type")}
                )
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/hook"

    def __enter__(self) -> "LoopbackWebhook":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def closed_port() -> int:
    """A port on loopback that is almost certainly nobody's: bound, then released."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class PluginNewTest(CliTestCase):
    def test_scaffolds_and_then_runs_it(self) -> None:
        _, text = self.fm("plugin", "new", "weekly_steps", "--dir", str(self.plugins_dir), expect=0)
        self.assertIn("created", text)
        project = self.plugins_dir / "weekly_steps"
        self.assertTrue((project / "weekly_steps" / "plugin.py").is_file())
        self.assertTrue((project / "tests" / "test_weekly_steps.py").is_file())
        # The next steps it prints have to be the steps that work: the project root
        # is the drop-in directory, because discovery reads `<pkg>/plugin.py`.
        self.assertIn(f"cd {project} && fm plugins list --dir .", text)
        self.fm("dev", "seed", "--days", "20", expect=0)
        _, text = self.fm(
            "plugins", "run", "weekly_steps", "--since", "20d", "--dir", str(project), expect=0
        )
        self.assertIn("weekly_steps: ok", text)

    def test_dry_run_writes_nothing(self) -> None:
        _, text = self.fm(
            "plugin", "new", "weekly_steps", "--dir", str(self.plugins_dir), "--dry-run", expect=0
        )
        self.assertIn("would create", text)
        self.assertIn("weekly_steps/plugin.py", text)
        self.assertFalse((self.plugins_dir / "weekly_steps").exists())

    def test_json_output(self) -> None:
        _, text = self.fm("plugin", "new", "weekly_steps", "--dir", str(self.plugins_dir), "--json", expect=0)
        payload = json.loads(text)
        self.assertEqual(payload["name"], "weekly_steps")
        self.assertEqual(payload["package"], "weekly_steps")
        self.assertIn("weekly_steps/plugin.py", payload["files"])
        self.assertIn("tests/test_weekly_steps.py", payload["files"])
        self.assertFalse(payload["dry_run"])

    def test_bad_name_is_reported(self) -> None:
        _, text = self.fm("plugin", "new", "Bad-Name", "--dir", str(self.plugins_dir), expect=2)
        self.assertIn("invalid plugin name", text)


class ModuleInvocationTest(CliTestCase):
    """`python -m fitbit_monitor` is the same program as the `fm` console script."""

    def test_module_entrypoint_runs(self) -> None:
        import os
        import subprocess

        env = dict(os.environ)
        env.update(self.env)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        result = subprocess.run(
            [sys.executable, "-m", "fitbit_monitor", "plugins", "--api"],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("plugin API version 1", result.stdout)

    def test_console_script_is_declared(self) -> None:
        import tomllib

        raw = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
        scripts = raw["project"]["scripts"]
        # Two names, one entrypoint - ADR-0001 §11.
        self.assertEqual(scripts["fm"], "fitbit_monitor.cli:main")
        self.assertEqual(scripts["fitbit-monitor"], "fitbit_monitor.cli:main")
        group = raw["project"]["entry-points"]["fitbit_monitor.plugins"]
        self.assertEqual(group["resting_trend"], "fitbit_monitor.plugins.builtin.resting_trend:RestingTrendPlugin")


class ErrorBoundaryTest(CliTestCase):
    """The CLI catches, reports, and returns a code; it does not traceback at users."""

    def test_unexpected_error_is_reported_as_error_exit(self) -> None:
        from unittest import mock

        from fitbit_monitor.cli import plugins_cmd

        with mock.patch.object(plugins_cmd, "cmd_plugins_list", side_effect=ValueError("unexpected")):
            _, text = self.fm("plugins", "list", expect=1)
        self.assertIn("error: ValueError: unexpected", text)

    def test_verbose_reraises_so_a_developer_sees_the_traceback(self) -> None:
        from unittest import mock

        from fitbit_monitor.cli import plugins_cmd

        with mock.patch.object(plugins_cmd, "cmd_plugins_list", side_effect=ValueError("unexpected")):
            with self.assertRaises(ValueError):
                main(["-vv", "plugins", "list"])


class ConfigAndPathsTest(CliTestCase):
    def test_explicit_db_and_data_dir_flags(self) -> None:
        db = self.home / "elsewhere.sqlite3"
        data = self.home / "elsewhere"
        self.fm("--db", str(db), "--data-dir", str(data), "dev", "seed", "--days", "3", "--end", "2026-05-31", expect=0)
        self.assertTrue(db.exists())
        _, text = self.fm("--db", str(db), "dev", "info", "--json", expect=0)
        self.assertTrue(json.loads(text)["exists"])

    def test_broken_config_file_is_reported(self) -> None:
        bad = self.home / "broken.toml"
        bad.write_text("[plugins\n")
        _, text = self.fm("--config", str(bad), "plugins", "list", expect=2)
        self.assertIn("invalid TOML", text)

    def test_config_store_path_is_honoured(self) -> None:
        config = self.home / "config.toml"
        config.write_text(f'[store]\npath = "{self.home / "from-config.sqlite3"}"\n')
        self.fm("--config", str(config), "dev", "seed", "--days", "3", "--end", "2026-05-31", expect=0)
        self.assertTrue((self.home / "from-config.sqlite3").exists())


class CommandSurfaceTest(CliTestCase):
    """The scaffold contract: the whole command surface exists, and `--help` works.

    A command may be a stub before it is built, but it must be *in* the parser so a
    doc page, a cron line or a test can name it, it must explain itself with `--help`,
    and it must fail as a stub (code 72, one line of prose) instead of as a traceback.
    """

    #: the names the scaffold ticket promises a user will find in `fm --help`
    REQUIRED = ["auth", "import", "store", "plugins", "report", "db", "doctor", "config"]
    #: the two that are declared but not built yet
    STUBS = ["report", "doctor"]

    def _top_level(self) -> dict:
        return build_parser()._subparsers._group_actions[0].choices

    def test_top_level_help_exits_zero_and_names_every_subcommand(self) -> None:
        code, text = self.fm("--help")
        self.assertEqual(code, 0)
        self.assertIn("usage", text.lower())
        for name in self.REQUIRED:
            self.assertIn(name, text, f"`fm --help` does not list {name}")

    def test_the_parser_and_the_help_text_agree(self) -> None:
        """`--help` truncates nothing that matters: every group is registered *and* shown."""
        _, text = self.fm("--help")
        for name in self._top_level():
            self.assertIn(name, text)

    def test_every_subcommand_has_working_help(self) -> None:
        for name in self._top_level():
            with self.subTest(command=name):
                code, text = self.fm(name, "--help")
                self.assertEqual(code, 0)
                self.assertIn("usage", text.lower())
                self.assertNotIn("Traceback", text)

    def test_stubs_report_themselves_as_not_implemented(self) -> None:
        for name in self.STUBS:
            with self.subTest(command=name):
                code, text = self.fm(name)
                self.assertEqual(code, EXIT_NOT_IMPLEMENTED, text)
                self.assertIn("not implemented", text)
                self.assertIn("--help", text, "a stub says where to read more")
                self.assertNotIn("Traceback", text)

    def test_not_implemented_is_a_code_of_its_own(self) -> None:
        """Distinct from every code the working commands use, and from the sysexits value."""
        with self.subTest("sysexits"):
            if hasattr(os, "EX_NOTIMPLEMENTED"):
                self.assertEqual(EXIT_NOT_IMPLEMENTED, os.EX_NOTIMPLEMENTED)
        for other in (EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_NO_STORE):
            self.assertNotEqual(EXIT_NOT_IMPLEMENTED, other)

    def test_a_bare_group_never_tracebacks(self) -> None:
        """`fm store` with no leaf command is a usage error with usage text, not a stack."""
        for name in self._top_level():
            if name in self.STUBS or name == "import":
                continue  # `fm import` is itself a command, not just a group
            with self.subTest(command=name):
                code, text = self.fm(name)
                self.assertEqual(code, EXIT_USAGE)
                self.assertIn("usage", text.lower())
                self.assertNotIn("Traceback", text)

    def test_bare_import_says_what_is_missing_instead_of_traceback(self) -> None:
        """`fm import` with nothing set up fails as a report, not as a stack trace."""
        code, text = self.fm("import")
        self.assertIn(code, (EXIT_ERROR, EXIT_USAGE, EXIT_NO_STORE), text)
        self.assertTrue(text.strip(), "a refused command should say why")
        self.assertNotIn("Traceback", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
