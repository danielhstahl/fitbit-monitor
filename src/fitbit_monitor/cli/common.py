"""Shared CLI plumbing: resolved paths + config, the store handles, small formatters.

Every subcommand gets one :class:`Session`. It is the only place that knows how
``--config``/``--data-dir``/``--db`` and the environment turn into concrete paths,
so the command modules stay about their own job and stay testable.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Sequence

from ..api.auth import CredentialStore
from ..api.client import FitbitClient
from ..api.http import ReplayTransport, load_fixtures
from ..config import AppConfig
from ..errors import StoreUnavailableError, UsageError
from ..paths import Paths
from ..plugins.registry import PluginRegistry
from ..store.reader import ReadOnlyStore
from ..store.writer import SeriesStore
from ..timeutil import DateRange, parse_date_arg
from .. import logsafe

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_NO_STORE = 4


@dataclass
class Session:
    """Everything a command needs that is not its own arguments."""

    paths: Paths
    config: AppConfig
    verbose: int = 0
    log: logging.Logger = field(default_factory=lambda: logging.getLogger("fitbit_monitor.cli"))

    # --- stores -------------------------------------------------------------
    @property
    def store_path(self) -> Path:
        return self.config.store_path or self.paths.store

    @property
    def import_settings(self):
        return self.config.import_

    @property
    def api_settings(self):
        return self.config.api

    def credentials(self) -> CredentialStore:
        """Where the OAuth tokens live: ``[api] credentials_path`` or the data dir."""
        return CredentialStore(self.config.api.credentials_path or self.paths.credentials)

    def api_client(self, transport=None, fixtures: Path | str | None = None) -> FitbitClient:
        """The one API client a command uses, wired to this session's paths.

        ``fixtures`` (or ``FM_FIXTURES``) replays recorded responses from a directory
        instead of calling the network - what lets a rehearsal, and this project's own
        test suite, happen offline. ``fm auth login --manual`` covers the headless case
        of the login flow.
        """
        if transport is None:
            directory = fixtures or os.environ.get("FM_FIXTURES")
            if directory:
                transport = ReplayTransport(load_fixtures(Path(directory).expanduser()))
        return FitbitClient(
            self.config.api,
            self.credentials(),
            import_settings=self.config.import_,
            transport=transport,
            etag_cache_path=self.paths.etag_cache,
        )

    def read_store(self) -> ReadOnlyStore:
        path = self.store_path
        if not path.exists():
            raise StoreUnavailableError(
                f"no store at {path}",
                hint="run `fm dev seed --days 30` for sample data, or point --db at an existing store",
            )
        return ReadOnlyStore(path)

    def write_store(self) -> SeriesStore:
        self.paths.ensure_data_dir()
        return SeriesStore.open(self.store_path)

    # --- plugins ------------------------------------------------------------
    def registry(self, extra_dirs: Sequence[Path | str] = ()) -> PluginRegistry:
        return PluginRegistry.discover(self.config, directories=extra_dirs)

    def window(self, args: argparse.Namespace) -> DateRange:
        since = parse_date_arg(args.since)
        until = parse_date_arg(args.until, end_of_day=is_day_only(args.until))
        return DateRange(since=since, until=until)

    def with_overrides(self, overrides: Iterable[str]) -> "Session":
        """Apply ``--set name.key=value`` on top of the config file."""
        sections: dict[str, dict[str, Any]] = {k: dict(v) for k, v in self.config.plugin_sections.items()}
        for item in overrides:
            if "=" not in item or "." not in item.split("=", 1)[0]:
                raise UsageError(
                    f"--set {item!r} is not name.key=value",
                    hint="e.g. --set resting_trend.lookback_weeks=12",
                )
            lhs, raw = item.split("=", 1)
            plugin, _, key = lhs.partition(".")
            sections.setdefault(plugin, {})[key] = _scalar(raw)
        return replace(self, config=replace(self.config, plugin_sections=sections))


def _scalar(text: str) -> Any:
    lowered = text.strip().lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


def is_day_only(text: str) -> bool:
    """``--until 2026-05-31`` means the end of that day, not its first second."""
    stripped = text.strip()
    return len(stripped) == 10 or stripped.lower() in ("today", "yesterday")


# --- presentation helpers ----------------------------------------------------
def render_table(headers: Sequence[str], rows: Iterable[Sequence[object]]) -> str:
    cells = [[str(c) for c in row] for row in rows]
    widths = [len(h) for h in headers]
    for row in cells:
        for index, cell in enumerate(row):
            if index < len(widths):
                widths[index] = max(widths[index], _visible(cell))
    lines = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip()]
    for row in cells:
        lines.append("  ".join(str(c).ljust(widths[i]) for i, c in enumerate(row)).rstrip())
    return "\n".join(lines)


def _visible(text: str) -> int:
    return max(len(line) for line in str(text).splitlines() or [""])


def print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def add_common_args(parser: argparse.ArgumentParser) -> None:
    """Flags every command accepts (also settable on the top-level parser)."""
    parser.add_argument("--config", help="path to config.toml (default: $FM_CONFIG or ~/.config/fitbit-monitor/config.toml)")
    parser.add_argument("--data-dir", help="data directory (default: $XDG_DATA_HOME/fitbit-monitor)")
    parser.add_argument("--db", help="store database path (default: <data-dir>/store.sqlite3)")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="show plugin log records and tracebacks")
    parser.add_argument("-q", "--quiet", action="store_true", help="only print errors and results")


def session_from_args(args: argparse.Namespace) -> Session:
    paths = Paths.resolve(
        config=getattr(args, "config", None),
        data_dir=getattr(args, "data_dir", None),
        store=getattr(args, "db", None),
    )
    config = AppConfig.load(paths.config)
    if getattr(args, "db", None):
        config = replace(config, store_path=paths.store)
    elif config.store_path is None and getattr(args, "data_dir", None):
        config = replace(config, store_path=paths.store)
    return Session(paths=paths, config=config, verbose=getattr(args, "verbose", 0))


def configure_logging(verbose: int, quiet: bool) -> None:
    """Set up the root handler, with health-value redaction on it.

    Every log line fm writes passes a :class:`~fitbit_monitor.logsafe.RedactingFilter`,
    so a debug line that quotes a reading ends up as ``resting_hr=<redacted>``. Datasets,
    counts, dates and durations are left alone - see ``docs/data-policy.md``.
    """
    level = logging.DEBUG if verbose > 1 else logging.INFO if verbose else logging.ERROR if quiet else logging.WARNING
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")
    logsafe.install()


__all__ = [
    "EXIT_ERROR",
    "EXIT_NO_STORE",
    "EXIT_OK",
    "EXIT_USAGE",
    "Session",
    "add_common_args",
    "configure_logging",
    "print_json",
    "render_table",
    "session_from_args",
]
