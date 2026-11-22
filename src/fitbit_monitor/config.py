"""``config.toml`` loading, and the plugin-scoped view of it.

Two audiences share this module. The host reads :class:`AppConfig`: where the store
is, which directories to scan for drop-in plugins, how alerts are delivered. A
plugin only ever sees :class:`PluginConfig`, which is a read-only window onto its
own ``[plugins.<name>]`` table plus the defaults it declared - it cannot read
another plugin's section, nor the webhook URL, nor anything else in the file.

Layout of the file::

    [store]
    path = "~/.local/share/fitbit-monitor/store.sqlite3"

    [plugins]
    dirs = ["~/.config/fitbit-monitor/plugins"]

    [plugins.resting_trend]
    enabled = true
    lookback_weeks = 12

    [alerts]
    cooldown = "36h"
    jsonl = true
    [alerts.webhook]
    url = "https://hooks.example/…"
    enabled = false
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Mapping

from .errors import ConfigError
from .paths import Paths

EMPTY: Mapping[str, Any] = {}


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return os.path.expandvars(os.path.expanduser(value))
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


@dataclass(frozen=True)
class PluginConfig(Mapping[str, Any]):
    """Read-only, plugin-scoped options with the plugin's own declared defaults.

    A plugin gets one of these and nothing else from the config file. Reads behave
    like a mapping (``cfg["lookback_weeks"]``) but the typed accessors give better
    errors, and unknown keys are reported rather than silently ignored.
    """

    plugin_name: str
    #: The user's ``[plugins.<name>]`` section, as written in config.toml.
    settings: Mapping[str, Any] = field(default_factory=dict)
    defaults: Mapping[str, Any] = field(default_factory=dict)
    #: Keys the *plugin* declares (its options plus ``enabled``). Anything the user
    #: set that is not in here shows up in :attr:`unknown_keys`.
    declared: frozenset[str] = frozenset()

    @property
    def enabled(self) -> bool:
        return bool(self.get("enabled", True))

    # --- Mapping: the plugin sees exactly its own section -------------------
    def __iter__(self):
        return iter(self.merged)

    def __len__(self) -> int:
        return len(self.merged)

    def __getitem__(self, key: str) -> Any:
        merged = self.merged
        if key not in merged:
            raise KeyError(f"{self.plugin_name}: no option {key!r}")
        return merged[key]

    @property
    def merged(self) -> dict[str, Any]:
        out: dict[str, Any] = dict(self.defaults)
        out.update(self.settings)
        return out

    @property
    def unknown_keys(self) -> tuple[str, ...]:
        """Keys set by the user that the plugin never declared (probably a typo)."""
        if not self.declared:
            return ()
        return tuple(sorted(k for k in self.settings if k not in self.declared))

    # --- typed accessors ---------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        """The value for ``key``, or ``default``. Unknown keys just yield the default."""
        return self.merged.get(key, default)

    def _coerce(self, key: str, value: Any, caster: Any, typename: str) -> Any:
        if isinstance(value, bool) and typename != "bool":
            raise ConfigError(f"{self.plugin_name}: option {key!r} must be {typename}, got a bool")
        try:
            return caster(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{self.plugin_name}: option {key!r} must be {typename}, got {value!r}") from exc

    def get_int(self, key: str, default: int) -> int:
        return int(self._coerce(key, self.get(key, default), int, "an integer"))

    def get_float(self, key: str, default: float) -> float:
        return float(self._coerce(key, self.get(key, default), float, "a number"))

    def get_bool(self, key: str, default: bool) -> bool:
        return bool(self._coerce(key, self.get(key, default), _to_bool, "a bool"))

    def get_str(self, key: str, default: str = "") -> str:
        value = self.get(key, default)
        if not isinstance(value, str):
            raise ConfigError(f"{self.plugin_name}: option {key!r} must be a string, got {value!r}")
        return value

    def get_duration(self, key: str, default: timedelta) -> timedelta:
        value = self.get(key, None)
        if value is None:
            return default
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return timedelta(seconds=float(value))
        return parse_duration(str(value), context=f"{self.plugin_name}: option {key!r}")

    def get_list(self, key: str, default: list[Any] | None = None) -> list[Any]:
        value = self.get(key, default if default is not None else [])
        if isinstance(value, (list, tuple)):
            return list(value)
        raise ConfigError(f"{self.plugin_name}: option {key!r} must be a list, got {value!r}")

    def as_dict(self) -> dict[str, Any]:
        return dict(self.merged)


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in {"1", "true", "yes", "on"}:
        return True
    if isinstance(value, str) and value.lower() in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"not a bool: {value!r}")


def parse_duration(text: str, *, context: str = "duration") -> timedelta:
    """Parse ``36h``, ``90m``, ``7d``, ``45s`` or a bare number of seconds."""
    raw = text.strip().lower()
    if not raw:
        raise ConfigError(f"{context}: empty duration")
    if raw.isdigit():
        return timedelta(seconds=int(raw))
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    total = 0.0
    rest = raw
    while rest:
        digits = ""
        while rest and (rest[0].isdigit() or rest[0] == "."):
            digits += rest[0]
            rest = rest[1:]
        if not digits or not rest or rest[0] not in units:
            raise ConfigError(f"{context}: cannot parse duration {text!r}", hint="use e.g. 45s, 90m, 36h, 7d")
        total += float(digits) * units[rest[0]]
        rest = rest[1:]
    return timedelta(seconds=total)


@dataclass(frozen=True)
class WebhookSettings:
    """Outbound alert webhook settings. Off unless a URL is configured."""

    url: str | None = None
    enabled: bool = False
    allow_insecure: bool = False
    timeout: float = 10.0

    @property
    def active(self) -> bool:
        return bool(self.enabled and self.url)


@dataclass(frozen=True)
class AppConfig:
    """The host-side half of ``config.toml``."""

    path: Path | None
    store_path: Path | None
    plugin_dirs: tuple[Path, ...]
    plugin_sections: Mapping[str, Mapping[str, Any]]
    plugin_defaults: Mapping[str, Any]
    alert_cooldown: timedelta
    alert_jsonl: bool
    alert_console: bool
    plugin_timeout: float
    webhook: WebhookSettings

    @classmethod
    def load(cls, path: Path | None = None, *, missing_ok: bool = True) -> "AppConfig":
        resolved = Path(path).expanduser() if path else _default_config()
        raw: dict[str, Any] = {}
        if resolved.exists():
            try:
                with resolved.open("rb") as handle:
                    raw = tomllib.load(handle)
            except tomllib.TOMLDecodeError as exc:
                raise ConfigError(f"{resolved}: invalid TOML: {exc}") from exc
        elif not missing_ok:
            raise ConfigError(f"config file not found: {resolved}")
        return cls._from_raw(resolved if resolved.exists() else None, raw)

    @classmethod
    def _from_raw(cls, path: Path | None, raw: Mapping[str, Any]) -> "AppConfig":
        store = raw.get("store", {}) or {}
        plugins = raw.get("plugins", {}) or {}
        alerts = raw.get("alerts", {}) or {}
        webhook = alerts.get("webhook", {}) or {}
        sections = {k: dict(v) for k, v in plugins.items() if isinstance(v, dict)}
        dirs = plugins.get("dirs", []) or []
        if isinstance(dirs, str):
            dirs = [dirs]
        env_dirs = os.environ.get("FM_PLUGIN_DIRS") or os.environ.get("FM_PLUGIN_DIR")
        if env_dirs:
            dirs = list(dirs) + [p for p in env_dirs.split(os.pathsep) if p]
        return cls(
            path=path,
            store_path=Path(_expand(store.get("path"))) if store.get("path") else None,
            plugin_dirs=tuple(Path(_expand(d)) for d in dirs),
            plugin_sections=sections,
            plugin_defaults=plugins.get("defaults", {}) if isinstance(plugins.get("defaults"), dict) else {},
            alert_cooldown=parse_duration(str(alerts.get("cooldown", "36h")), context="alerts.cooldown"),
            alert_jsonl=bool(alerts.get("jsonl", True)),
            alert_console=bool(alerts.get("console", True)),
            plugin_timeout=float(alerts.get("timeout_seconds", 120)),
            webhook=WebhookSettings(
                url=_expand(webhook.get("url")) or None,
                enabled=bool(webhook.get("enabled", bool(webhook.get("url")))),
                allow_insecure=bool(webhook.get("allow_insecure", False)),
                timeout=float(webhook.get("timeout_seconds", 10)),
            ),
        )

    def plugin_config(self, plugin_name: str, defaults: Mapping[str, Any] = EMPTY) -> PluginConfig:
        """The one slice of the config file a plugin is allowed to see."""
        values = self.plugin_sections.get(plugin_name, EMPTY)
        shared = {k: v for k, v in self.plugin_defaults.items() if k != "enabled"}
        merged_defaults = {**shared, **dict(defaults)}
        return PluginConfig(
            plugin_name=plugin_name,
            settings=dict(values),
            defaults=merged_defaults,
            # `enabled` is the host's on/off switch, valid for every plugin.
            declared=frozenset(set(merged_defaults) | {"enabled"}),
        )


def _default_config() -> Path:
    return Paths.resolve().config
