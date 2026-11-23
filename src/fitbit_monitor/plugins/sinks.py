"""Where alerts and metrics go once a plugin has produced them.

Sinks are deliberately dumb one-method objects - ``emit(alert)`` - so adding one is
a class and a line in :func:`build_dispatcher`. Nothing in here may raise into the
runner: a dead webhook must not turn a successful run into a failed one, so every
sink swallows its own failure, logs it, and reports it in the run record.

The dispatcher owns the policy that sinks must agree on: **dedup**. An alert whose
dedup key was already seen inside the cooldown window updates its stored row and
is *not* delivered again, so one overnight episode is one notification. A store
that cannot take that write - full disk, read-only mount, locked by another
process - degrades to delivering without dedup rather than failing the run.

Outbound traffic: :class:`WebhookSink` is the only place in this package that
opens a socket, and it refuses anything that is not ``https://`` unless the user
explicitly opts in. See ``docs/data-policy.md``.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol, Sequence

from ..config import AppConfig, WebhookSettings
from ..errors import ConfigError, FmError
from ..paths import alert_log_path
from ..plugins.models import Alert
from ..store.alerts import AlertHistory

log = logging.getLogger("fitbit_monitor.sinks")

Printer = Callable[[str], None]


class AlertSink(Protocol):
    """Anything that can take delivery of an alert."""

    name: str

    def emit(self, alert: Alert) -> bool:
        """Deliver ``alert``. Return True on success. Must not raise."""
        ...


@dataclass
class ConsoleSink:
    """Print the alert. The default sink, because a monitor nobody sees is decoration."""

    printer: Printer = print
    name: str = "console"

    def emit(self, alert: Alert) -> bool:
        self.printer(alert.rendered())
        return True


@dataclass
class JsonlSink:
    """Append one JSON object per alert to a file under the data dir.

    Chosen over a second database because it is ``tail -f``-able, trivially
    consumable by whatever the user already runs, and survives the store being
    rebuilt.
    """

    path: Path
    name: str = "jsonl"

    def emit(self, alert: Alert) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(alert.as_dict(), sort_keys=True) + "\n")
            return True
        except OSError as exc:
            log.warning("jsonl sink could not write %s: %s", self.path, exc)
            return False

    def read_all(self) -> list[Alert]:
        """Every alert ever appended, oldest first. Silently skips unparseable lines."""
        if not self.path.exists():
            return []
        out: list[Alert] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(Alert.from_dict(json.loads(line)))
            except (ValueError, KeyError) as exc:
                log.warning("ignoring malformed line in %s: %s", self.path, exc)
        return out


class WebhookSink:
    """POST the alert to a user-configured URL.

    This is the one outbound path in the program, so it is paranoid on purpose:
    https only unless ``allow_insecure`` is set, a short timeout, a body that is
    exactly the alert (no store paths, no config, no other plugins' output), and
    any failure is logged rather than raised.
    """

    name = "webhook"

    def __init__(
        self,
        url: str,
        *,
        timeout: float = 10.0,
        allow_insecure: bool = False,
        opener: Callable[..., Any] | None = None,
        user_agent: str = "fitbit-monitor/0.1",
    ) -> None:
        self.url = url
        self.timeout = float(timeout)
        self.opener = opener or urllib.request.urlopen
        self.user_agent = user_agent
        self.failures: list[str] = []
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" and not allow_insecure:
            raise ConfigError(
                f"alerts.webhook.url must be https:// (got {url!r}); health data does not leave "
                "this machine over plaintext",
                hint="set alerts.webhook.allow_insecure = true (or pass --allow-insecure-webhook) "
                "only for a loopback test endpoint",
            )
        if parsed.scheme not in ("https", "http"):
            raise ConfigError(f"alerts.webhook.url has no http(s) scheme: {url!r}")
        if parsed.scheme == "http":
            log.warning("webhook %s is plaintext HTTP - explicit allow_insecure override in effect", url)

    def emit(self, alert: Alert) -> bool:
        body = json.dumps(alert.as_dict(full=False), sort_keys=True).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": self.user_agent},
            method="POST",
        )
        try:
            with self.opener(request, timeout=self.timeout) as response:
                status = int(getattr(response, "status", 200))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            self.failures.append(str(exc))
            log.warning("webhook POST to %s failed: %s (alert still stored locally)", self.url, exc)
            return False
        if 200 <= status < 300:
            return True
        self.failures.append(f"HTTP {status}")
        log.warning("webhook POST to %s returned HTTP %s (alert still stored locally)", self.url, status)
        return False


@dataclass
class Delivery:
    """What happened to one alert: stored, delivered, or suppressed as a repeat."""

    alert: Alert
    stored: bool = True
    is_new: bool = True
    delivered_to: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    suppressed_by: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "plugin": self.alert.plugin,
            "key": self.alert.key,
            "severity": self.alert.severity.value,
            "dedup_key": self.alert.effective_dedup_key,
            "is_new": self.is_new,
            "occurrences": self.alert.occurrences,
            "alert_id": self.alert.alert_id,
            "delivered_to": list(self.delivered_to),
            "failed": list(self.failed),
            "suppressed_by": self.suppressed_by,
        }


@dataclass
class AlertDispatcher:
    """Dedup + fan-out. The single path from ``ctx.alert()`` to the outside world."""

    sinks: Sequence[AlertSink] = ()
    history: AlertHistory | None = None
    cooldown: timedelta = timedelta(hours=36)
    deliveries: list[Delivery] = field(default_factory=list)

    def dispatch(self, alert: Alert) -> Delivery:
        stored, is_new = alert, True
        if self.history is not None:
            try:
                stored, is_new = self.history.record(alert, self.cooldown)
            except FmError as exc:
                log.warning("alert history unavailable (%s); delivering without dedup", exc)
            except Exception as exc:  # noqa: BLE001 - a full disk must not swallow the alert
                # sqlite raises plain OperationalError for "disk full", "read-only database"
                # and "database is locked". Losing dedup is bad; losing the alert and the run
                # is worse, so the alert goes out un-deduped and the reason is logged.
                log.warning(
                    "alert history write failed (%s: %s); delivering without dedup", type(exc).__name__, exc
                )
        if not is_new:
            log.debug(
                "suppressing repeat of %s within cooldown (%s occurrences so far)",
                stored.effective_dedup_key,
                stored.occurrences,
            )
            delivery = Delivery(alert=stored, stored=True, is_new=False, suppressed_by="cooldown")
            self.deliveries.append(delivery)
            return delivery
        delivered: list[str] = []
        failed: list[str] = []
        for sink in self.sinks:
            try:
                ok = bool(sink.emit(stored))
            except Exception as exc:  # noqa: BLE001 - a sink failure is never a run failure
                log.warning("alert sink %s raised %s: %s", sink.name, type(exc).__name__, exc)
                ok = False
            (delivered if ok else failed).append(sink.name)
        delivery = Delivery(
            alert=stored, stored=True, is_new=True, delivered_to=tuple(delivered), failed=tuple(failed)
        )
        self.deliveries.append(delivery)
        return delivery

    def dispatch_all(self, alerts: Iterable[Alert]) -> list[Delivery]:
        return [self.dispatch(a) for a in alerts]

    @property
    def suppressed_count(self) -> int:
        return sum(1 for d in self.deliveries if not d.is_new)

    @property
    def failures(self) -> list[str]:
        out: list[str] = []
        for d in self.deliveries:
            out.extend(f"{name}: {d.alert.plugin}/{d.alert.key}" for name in d.failed)
        return out


def build_dispatcher(
    config: AppConfig | None = None,
    *,
    store_path: Path | None = None,
    data_dir: Path | None = None,
    console: bool | None = None,
    jsonl: bool | None = None,
    history: bool = True,
    webhook: WebhookSettings | None = None,
    printer: Printer = print,
    opener: Callable[..., Any] | None = None,
    cooldown: timedelta | None = None,
) -> AlertDispatcher:
    """Assemble the sinks the configuration asks for. The CLI's only sink wiring."""
    cfg = config or AppConfig.load(missing_ok=True)
    settings = webhook or cfg.webhook
    sinks: list[AlertSink] = [ConsoleSink(printer)] if (console if console is not None else cfg.alert_console) else []
    if jsonl if jsonl is not None else cfg.alert_jsonl:
        if data_dir is not None:
            sinks.append(JsonlSink(alert_log_path(data_dir)))
    alert_store: AlertHistory | None = None
    if history and store_path is not None:
        alert_store = AlertHistory.open(store_path)
    if settings.active and settings.url:
        sinks.append(
            WebhookSink(
                settings.url,
                timeout=settings.timeout,
                allow_insecure=settings.allow_insecure,
                opener=opener,
            )
        )
    return AlertDispatcher(
        sinks=sinks, history=alert_store, cooldown=cooldown or cfg.alert_cooldown
    )


__all__ = [
    "AlertDispatcher",
    "AlertSink",
    "ConsoleSink",
    "Delivery",
    "JsonlSink",
    "WebhookSink",
    "build_dispatcher",
]
