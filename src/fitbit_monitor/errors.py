"""The error hierarchy shared by the store, the plugin system and the CLI.

Every error raised on purpose by this package derives from :class:`FmError`, so a
caller that wants to be safe catches one type. The ``exit_code`` attribute is what
the CLI maps an escaping exception to; commands raise instead of printing so the
argument parser stays thin and everything below it stays testable.
"""

from __future__ import annotations


class FmError(Exception):
    """Base class for every expected failure in fitbit-monitor."""

    exit_code = 1

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:  # pragma: no cover - trivial
        if self.hint:
            return f"{self.message}\n  hint: {self.hint}"
        return self.message


class UsageError(FmError):
    """The command line or a config value was wrong. Nothing was changed."""

    exit_code = 2


class ConfigError(UsageError):
    """``config.toml`` is missing, unreadable, or holds an invalid value."""


class StoreError(FmError):
    """The local store is missing, unreadable, or from a schema we do not know."""

    exit_code = 4


class StoreUnavailableError(StoreError):
    """The store database file does not exist yet."""

    exit_code = 4
