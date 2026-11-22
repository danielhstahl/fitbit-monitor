"""The plugin contract in code: :class:`MonitorPlugin`, :class:`PluginContext`.

This is ADR-0004 turned into types. Read it top to bottom and you know what a
plugin may touch:

* ``ctx.store`` - a :class:`~fitbit_monitor.store.reader.ReadOnlyStore`. Reads
  only; SQLite itself refuses a write through it.
* ``ctx.config`` - a :class:`~fitbit_monitor.config.PluginConfig`, scoped to
  ``[plugins.<name>]``. A plugin cannot read another plugin's options.
* ``ctx.window`` / ``ctx.now()`` - the half-open UTC window and an injectable clock.
* ``ctx.log`` - a logger named ``fitbit_monitor.plugin.<name>``.
* ``ctx.metric(...)`` / ``ctx.alert(...)`` - the only way out. Results are
  collected by the host and handed to the sinks.

A plugin gets no connection, no filesystem handle, no network helper and no
reference to the host, and the host runs it in a worker thread with a deadline.
"""

from __future__ import annotations

import abc
import logging
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Final, Iterable, Mapping, Sequence

from ..config import PluginConfig
from ..store.reader import ReadOnlyStore
from ..timeutil import DateRange, ensure_utc
from .errors import PluginRegistrationError, PluginTimeout
from .models import Alert, Evidence, MetricResult, Severity

#: Bumped when the contract changes incompatibly. A plugin that needs a newer host
#: than this is refused at discovery with a named error instead of misbehaving.
PLUGIN_API_VERSION: Final[int] = 1

#: Oldest host API a plugin may declare. New hosts run old plugins, never the reverse.
MIN_PLUGIN_API_VERSION: Final[int] = 1

_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")

LogFactory = Callable[[str], logging.LoggerAdapter]
Clock = Callable[[], datetime]


def plugin_logger(name: str) -> logging.LoggerAdapter:
    return logging.LoggerAdapter(logging.getLogger(f"fitbit_monitor.plugin.{name}"), {"plugin": name})


@dataclass(frozen=True)
class PluginOption:
    """A declared option: the plugin's config schema, visible to ``fm plugins show``."""

    name: str
    type: str = "float"
    default: Any = None
    help: str = ""
    required: bool = False
    choices: tuple[Any, ...] = ()

    _TYPES = ("int", "float", "bool", "str", "duration", "list")

    def __post_init__(self) -> None:
        if not _NAME_RE.match(self.name):
            raise PluginRegistrationError(f"invalid option name {self.name!r}")
        if self.type not in self._TYPES:
            raise PluginRegistrationError(
                f"option {self.name!r}: type must be one of {', '.join(self._TYPES)}",
                plugin=None,
            )

    def coerce(self, value: Any) -> Any:
        """Cast a TOML value to the declared type; raise on nonsense."""
        from ..config import parse_duration

        if value is None:
            if self.required:
                from ..errors import ConfigError

                raise ConfigError(f"option {self.name!r} is required")
            return None
        if self.choices and value not in self.choices:
            from ..errors import ConfigError

            raise ConfigError(
                f"option {self.name!r} must be one of {', '.join(map(repr, self.choices))}, got {value!r}"
            )
        if self.type == "int":
            return int(value)
        if self.type == "float":
            return float(value)
        if self.type == "bool":
            return bool(value) if not isinstance(value, str) else value.lower() in {"1", "true", "yes", "on"}
        if self.type == "str":
            return str(value)
        if self.type == "duration":
            return parse_duration(str(value), context=f"option {self.name!r}")
        if self.type == "list":
            return list(value) if isinstance(value, (list, tuple)) else [value]
        return value

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "type": self.type,
            "default": self.default if not isinstance(self.default, timedelta) else self.default.total_seconds(),
            "help": self.help,
        }
        if self.required:
            out["required"] = True
        if self.choices:
            out["choices"] = list(self.choices)
        return out


