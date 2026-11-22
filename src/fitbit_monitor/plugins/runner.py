"""Running plugins: isolation, timeouts, and an honest report.

``run_plugins`` is what ``fm plugins run`` executes, and what the ingest path will
call after each import, so the isolation rules live here once:

* **One bad plugin cannot break the run.** Import, configure and run failures are
  captured per plugin; every other plugin still runs.
* **A slow plugin cannot hang the run.** Each plugin gets a wall-clock budget on a
  daemon worker thread; exceeding it yields a named :class:`PluginTimeout`.
* **"Nothing found" and "nothing to find" are different answers.** A plugin whose
  declared datasets are empty for the window is reported ``no_data`` without being
  run, and is not the same as a plugin that ran and produced nothing.
* **Exit codes mean something.** Nonzero only when a plugin errored, never merely
  because it found nothing to report.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, Sequence

from ..config import AppConfig, PluginConfig
from ..errors import ConfigError, FmError
from ..store.reader import ReadOnlyStore
from ..timeutil import DateRange
from .contract import Clock, PLUGIN_API_VERSION, MonitorPlugin, PluginContext
from .errors import PluginConfigError, PluginTimeout
from .models import Alert, MetricResult
from .registry import PluginRegistry
from .sinks import AlertDispatcher, Delivery

log = logging.getLogger("fitbit_monitor.plugins")

#: Exit codes. Documented in ``fm plugins run --help`` and in docs/plugins.md.
EXIT_OK = 0
EXIT_PLUGIN_ERROR = 1
EXIT_USAGE = 2
EXIT_NO_STORE = 4

DEFAULT_TIMEOUT_SECONDS = 120.0

STATUS_OK = "ok"
STATUS_NO_DATA = "no_data"
STATUS_SKIPPED = "skipped"
STATUS_CONFIG_ERROR = "config_error"
STATUS_ERROR = "error"
STATUS_TIMEOUT = "timeout"
FAILING_STATUSES = (STATUS_ERROR, STATUS_TIMEOUT, STATUS_CONFIG_ERROR)


@dataclass
class PluginOutcome:
    """The result of one plugin's turn."""

    name: str
    status: str
    version: str = ""
    source: str = ""
    duration: float = 0.0
    metrics: list[MetricResult] = field(default_factory=list)
    alerts: list[Alert] = field(default_factory=list)
    deliveries: list[Delivery] = field(default_factory=list)
    error: str | None = None
    error_stage: str | None = None
    error_type: str | None = None
    skipped_reason: str | None = None
    coverage: dict[str, dict[str, Any]] = field(default_factory=dict)
    datasets: tuple[str, ...] = ()

    @property
    def produced(self) -> int:
        return len(self.metrics) + len(self.alerts)

    @property
    def failed(self) -> bool:
        return self.status in FAILING_STATUSES

    @property
    def headline(self) -> str:
        """One-line human summary, shared by the CLI and the text report."""
        if self.status == STATUS_OK:
            if self.produced:
                bits = []
                if self.metrics:
                    bits.append(f"{len(self.metrics)} metric{'s' if len(self.metrics) != 1 else ''}")
                if self.alerts:
                    bits.append(f"{len(self.alerts)} alert{'s' if len(self.alerts) != 1 else ''}")
                return f"{self.name}: ok ({', '.join(bits)})"
            return f"{self.name}: ok - ran, nothing to report for this range"
        if self.status == STATUS_NO_DATA:
            return f"{self.name}: no data - nothing stored for {', '.join(self.datasets) or 'this plugin'} in range"
        if self.status == STATUS_SKIPPED:
            return f"{self.name}: skipped - {self.skipped_reason}"
        if self.status == STATUS_TIMEOUT:
            return f"{self.name}: TIMEOUT - {self.error}"
        if self.status == STATUS_CONFIG_ERROR:
            return f"{self.name}: MISCONFIGURED - {self.error}"
        return f"{self.name}: ERROR ({self.error_stage or 'run'}) - {self.error}"

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "status": self.status,
            "version": self.version,
            "source": self.source,
            "duration_ms": round(self.duration * 1000.0, 1),
            "datasets": list(self.datasets),
            "metrics": [m.as_dict() for m in self.metrics],
            "alerts": [a.as_dict() for a in self.alerts],
            "deliveries": [d.as_dict() for d in self.deliveries],
        }
        if self.error:
            out["error"] = self.error
            out["error_stage"] = self.error_stage
            out["error_type"] = self.error_type
        if self.skipped_reason:
            out["skipped_reason"] = self.skipped_reason
        if self.coverage:
            out["coverage"] = self.coverage
        return out


