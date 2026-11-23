"""One place that decides what an ``fm`` log line looks like, and how loud it is.

The CLI is a cron job as often as it is a terminal command, so the log is the record:
a refresh that failed, a dataset skipped for lack of scope, a webhook that 500'd. That
only works if the log is *parseable* by the reader who has to diagnose it, which is two
readers with two different tools - a human with ``less`` and a shell with ``grep`` and
``jq``. So there are two formats and one flag each:

``text`` (default)
    ``2026-11-23T18:04:11Z INFO fitbit_monitor.api.client: rate limit: 12s wait ...``
    timestamp, level, logger name, message - grep-shaped, one line per record.

``json``
    one JSON object per line: ``ts``, ``level``, ``logger``, ``module``, ``message``,
    plus any ``extra`` the emitter attached, plus a redacted ``exception``. Field order
    is fixed and the line is single-line, so ``jq`` and a log shipper both work.

Level comes from the command line first (``-v`` INFO, ``-vv`` DEBUG, ``-q`` ERROR,
``--log-level NAME``) and from ``FM_LOG_LEVEL`` otherwise; format from
``--log-format`` or ``FM_LOG_FORMAT``. Both are resolved here, once, so a command never
argues about it.

Every handler this module builds carries both redaction filters
(:mod:`~fitbit_monitor.logsecrets` and :mod:`~fitbit_monitor.logsafe`), which is where
secrets and health readings get removed - see those modules; the filter runs whatever
the format, so ``--log-format json`` is not a blinder.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import UTC, datetime
from typing import Any

from . import logsecrets, logsafe
from .errors import UsageError

#: Environment fallbacks, documented in the README and in ``fm --help``.
LEVEL_ENV = "FM_LOG_LEVEL"
FORMAT_ENV = "FM_LOG_FORMAT"

#: Acceptable level names, mapped to :mod:`logging` constants. ``warn``/``fatal`` are
#: accepted as the aliases people type.
LEVELS: dict[str, int] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warn": logging.WARNING,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
    "fatal": logging.CRITICAL,
}

#: Acceptable output formats.
FORMATS: tuple[str, ...] = ("text", "json")

#: Levels the flags produce, in order of verbosity.
VERBOSE_LEVELS = (logging.WARNING, logging.INFO, logging.DEBUG)

DEFAULT_LEVEL = logging.WARNING

#: Attributes every ``LogRecord`` carries, so anything else on the record is an
#: ``extra`` the emitter attached and belongs in the JSON output.
_RESERVED = {
    "args",
    "asctime",
    "created",
    "exc_info",
    "exc_text",
    "filename",
    "funcName",
    "levelname",
    "levelno",
    "lineno",
    "module",
    "msecs",
    "msg",
    "name",
    "pathname",
    "process",
    "processName",
    "relativeCreated",
    "stack_info",
    "taskName",
    "thread",
    "threadName",
    "message",
}


def level_name(value: int) -> str:
    """The lowercase name of a level (``20`` -> ``info``)."""
    return logging.getLevelName(value).lower()


def parse_level(value: str, *, source: str = LEVEL_ENV) -> int:
    """Turn ``DEBUG``/``debug``/``20`` into a level, or raise a usage error.

    A bad ``FM_LOG_LEVEL`` is an error rather than a silent default: a user who set it
    is trying to read something, and getting WARNING anyway is the worst outcome.
    """
    text = str(value).strip().lower()
    if text in LEVELS:
        return LEVELS[text]
    if text.isdigit():
        return int(text)
    raise UsageError(
        f"{source} must be one of {', '.join(sorted(LEVELS))} (or a number), got {value!r}",
        hint="e.g. FM_LOG_LEVEL=debug, or -v for info and -vv for debug",
    )


def resolve_level(*, verbose: int = 0, quiet: bool = False, level: str | None = None) -> int:
    """The level for this run. Flags beat the environment; ``-q`` beats ``-v``.

    Order: ``--log-level``, ``-q``, ``-vv``/``-v``, ``FM_LOG_LEVEL``, then WARNING.
    ``-q`` first because a user who passes both is asking to be surprised by less.
    """
    if level:
        return parse_level(level, source="--log-level")
    if quiet:
        return logging.ERROR
    if verbose > 0:
        return VERBOSE_LEVELS[min(int(verbose), len(VERBOSE_LEVELS) - 1)]
    env = os.environ.get(LEVEL_ENV)
    if env and env.strip():
        return parse_level(env)
    return DEFAULT_LEVEL


def resolve_format(fmt: str | None = None) -> str:
    """``text`` or ``json``: flag, then ``FM_LOG_FORMAT``, then ``text``."""
    raw = (fmt or os.environ.get(FORMAT_ENV) or "text").strip().lower()
    if raw not in FORMATS:
        raise UsageError(
            f"{FORMAT_ENV} must be one of {', '.join(FORMATS)}, got {fmt or raw!r}",
            hint="e.g. FM_LOG_FORMAT=json",
        )
    return raw


def stamp(record: logging.LogRecord) -> str:
    """Record time as ISO-8601 UTC with milliseconds (the same instant, two formats)."""
    moment = datetime.fromtimestamp(record.created, UTC)
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def extras(record: logging.LogRecord) -> dict[str, Any]:
    """The ``extra`` a caller attached to one record, keys sorted."""
    return {key: value for key, value in sorted(vars(record).items()) if key not in _RESERVED}


def exception_text(record: logging.LogRecord, formatter: logging.Formatter) -> str:
    """The formatted traceback, redacted - even if the filter was never installed.

    The filter normally pre-computes ``exc_text`` (see
    :func:`~fitbit_monitor.logsecrets.scrub_exception`); this is the second door, for a
    handler built somewhere the CLI did not configure.
    """
    if record.exc_text:
        return record.exc_text
    if record.exc_info and record.exc_info[1] is not None:
        return logsecrets.redact_secrets(formatter.formatException(record.exc_info))
    return ""


class TextFormatter(logging.Formatter):
    """``<timestamp> <LEVEL> <logger>: <message>`` - the grep-friendly line."""

    FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    DATE_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

    def __init__(self) -> None:
        super().__init__(self.FORMAT, datefmt=self.DATE_FORMAT)
        # UTC in the log, whatever the machine's timezone: an import runs at a fixed UTC
        # boundary and the store is UTC throughout.
        self.converter = time.gmtime


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with a fixed set of leading keys."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": stamp(record),
            "level": record.levelname.lower(),
            "logger": record.name,
            "module": record.module,
            "message": record.getMessage(),
        }
        for key, value in extras(record).items():
            payload.setdefault(key, value if isinstance(value, (int, float, bool, type(None))) else str(value))
        if record.stack_info:
            payload["stack"] = logsecrets.redact_secrets(self.formatStack(record.stack_info))
        if record.exc_info or record.exc_text:
            text = exception_text(record, self)
            if text:
                payload["exception"] = text
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def make_formatter(fmt: str) -> logging.Formatter:
    """The formatter for a format name."""
    if fmt == "json":
        return JsonFormatter()
    if fmt == "text":
        return TextFormatter()
    raise UsageError(f"unknown log format {fmt!r}", hint=f"choose one of: {', '.join(FORMATS)}")


def configure(
    *,
    verbose: int = 0,
    quiet: bool = False,
    level: str | None = None,
    format: str | None = None,
    stream: Any = None,
    logger: logging.Logger | None = None,
) -> logging.Handler:
    """Point the (root) logger at a level and a format, with redaction on top.

    Idempotent by construction: an existing handler is reconfigured rather than added
    to, and each filter is installed only once per handler, so a command that calls this
    twice - or a library that called it first - does not double-print or double-mask.

    Returns the handler the records will go to (the first one, when something else
    already installed handlers on the root logger).
    """
    resolved_level = resolve_level(verbose=verbose, quiet=quiet, level=level)
    formatter = make_formatter(resolve_format(format))
    root = logger if logger is not None else logging.getLogger()
    root.setLevel(resolved_level)
    handlers = list(root.handlers)
    if not handlers:
        handler: logging.Handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
        root.addHandler(handler)
        handlers = [handler]
    else:
        for existing in handlers:
            if stream is not None and isinstance(existing, logging.StreamHandler):
                existing.setStream(stream)
    for handler in handlers:
        handler.setLevel(resolved_level)
        handler.setFormatter(formatter)
        logsecrets.install(handler)
        logsafe.install(handler)
    return handlers[0]


__all__ = [
    "DEFAULT_LEVEL",
    "FORMATS",
    "FORMAT_ENV",
    "LEVELS",
    "LEVEL_ENV",
    "JsonFormatter",
    "TextFormatter",
    "configure",
    "exception_text",
    "extras",
    "level_name",
    "make_formatter",
    "parse_level",
    "resolve_format",
    "resolve_level",
    "stamp",
]