class PluginContext:
    """The read-only world a plugin runs inside. Built by the host, never by a plugin."""

    __slots__ = (
        "_metrics",
        "_alerts",
        "_deadline",
        "_cancel",
        "plugin_name",
        "api_version",
        "store",
        "config",
        "window",
        "clock",
        "log",
    )

    def __init__(
        self,
        *,
        plugin_name: str,
        store: ReadOnlyStore,
        config: PluginConfig | None = None,
        window: DateRange,
        clock: Clock | None = None,
        log: logging.LoggerAdapter | None = None,
        api_version: int = PLUGIN_API_VERSION,
        deadline: datetime | None = None,
        cancel: threading.Event | None = None,
    ) -> None:
        self.plugin_name = plugin_name
        self.api_version = api_version
        self.store = store
        self.config = config if config is not None else PluginConfig(plugin_name=plugin_name)
        self.window = window
        self.clock: Clock = clock or (lambda: datetime.now(ensure_utc(window.until).tzinfo))
        self.log = log if log is not None else plugin_logger(plugin_name)
        self._metrics: list[MetricResult] = []
        self._alerts: list[Alert] = []
        self._deadline = deadline
        self._cancel = cancel or threading.Event()

    # --- time ---------------------------------------------------------------
    def now(self) -> datetime:
        """The host's clock. Never call ``datetime.now()`` in a plugin: tests inject this."""
        return ensure_utc(self.clock(), field="clock result")

    @property
    def deadline(self) -> datetime | None:
        return self._deadline

    def time_remaining(self) -> float | None:
        if self._deadline is None:
            return None
        return (self._deadline - self.now()).total_seconds()

    def request_cancel(self) -> None:
        """Host-side only: ask a running plugin to stop at its next checkpoint."""
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def check_timeout(self) -> None:
        """Cooperative checkpoint for long scans. Raises :class:`PluginTimeout`."""
        if self._cancel.is_set():
            raise PluginTimeout("cancelled by the host", plugin=self.plugin_name)
        remaining = self.time_remaining()
        if remaining is not None and remaining <= 0:
            raise PluginTimeout("wall-clock budget exhausted", plugin=self.plugin_name)

    # --- emission -----------------------------------------------------------
    @property
    def metrics(self) -> tuple[MetricResult, ...]:
        return tuple(self._metrics)

    @property
    def alerts(self) -> tuple[Alert, ...]:
        return tuple(self._alerts)

    def metric(
        self,
        key: str,
        value: float | int | str,
        *,
        unit: str | None = None,
        confidence: float | None = None,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
        labels: Mapping[str, str] | None = None,
        details: Mapping[str, Any] | None = None,
        summary: str | None = None,
    ) -> MetricResult:
        """Record a computed value. The run window and plugin name are filled in."""
        result = MetricResult(
            plugin=self.plugin_name,
            key=key,
            value=value,
            unit=unit,
            confidence=confidence,
            window_start=window_start or self.window.since,
            window_end=window_end or self.window.until,
            labels=dict(labels or {}),
            details=dict(details or {}),
            produced_at=self.now(),
            summary=summary,
        )
        self._metrics.append(result)
        return result

    def alert(
        self,
        key: str,
        title: str,
        message: str,
        *,
        occurred_at: datetime,
        severity: Severity | str = Severity.WARNING,
        confidence: float | None = None,
        evidence: Iterable[Evidence] | None = None,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
        dedup_key: str | None = None,
        labels: Mapping[str, str] | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> Alert:
        """Raise an alert. Delivered to the configured sinks by the host."""
        result = Alert(
            plugin=self.plugin_name,
            key=key,
            title=title,
            message=message,
            occurred_at=occurred_at,
            severity=Severity.parse(severity),
            confidence=confidence,
            evidence=tuple(evidence or ()),
            window_start=window_start or self.window.since,
            window_end=window_end or self.window.until,
            dedup_key=dedup_key,
            labels=dict(labels or {}),
            details=dict(details or {}),
        )
        self._alerts.append(result)
        return result

    def adopt(self, results: Iterable[Any]) -> None:
        """Take ownership of models a ``run()`` returned instead of emitting."""
        for item in results:
            if isinstance(item, MetricResult):
                self._metrics.append(item)
            elif isinstance(item, Alert):
                self._alerts.append(item)
            else:
                raise TypeError(
                    f"{self.plugin_name}: run() returned {type(item).__name__}; "
                    "return MetricResult/Alert objects or use ctx.metric()/ctx.alert()"
                )

    def as_dict(self) -> dict[str, Any]:
        return {
            "plugin": self.plugin_name,
            "api_version": self.api_version,
            "window": self.window.iso(),
            "config": self.config.as_dict(),
            "deadline": self._deadline.isoformat() if self._deadline else None,
        }


class MonitorPlugin(abc.ABC):
    """Base class for every plugin, built-in or third-party.

    Subclass it, set the identity attributes, declare ``options``, implement
    :meth:`run`. ``run`` either calls ``ctx.metric``/``ctx.alert`` or returns the
    models it produced - not both.
    """

    #: Stable, machine-readable id. This is the key used by ``--name`` and
    #: ``[plugins.<name>]``, so it must not change between releases.
    name: str = ""
    #: One-line human description, shown by ``fm plugins list`` and the scaffold docs.
    summary: str = ""
    #: The plugin's own version, independent of the package version.
    version: str = "0.1.0"
    #: Host plugin API this plugin was written against.
    requires_api: int = PLUGIN_API_VERSION
    #: Datasets the plugin reads - shown by ``fm plugins list`` and used to answer
    #: "no data for this range" without running the plugin.
    datasets: Sequence[str] = ()
    #: Declared options: defaults, types and help text.
    options: Sequence[PluginOption] = ()

    # -----------------------------------------------------------------------
    def configure(self, config: PluginConfig) -> None:
        """Validate and cache options. May raise :class:`PluginConfigError`.

        Called before ``run`` and once per discovery pass by ``fm plugins validate``.
        Anything expensive or fallible about configuration belongs here, not in run.
        """
        self.config = config

    #: Set by :meth:`configure`; plugins may use ``self.config`` in run().
    config: PluginConfig

    @abc.abstractmethod
    def run(self, ctx: PluginContext) -> Iterable[Any] | None:
        """Compute metrics and alerts over ``ctx.window``.

        Must not write: the store handed to the plugin is read-only, and the host
        runs this in a worker thread with a wall-clock deadline. Long loops should
        call ``ctx.check_timeout()`` periodically.
        """
        raise NotImplementedError

    # -----------------------------------------------------------------------
    @classmethod
    def declared_defaults(cls) -> dict[str, Any]:
        return {opt.name: opt.default for opt in cls.options}

    @classmethod
    def option_map(cls) -> dict[str, PluginOption]:
        return {opt.name: opt for opt in cls.options}

    @classmethod
    def describe(cls) -> dict[str, Any]:
        return {
            "name": cls.name,
            "summary": cls.summary,
            "version": cls.version,
            "requires_api": cls.requires_api,
            "datasets": list(cls.datasets),
            "options": [opt.as_dict() for opt in cls.options],
        }


def validate_plugin_class(candidate: Any, *, source: str = "") -> type[MonitorPlugin]:
    """Raise :class:`PluginRegistrationError` unless ``candidate`` is a usable plugin class.

    Every rejection names the plugin and says what is missing, because this is the
    error a plugin author sees first.
    """
    where = f" from {source}" if source else ""
    if not isinstance(candidate, type):
        raise PluginRegistrationError(
            f"{candidate!r}{where} is not a class; a plugin must be a subclass of MonitorPlugin"
        )
    if not issubclass(candidate, MonitorPlugin):
        missing = [m for m in ("name", "version", "run") if not hasattr(candidate, m)]
        raise PluginRegistrationError(
            f"{candidate.__module__}.{candidate.__name__}{where} is not a MonitorPlugin subclass"
            + (f"; missing {', '.join(missing)}" if missing else ""),
            plugin=getattr(candidate, "name", None) or candidate.__name__,
            hint="subclass fitbit_monitor.plugins.MonitorPlugin",
        )
    plugin_name = candidate.name or candidate.__name__
    if candidate.__abstractmethods__:
        raise PluginRegistrationError(
            f"plugin {plugin_name!r}{where} does not implement the required interface: "
            f"{', '.join(sorted(candidate.__abstractmethods__))}",
            plugin=plugin_name,
            hint="implement run(self, ctx)",
        )
    if not candidate.name:
        raise PluginRegistrationError(
            f"{candidate.__module__}.{candidate.__name__}{where} has no `name`; "
            "set a stable lowercase id like name = \"my_metric\"",
            plugin=candidate.__name__,
        )
    if not _NAME_RE.match(candidate.name):
        raise PluginRegistrationError(
            f"plugin name {candidate.name!r}{where} is not a valid id",
            plugin=candidate.name,
            hint="use lowercase letters, digits, '_', '-' or '.', starting with a letter",
        )
    if not isinstance(candidate.version, str) or not candidate.version.strip():
        raise PluginRegistrationError(
            f"plugin {candidate.name!r}{where} has an invalid `version`; set e.g. version = \"1.0.0\"",
            plugin=candidate.name,
        )
    if candidate.requires_api > PLUGIN_API_VERSION:
        raise PluginRegistrationError(
            f"plugin {candidate.name!r}{where} needs plugin API {candidate.requires_api}, "
            f"this host provides {PLUGIN_API_VERSION}",
            plugin=candidate.name,
            hint="upgrade fitbit-monitor",
        )
    if candidate.requires_api < MIN_PLUGIN_API_VERSION:
        raise PluginRegistrationError(
            f"plugin {candidate.name!r}{where} targets plugin API {candidate.requires_api}, "
            f"this host dropped it at {MIN_PLUGIN_API_VERSION}",
            plugin=candidate.name,
        )
    for dataset in candidate.datasets:
        if not isinstance(dataset, str) or not dataset.strip():
            raise PluginRegistrationError(
                f"plugin {candidate.name!r}{where} declares a bad dataset {dataset!r}", plugin=candidate.name
            )
    names = [opt.name for opt in candidate.options]
    if len(names) != len(set(names)):
        raise PluginRegistrationError(
            f"plugin {candidate.name!r}{where} declares duplicate options", plugin=candidate.name
        )
    return candidate
