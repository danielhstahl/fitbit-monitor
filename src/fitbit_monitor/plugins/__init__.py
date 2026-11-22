"""fitbit-monitor plugins (ADR-0004).

Public API for plugin authors. Everything you need is importable from here::

    from fitbit_monitor.plugins import MonitorPlugin, PluginContext, PluginOption

    class MyPlugin(MonitorPlugin):
        name = "my_plugin"
        summary = "weekly mean of a stored field"
        datasets = ("daily_summary",)
        options = [PluginOption("field", "str", "resting_hr", "which field to average")]

        def run(self, ctx: PluginContext) -> None:
            field = self.config.get_str("field", "resting_hr")
            for day, value in self._daily(ctx, field):
                ctx.metric("my_metric", value, unit="…", window_start=day, window_end=day)

The read-only context, the emitted models and the CLI's plugin commands all come
from this package; ``docs/plugins.md`` is the prose version.
"""

from __future__ import annotations

from ..store.downsample import aggregate, daily_values, downsample
from ..store.reader import Completeness, Coverage, DatasetInfo, Gap, ReadOnlyStore, SeriesPoint
from .contract import (
    MIN_PLUGIN_API_VERSION,
    PLUGIN_API_VERSION,
    MonitorPlugin,
    PluginContext,
    PluginOption,
    validate_plugin_class,
)
from .errors import (
    PluginConfigError,
    PluginError,
    PluginImportError,
    PluginRegistrationError,
    PluginRuntimeError,
    PluginTimeout,
)
from .loader import ENTRY_POINT_GROUP
from .models import Alert, Evidence, MetricResult, Severity
from .registry import PluginRecord, PluginRegistry
from .runner import RunReport, run_plugins
from .sinks import AlertDispatcher, ConsoleSink, JsonlSink, WebhookSink

__all__ = [
    "ENTRY_POINT_GROUP",
    "MIN_PLUGIN_API_VERSION",
    "PLUGIN_API_VERSION",
    "Alert",
    "AlertDispatcher",
    "Completeness",
    "DatasetInfo",
    "Gap",
    "ConsoleSink",
    "Coverage",
    "Evidence",
    "JsonlSink",
    "MetricResult",
    "MonitorPlugin",
    "PluginConfigError",
    "PluginContext",
    "PluginError",
    "PluginImportError",
    "PluginOption",
    "PluginRecord",
    "PluginRegistry",
    "PluginRegistrationError",
    "PluginRuntimeError",
    "PluginTimeout",
    "ReadOnlyStore",
    "RunReport",
    "SeriesPoint",
    "Severity",
    "WebhookSink",
    "aggregate",
    "daily_values",
    "downsample",
    "run_plugins",
    "validate_plugin_class",
]

#: What ``fm plugins --api`` prints: the contract version this host serves.
API_VERSION_STRING = f"1.{PLUGIN_API_VERSION}"
