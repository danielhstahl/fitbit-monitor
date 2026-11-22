"""Where fitbit-monitor keeps its files (XDG-based, every one overridable).

The plugin system needs three of these: the config file (per-plugin options), the
store (what a plugin reads), and the data dir (where alert history and import
reports are written). The OAuth token file lives in the data dir too, so "where does
my token live" has one answer: :func:`credentials_path`, mode 0600.
Resolution order is documented once here and honoured everywhere:

1. an explicit CLI flag (``--config`` / ``--data-dir`` / ``--db``),
2. an environment variable (``FM_CONFIG`` / ``FM_DATA_DIR`` / ``FM_DB``),
3. the XDG default under ``$XDG_CONFIG_HOME`` / ``$XDG_DATA_HOME``.

Nothing in this module touches the network or creates directories as a side
effect of importing; call :func:`ensure_data_dir` when a command really needs one.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

APP_DIR = "fitbit-monitor"
CONFIG_FILENAME = "config.toml"
STORE_FILENAME = "store.sqlite3"
ALERT_LOG_FILENAME = "alerts.jsonl"
CREDENTIALS_FILENAME = "credentials.json"
IMPORTS_DIRNAME = "imports"
ETAG_CACHE_FILENAME = "http-etag-cache.json"


def config_home() -> Path:
    root = os.environ.get("XDG_CONFIG_HOME") or os.environ.get("FM_CONFIG_HOME")
    if root:
        return Path(root).expanduser()
    return Path.home() / ".config"


def data_home() -> Path:
    root = os.environ.get("XDG_DATA_HOME") or os.environ.get("FM_DATA_DIR")
    if root:
        return Path(root).expanduser()
    return Path.home() / ".local" / "share"


def default_config_path() -> Path:
    env = os.environ.get("FM_CONFIG")
    if env:
        return Path(env).expanduser()
    return config_home() / APP_DIR / CONFIG_FILENAME


def default_data_dir() -> Path:
    return data_home() / APP_DIR


def default_store_path(data_dir: Path | None = None) -> Path:
    env = os.environ.get("FM_DB")
    if env:
        return Path(env).expanduser()
    return (data_dir or default_data_dir()) / STORE_FILENAME


def alert_log_path(data_dir: Path) -> Path:
    return data_dir / ALERT_LOG_FILENAME


def credentials_path(data_dir: Path) -> Path:
    """Where the OAuth tokens live: written 0600, and never inside the store."""
    return data_dir / CREDENTIALS_FILENAME


def imports_dir(data_dir: Path) -> Path:
    """Where the JSON report of each import run is written."""
    return data_dir / IMPORTS_DIRNAME


def etag_cache_path(data_dir: Path) -> Path:
    return data_dir / ETAG_CACHE_FILENAME


@dataclass(frozen=True)
class Paths:
    """Resolved, request-scoped view of the three interesting paths."""

    config: Path
    data_dir: Path
    store: Path

    def ensure_data_dir(self) -> Path:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return self.data_dir

    def ensure_imports_dir(self) -> Path:
        directory = imports_dir(self.data_dir)
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    @property
    def credentials(self) -> Path:
        return credentials_path(self.data_dir)

    @property
    def imports(self) -> Path:
        return imports_dir(self.data_dir)

    @property
    def etag_cache(self) -> Path:
        return etag_cache_path(self.data_dir)

    @classmethod
    def resolve(
        cls,
        *,
        config: str | os.PathLike[str] | None = None,
        data_dir: str | os.PathLike[str] | None = None,
        store: str | os.PathLike[str] | None = None,
    ) -> "Paths":
        data = Path(data_dir).expanduser() if data_dir else default_data_dir()
        return cls(
            config=Path(config).expanduser() if config else default_config_path(),
            data_dir=data,
            store=Path(store).expanduser() if store else default_store_path(data),
        )
