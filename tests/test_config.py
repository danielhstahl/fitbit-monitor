"""Tests for config loading and the plugin-scoped config view.

The scoping rule is a security boundary in miniature: a plugin must not be able to
read another plugin's options, the webhook URL, or anything else in the file.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from fitbit_monitor.config import AppConfig, PluginConfig, parse_duration
from fitbit_monitor.errors import ConfigError, UsageError
from fitbit_monitor.paths import Paths
from fitbit_monitor.timeutil import DateRange, parse_date_arg

TOML = """
[store]
path = "{store}"

[plugins]
dirs = ["{plugins}"]

[plugins.resting_trend]
enabled = false
lookback_weeks = 12
hrv_field = "sdnn"
mystery_option = 1

[plugins.other]
threshold = 2.5

[alerts]
cooldown = "12h"
jsonl = false
console = true
timeout_seconds = 45

[alerts.webhook]
url = "https://example.invalid/hook"
enabled = true
"""


class ConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.config_path = self.home / "config.toml"
        self.config_path.write_text(
            TOML.format(store=str(self.home / "store.sqlite3"), plugins=str(self.home / "plugins"))
        )
        self.config = AppConfig.load(self.config_path)

    def test_host_settings(self) -> None:
        self.assertEqual(self.config.store_path, self.home / "store.sqlite3")
        self.assertEqual(self.config.plugin_dirs, (self.home / "plugins",))
        self.assertEqual(self.config.alert_cooldown, timedelta(hours=12))
        self.assertFalse(self.config.alert_jsonl)
        self.assertEqual(self.config.plugin_timeout, 45.0)
        self.assertTrue(self.config.webhook.active)
        self.assertEqual(self.config.webhook.url, "https://example.invalid/hook")

    def test_missing_file_is_fine_unless_required(self) -> None:
        cfg = AppConfig.load(self.home / "absent.toml")
        self.assertIsNone(cfg.store_path)
        self.assertEqual(cfg.plugin_dirs, ())
        self.assertFalse(cfg.webhook.active)
        with self.assertRaises(ConfigError):
            AppConfig.load(self.home / "absent.toml", missing_ok=False)

    def test_invalid_toml_is_reported_with_the_path(self) -> None:
        bad = self.home / "bad.toml"
        bad.write_text("[plugins\noops")
        with self.assertRaises(ConfigError):
            AppConfig.load(bad)

    def test_plugin_scoping(self) -> None:
        section = self.config.plugin_config("resting_trend", {"lookback_weeks": 8, "hrv_field": "rmssd"})
        self.assertFalse(section.enabled)
        self.assertEqual(section["lookback_weeks"], 12)
        self.assertEqual(section.get_str("hrv_field"), "sdnn")
        # defaults fill what the file omits
        self.assertEqual(section.get_int("min_days_per_week", 4), 4)
        # another plugin's section is invisible
        other = self.config.plugin_config("unrelated")
        self.assertIsNone(other.get("lookback_weeks", None))
        with self.assertRaises(KeyError):
            other["lookback_weeks"]

    def test_declared_unknown_keys_are_surfaced(self) -> None:
        declared = {"lookback_weeks": 8, "hrv_field": "rmssd", "min_days_per_week": 4, "mystery_option": 0}
        section = self.config.plugin_config("resting_trend", declared)
        self.assertEqual(section.unknown_keys, ())
        declared = {"lookback_weeks": 8, "hrv_field": "rmssd", "min_days_per_week": 4}
        stray = self.config.plugin_config("resting_trend", declared)
        # `mystery_option` is in the file but declared by no plugin - a typo, surfaced
        # by `fm plugins show` rather than silently ignored.
        self.assertEqual(stray.unknown_keys, ("mystery_option",))

    def test_typed_accessors_coerce_and_complain(self) -> None:
        section = PluginConfig(
            plugin_name="p",
            settings={"n": "12", "x": "2.5", "flag": "yes", "text": 3, "bad": "abc", "when": "2h"},
            defaults={},
            declared=frozenset({"n", "x", "flag", "text", "bad", "when"}),
        )
        self.assertEqual(section.get_int("n", 0), 12)
        self.assertEqual(section.get_float("x", 0.0), 2.5)
        self.assertTrue(section.get_bool("flag", False))
        self.assertEqual(section.get_duration("when", timedelta(hours=1)), timedelta(hours=2))
        with self.assertRaises(ConfigError):
            section.get_str("text")
        with self.assertRaises(ConfigError):
            section.get_int("bad", 0)
        # An undeclared key is a silent default, not a crash: `unknown_keys` reports it.
        self.assertEqual(section.get("never_declared", 5), 5)

    def test_environment_adds_plugin_dirs(self) -> None:
        os.environ["FM_PLUGIN_DIRS"] = f"{self.home / 'a'}{os.pathsep}{self.home / 'b'}"
        self.addCleanup(os.environ.pop, "FM_PLUGIN_DIRS", None)
        cfg = AppConfig.load(self.config_path)
        self.assertEqual(cfg.plugin_dirs[-2:], (self.home / "a", self.home / "b"))

    def test_shared_defaults_apply_to_every_plugin(self) -> None:
        path = self.home / "defaults.toml"
        path.write_text('[plugins]\ndefaults = { timeout_seconds = 5 }\n[plugins.x]\nenabled = true\n')
        cfg = AppConfig.load(path)
        section = cfg.plugin_config("x")
        self.assertEqual(section.get("timeout_seconds"), 5)
        self.assertTrue(section.enabled)

    def test_appconfig_from_raw_has_no_file_dependency(self) -> None:
        cfg = AppConfig._from_raw(None, {"alerts": {"cooldown": "90m"}})
        self.assertEqual(cfg.alert_cooldown, timedelta(minutes=90))


class DurationTest(unittest.TestCase):
    def test_parses(self) -> None:
        self.assertEqual(parse_duration("45s"), timedelta(seconds=45))
        self.assertEqual(parse_duration("90m"), timedelta(minutes=90))
        self.assertEqual(parse_duration("2h30m"), timedelta(hours=2, minutes=30))
        self.assertEqual(parse_duration("1w"), timedelta(weeks=1))
        self.assertEqual(parse_duration("300"), timedelta(seconds=300))

    def test_rejects_junk(self) -> None:
        for bad in ("", "soon", "2 days", "d2"):
            with self.assertRaises(ConfigError, msg=bad):
                parse_duration(bad)


class DateArgTest(unittest.TestCase):
    def test_forms(self) -> None:
        day = parse_date_arg("2026-05-01")
        self.assertEqual((day.year, day.month, day.day), (2026, 5, 1))
        self.assertEqual(day.tzinfo, __import__("datetime").UTC)
        self.assertEqual(parse_date_arg("2026-05-01", end_of_day=True), day + timedelta(days=1))
        self.assertEqual(parse_date_arg("2026-05-01T10:30").hour, 10)
        self.assertLess(parse_date_arg("7d"), parse_date_arg("1d"))
        self.assertIsNotNone(parse_date_arg("now"))
        self.assertIsNotNone(parse_date_arg("today"))
        self.assertIsNotNone(parse_date_arg("yesterday"))

    def test_rejects_junk(self) -> None:
        with self.assertRaises(UsageError):
            parse_date_arg("last tuesday")

    def test_range_validates_order(self) -> None:
        with self.assertRaises(Exception):
            DateRange(since=parse_date_arg("2026-05-02"), until=parse_date_arg("2026-05-01"))
        window = DateRange(since=parse_date_arg("2026-05-01"), until=parse_date_arg("2026-05-08"))
        self.assertEqual(window.days, 7.0)
        self.assertIn("since", window.iso())


class PathsTest(unittest.TestCase):
    def test_resolution_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = Paths.resolve(config=f"{tmp}/c.toml", data_dir=f"{tmp}/d", store=f"{tmp}/s.db")
            self.assertEqual(paths.config, Path(tmp) / "c.toml")
            self.assertEqual(paths.store, Path(tmp) / "s.db")
            paths.ensure_data_dir()
            self.assertTrue(paths.data_dir.is_dir())

    def test_environment_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["FM_DB"] = f"{tmp}/env.sqlite3"
            os.environ["FM_CONFIG"] = f"{tmp}/env.toml"
            self.addCleanup(os.environ.pop, "FM_DB", None)
            self.addCleanup(os.environ.pop, "FM_CONFIG", None)
            self.assertEqual(Paths.resolve().store, Path(tmp) / "env.sqlite3")
            self.assertEqual(Paths.resolve().config, Path(tmp) / "env.toml")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
