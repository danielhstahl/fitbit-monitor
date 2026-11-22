"""Date handling. Everything stored and compared is UTC and timezone-aware.

Both the store and the plugin contract forbid naive datetimes, because a health
series spanning a DST change is exactly where naive arithmetic goes wrong. The
CLI accepts the friendly forms (``2026-05-01``, ``7d``, ``today``) and converts
them to aware UTC instants here, in one place, so plugins never parse strings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from .errors import UsageError

_RELATIVE_RE = re.compile(r"^(\d+)\s*([hdwm])$", re.IGNORECASE)
_UNITS = {"h": "hours", "d": "days", "w": "weeks", "m": "minutes"}


def utcnow() -> datetime:
    return datetime.now(UTC)


def ensure_utc(value: datetime, *, field: str = "datetime") -> datetime:
    """Return ``value`` in UTC. Naive input is a bug in the caller, not a guess."""
    if not isinstance(value, datetime):
        raise TypeError(f"{field} must be a datetime, got {type(value).__name__}")
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(f"{field} must be timezone-aware (use datetime.now(UTC)): {value!r}")
    return value.astimezone(UTC)


def to_day(value: datetime | date) -> date:
    if isinstance(value, datetime):
        return ensure_utc(value).date()
    return value


def parse_date_arg(text: str, *, end_of_day: bool = False) -> datetime:
    """Parse a user-supplied date expression into an aware UTC instant.

    Accepted: ``now``, ``today``, ``yesterday``, ``<YYYY-MM-DD>``,
    ``<YYYY-MM-DDTHH:MM[:SS][offset]>`` and relative offsets ``7d`` / ``12h`` /
    ``3w`` / ``90m`` measured backwards from now.
    """
    raw = text.strip()
    lowered = raw.lower()
    now = utcnow()
    if lowered == "now":
        return now
    if lowered == "today":
        return _day_start(now.date(), end_of_day=end_of_day)
    if lowered == "yesterday":
        return _day_start(now.date() - timedelta(days=1), end_of_day=end_of_day)

    relative = _RELATIVE_RE.match(raw)
    if relative:
        amount, unit = int(relative.group(1)), relative.group(2).lower()
        return now - timedelta(**{_UNITS[unit]: amount})

    try:
        if len(raw) == 10:
            return _day_start(date.fromisoformat(raw), end_of_day=end_of_day)
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise UsageError(
            f"cannot parse date {raw!r}",
            hint="use YYYY-MM-DD, an ISO-8601 datetime, now/today/yesterday, or 7d/12h/3w",
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _day_start(day: date, *, end_of_day: bool) -> datetime:
    if end_of_day:
        return datetime(day.year, day.month, day.day, tzinfo=UTC) + timedelta(days=1)
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


@dataclass(frozen=True)
class DateRange:
    """A half-open UTC window ``[since, until)`` - the window a plugin runs over."""

    since: datetime
    until: datetime

    def __post_init__(self) -> None:
        since = ensure_utc(self.since, field="since")
        until = ensure_utc(self.until, field="until")
        if until <= since:
            raise UsageError(
                f"--until ({until.date()}) must be after --since ({since.date()})",
                hint="a range is [since, until); use --since 7d for the last week",
            )
        object.__setattr__(self, "since", since)
        object.__setattr__(self, "until", until)

    @property
    def days(self) -> float:
        return (self.until - self.since).total_seconds() / 86400.0

    def iso(self) -> dict[str, str]:
        return {"since": self.since.isoformat(), "until": self.until.isoformat()}

    def __str__(self) -> str:  # pragma: no cover - display helper
        return f"{self.since.isoformat()} .. {self.until.isoformat()}"
