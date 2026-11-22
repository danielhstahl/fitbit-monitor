"""Shared fixtures for the plugin tests: sample plugins and small helpers.

Deliberately importable as ``support`` from both pytest and ``unittest discover``
(so: no package-relative imports, no pytest-only fixtures).
"""

from __future__ import annotations

import tempfile
import time
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping

from fitbit_monitor.config import AppConfig
from fitbit_monitor.plugins import (
    MetricResult,
    MonitorPlugin,
    PluginContext,
    PluginOption,
    Severity,
)
from fitbit_monitor.plugins.loader import Candidate
from fitbit_monitor.timeutil import DateRange

FIXTURE_END = date(2026, 5, 31)


def utc(day: int = 1, hour: int = 0) -> datetime:
    return datetime(2026, 5, day, hour, tzinfo=UTC)


def window(days: int = 10) -> DateRange:
    return DateRange(since=utc(1), until=utc(1) + timedelta(days=days))


def candidate(cls: type, *, kind: str = "entry_point", location: str | None = None) -> Candidate:
    """A discovery candidate wrapping an in-process class."""
    return Candidate(
        kind=kind,
        location=location or f"{cls.__module__}:{cls.__name__}",
        label=getattr(cls, "name", None) or cls.__name__,
        load=lambda c=cls: c,
    )


def write_plugin_file(directory: Path, filename: str, body: str) -> Path:
    path = directory / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


# --- sample plugins ----------------------------------------------------------
class OkPlugin(MonitorPlugin):
    """Emits one metric and one alert, and records that it ran."""

    name = "ok_plugin"
    summary = "emits one metric and one alert"
    version = "1.2.3"
    datasets = ("daily_summary",)
    options = (PluginOption("threshold", "float", 60.0, "alert when the mean exceeds this"),)

    def run(self, ctx: PluginContext) -> Iterable[Any] | None:
        values = [p["resting_hr"] for p in ctx.store.query("daily_summary", ctx.window.since, ctx.window.until, fields=["resting_hr"])]
        mean = sum(values) / len(values) if values else 0.0
        ctx.metric("mean_resting_hr", round(mean, 2), unit="bpm", confidence=0.9, details={"n": len(values)})
        threshold = self.config.get_float("threshold", 60.0)
        if mean >= threshold:
            ctx.alert(
                "resting_hr_high",
                "Resting HR above threshold",
                f"mean {mean:.1f} bpm over the window",
                occurred_at=ctx.now(),
                severity=Severity.WARNING,
                confidence=0.7,
                evidence=[(utc(2), mean)],
            )
        return None


class NoDataPlugin(MonitorPlugin):
    """Reads a dataset the fixture never contains."""

    name = "no_data_plugin"
    summary = "reads a dataset that is usually absent"
    datasets = ("glucose",)

    def run(self, ctx: PluginContext) -> None:
        raise AssertionError("must not run when its dataset is missing")


class SilentPlugin(MonitorPlugin):
    """Runs cleanly and emits nothing - 'no findings', not 'no data'."""

    name = "silent_plugin"
    summary = "runs and finds nothing"
    datasets = ("daily_summary",)

    def run(self, ctx: PluginContext) -> None:
        return None


class ReturningPlugin(MonitorPlugin):
    """Returns its models instead of emitting them."""

    name = "returning_plugin"
    summary = "returns models"
    datasets = ("daily_summary",)

    def run(self, ctx: PluginContext) -> Iterable[Any]:
        return [MetricResult(plugin=self.name, key="returned", value=1, window_start=utc(1), window_end=utc(2))]


class ConfigurePlugin(MonitorPlugin):
    """Rejects a config value it cannot live with."""

    name = "configure_plugin"
    summary = "validates its own options"
    options = (PluginOption("mode", "str", "safe", "must be safe or fast", choices=("safe", "fast")),)

    def run(self, ctx: PluginContext) -> None:
        return None

    def configure(self, config) -> None:
        super().configure(config)
        mode = config.get_str("mode", "safe")
        if mode == "fast":
            from fitbit_monitor.plugins.errors import PluginConfigError

            raise PluginConfigError("fast mode is not implemented", plugin=self.name)


class BrokenRunPlugin(MonitorPlugin):
    """Raises in run(). The isolation tests' favourite plugin."""

    name = "broken_run_plugin"
    summary = "always raises in run"
    datasets = ("daily_summary",)

    def run(self, ctx: PluginContext) -> None:
        raise RuntimeError("boom in run")


class BrokenConfigurePlugin(MonitorPlugin):
    """Raises while being configured."""

    name = "broken_configure_plugin"
    summary = "always raises in configure"
    datasets = ("daily_summary",)

    def run(self, ctx: PluginContext) -> None:
        raise AssertionError("must not run")

    def configure(self, config) -> None:
        raise ValueError("no good reason")


class SlowPlugin(MonitorPlugin):
    """Sleeps past any reasonable budget unless the host cancels it."""

    name = "slow_plugin"
    summary = "sleeps for a long time"
    datasets = ("daily_summary",)

    def run(self, ctx: PluginContext) -> None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            ctx.check_timeout()
            time.sleep(0.01)


class IgnoringSlowPlugin(MonitorPlugin):
    """Never checks the deadline - the hard-timeout case."""

    name = "ignoring_slow_plugin"
    summary = "ignores cancellation"
    datasets = ("daily_summary",)

    def run(self, ctx: PluginContext) -> None:
        time.sleep(2)


class BadModelPlugin(MonitorPlugin):
    """Emits an invalid model, so the contract validation is exercised in a run."""

    name = "bad_model_plugin"
    summary = "emits an invalid metric"
    datasets = ("daily_summary",)

    def run(self, ctx: PluginContext) -> None:
        ctx.metric("nope", 1.0, confidence=4.0)


def make_config(
    sections: Mapping[str, Mapping[str, Any]] | None = None,
    *,
    alerts: Mapping[str, Any] | None = None,
    plugins_extra: Mapping[str, Any] | None = None,
) -> AppConfig:
    """An in-memory AppConfig: ``sections`` is ``{plugin: {key: value}}``."""
    plugins: dict[str, Any] = {name: dict(values) for name, values in (sections or {}).items()}
    plugins.update(dict(plugins_extra or {}))
    raw: dict[str, Any] = {"plugins": plugins}
    if alerts:
        raw["alerts"] = dict(alerts)
    return AppConfig._from_raw(None, raw)


@contextmanager
def temp_dir(prefix: str = "fm-test-"):
    tmp = tempfile.TemporaryDirectory(prefix=prefix)
    try:
        yield Path(tmp.name)
    finally:
        tmp.cleanup()


__all__ = [
    "BadModelPlugin",
    "ConfigurePlugin",
    "FIXTURE_END",
    "IgnoringSlowPlugin",
    "NoDataPlugin",
    "OkPlugin",
    "ReturningPlugin",
    "SilentPlugin",
    "SlowPlugin",
    "BrokenRunPlugin",
    "BrokenConfigurePlugin",
    "candidate",
    "make_config",
    "temp_dir",
    "utc",
    "window",
    "write_plugin_file",
]