@dataclass
class RunReport:
    """Everything that happened in one ``fm plugins run``."""

    window: DateRange
    outcomes: list[PluginOutcome] = field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    store_path: str | None = None

    @property
    def exit_code(self) -> int:
        return EXIT_PLUGIN_ERROR if any(o.failed for o in self.outcomes) else EXIT_OK

    @property
    def alerts(self) -> list[Alert]:
        return [a for o in self.outcomes for a in o.alerts]

    @property
    def metrics(self) -> list[MetricResult]:
        return [m for o in self.outcomes for m in o.metrics]

    def counts(self) -> dict[str, int]:
        out = {status: 0 for status in (STATUS_OK, STATUS_NO_DATA, STATUS_SKIPPED, STATUS_CONFIG_ERROR, STATUS_ERROR, STATUS_TIMEOUT)}
        for outcome in self.outcomes:
            out[outcome.status] = out.get(outcome.status, 0) + 1
        return out

    def as_dict(self) -> dict[str, Any]:
        return {
            "window": self.window.iso(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "store": self.store_path,
            "counts": self.counts(),
            "exit_code": self.exit_code,
            "outcomes": [o.as_dict() for o in self.outcomes],
        }

    def render_text(self, *, show_metrics: bool = True) -> str:
        lines: list[str] = []
        counts = self.counts()
        lines.append(f"ran {len(self.outcomes)} plugin(s) over {self.window}")
        for outcome in self.outcomes:
            lines.append(f"  {outcome.headline}")
            if outcome.status == STATUS_NO_DATA:
                for dataset, cov in outcome.coverage.items():
                    lines.append(f"      {dataset}: {cov.get('points', 0)} points in range")
            if outcome.status == STATUS_OK and show_metrics:
                for metric in outcome.metrics:
                    lines.append(f"      {metric.key} = {_fmt(metric.value)}" + (f" {metric.unit}" if metric.unit else ""))
                for alert in outcome.alerts:
                    lines.append("      " + alert.rendered().replace("\n", " / "))
        lines.append(
            "summary: "
            + ", ".join(f"{count} {status}" for status, count in counts.items() if count)
            + f"; exit {self.exit_code}"
        )
        return "\n".join(lines)


def run_plugins(
    registry: PluginRegistry,
    store: ReadOnlyStore,
    window: DateRange,
    *,
    config: AppConfig | None = None,
    names: Sequence[str] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    dispatcher: AlertDispatcher | None = None,
    clock: Clock | None = None,
) -> RunReport:
    """Run ``names`` (or every plugin) over ``window``. Never raises for plugin reasons."""
    cfg = config or AppConfig.load(missing_ok=True)
    report = RunReport(window=window, started_at=(clock or _now)(), store_path=str(getattr(store, "path", "")) or None)
    selected = _select(registry, names)
    for name in selected:
        outcome = _run_one(
            registry, store, window, name, config=cfg, timeout=timeout, dispatcher=dispatcher, clock=clock
        )
        report.outcomes.append(outcome)
    report.finished_at = (clock or _now)()
    return report


# ---------------------------------------------------------------------------
def _now() -> datetime:
    from datetime import UTC

    return datetime.now(UTC)


def _select(registry: PluginRegistry, names: Sequence[str] | None) -> list[str]:
    if names:
        return list(dict.fromkeys(names))
    return registry.names()


def _run_one(
    registry: PluginRegistry,
    store: ReadOnlyStore,
    window: DateRange,
    name: str,
    *,
    config: AppConfig,
    timeout: float,
    dispatcher: AlertDispatcher | None,
    clock: Clock | None,
) -> PluginOutcome:
    record = registry.get(name)
    if record is None:
        return PluginOutcome(
            name=name,
            status=STATUS_ERROR,
            error=f"no plugin named {name!r} is installed",
            error_stage="discover",
            error_type="UnknownPlugin",
        )
    outcome = PluginOutcome(name=name, status=STATUS_OK, source=record.kind)
    if not record.ok or record.plugin_class is None:
        error = record.error
        outcome.status = STATUS_ERROR
        outcome.error = str(error) if error else "plugin unavailable"
        outcome.error_stage = getattr(error, "stage", "discover")
        outcome.error_type = type(error).__name__
        return outcome

    plugin_class = record.plugin_class
    outcome.version = plugin_class.version
    outcome.datasets = tuple(plugin_class.datasets)

    # --- configuration ------------------------------------------------------
    try:
        plugin_config = _plugin_config(config, plugin_class)
    except (ConfigError, PluginConfigError, ValueError) as exc:
        outcome.status = STATUS_CONFIG_ERROR
        outcome.error = str(exc)
        outcome.error_type = type(exc).__name__
        return outcome
    if not plugin_config.enabled:
        outcome.status = STATUS_SKIPPED
        outcome.skipped_reason = f"disabled in config ([plugins.{name}] enabled = false)"
        return outcome
    budget = _budget(plugin_config, timeout)
    try:
        plugin = record.create()
        plugin.configure(plugin_config)
    except (PluginConfigError, ConfigError, ValueError) as exc:
        outcome.status = STATUS_CONFIG_ERROR
        outcome.error = str(exc)
        outcome.error_type = type(exc).__name__
        outcome.error_stage = "configure"
        return outcome
    except KeyboardInterrupt:
        raise  # Ctrl-C belongs to the host, not to the plugin that happened to be running
    except BaseException as exc:  # noqa: BLE001 - see module docstring
        outcome.status = STATUS_ERROR
        outcome.error = f"{type(exc).__name__}: {exc}"
        outcome.error_type = type(exc).__name__
        outcome.error_stage = "configure"
        return outcome

    # --- coverage gate ------------------------------------------------------
    for dataset in outcome.datasets:
        outcome.coverage[dataset] = store.coverage(dataset, window.since, window.until).as_dict()
    if outcome.datasets and not store.has_any_data(outcome.datasets, window):
        outcome.status = STATUS_NO_DATA
        return outcome

    # --- run ----------------------------------------------------------------
    ctx = PluginContext(
        plugin_name=name,
        store=store,
        config=plugin_config,
        window=window,
        clock=clock,
        api_version=PLUGIN_API_VERSION,
        deadline=(_now() + timedelta(seconds=budget)) if budget > 0 else None,
    )
    started = time.monotonic()
    try:
        returned = _invoke(plugin, ctx, budget)
        if returned:
            ctx.adopt(returned)
        outcome.status = STATUS_OK
    except PluginTimeout as exc:
        outcome.status = STATUS_TIMEOUT
        outcome.error = f"{exc} (budget {budget:g}s)"
        outcome.error_type = "PluginTimeout"
        outcome.error_stage = "timeout"
    except KeyboardInterrupt:
        raise  # Ctrl-C belongs to the host, not to the plugin that happened to be running
    except BaseException as exc:  # noqa: BLE001 - isolation is the whole point
        # Deliberately BaseException and not Exception: `sys.exit()` in a plugin - or
        # any other BaseException - is that plugin's bug, and ending the host (and
        # every plugin after it) is exactly what this module promises cannot happen.
        outcome.status = STATUS_ERROR
        outcome.error = f"{type(exc).__name__}: {exc}"
        outcome.error_type = type(exc).__name__
        outcome.error_stage = "run"
        log.debug("plugin %s raised", name, exc_info=True)
    outcome.duration = time.monotonic() - started
    outcome.metrics = list(ctx.metrics)
    outcome.alerts = list(ctx.alerts)

    # --- delivery -----------------------------------------------------------
    if dispatcher is not None and outcome.status == STATUS_OK:
        outcome.deliveries = dispatcher.dispatch_all(outcome.alerts)
    return outcome


def _plugin_config(config: AppConfig, plugin_class: type[MonitorPlugin]) -> PluginConfig:
    """Scoped section + declared defaults, with every value coerced to its declared type."""
    declared = {opt.name: opt for opt in plugin_class.options}
    base = config.plugin_config(plugin_class.name, plugin_class.declared_defaults())
    coerced: dict[str, Any] = {}
    for key, value in base.as_dict().items():
        option = declared.get(key)
        coerced[key] = option.coerce(value) if option is not None else value
    return PluginConfig(
        plugin_name=plugin_class.name,
        settings=coerced,
        defaults={},
        declared=frozenset(base.declared),
    )


def _budget(plugin_config: PluginConfig, default: float) -> float:
    try:
        override = plugin_config.get("timeout_seconds", None)
    except FmError:
        override = None
    if override is None:
        return default
    try:
        return float(override)
    except (TypeError, ValueError):
        return default


def _invoke(plugin: MonitorPlugin, ctx: PluginContext, budget: float) -> Iterable[Any] | None:
    """Run one plugin on a daemon worker thread with a wall-clock budget.

    A plugin that blows the budget is abandoned: the host moves on, the thread is
    left to finish (it is a daemon, so it cannot keep the process alive) and
    well-behaved plugins stop at the next ``ctx.check_timeout()`` checkpoint.
    """
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["result"] = plugin.run(ctx)
        except BaseException as exc:  # noqa: BLE001 - reported, never propagated
            box["exc"] = exc

    thread = threading.Thread(target=target, name=f"fm-plugin-{ctx.plugin_name}", daemon=True)
    thread.start()
    thread.join(budget if budget > 0 else None)
    if thread.is_alive():
        ctx.request_cancel()
        raise PluginTimeout(f"exceeded {budget:g}s wall-clock budget", plugin=ctx.plugin_name)
    if "exc" in box:
        raise box["exc"]
    return box.get("result")


def _fmt(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return str(value)


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "EXIT_NO_STORE",
    "EXIT_OK",
    "EXIT_PLUGIN_ERROR",
    "EXIT_USAGE",
    "PluginOutcome",
    "RunReport",
    "STATUS_CONFIG_ERROR",
    "STATUS_ERROR",
    "STATUS_NO_DATA",
    "STATUS_OK",
    "STATUS_SKIPPED",
    "STATUS_TIMEOUT",
    "run_plugins",
]
