"""Keep measurements out of the log file.

A log is the easiest place for health data to leak quietly: it is written by
convenience, copied into bug reports, and left behind by ``--verbose`` runs. The policy
(``docs/data-policy.md``) says logs may name datasets, counts, dates and durations -
they may not contain readings. This module is how that promise is enforced rather than
merely intended: every record that reaches an ``fm`` handler passes through
:class:`RedactingFilter`, which replaces the *value* in any ``field = number`` shaped
text with ``<redacted>`` and leaves everything else alone.

It is deliberately narrow. Only a known health field name followed by a number is
masked, so paths, dataset names, row counts, timings, version numbers and timestamps
survive: a log line you cannot read is a log line that will get turned off.
"""

from __future__ import annotations

import logging
import re

#: Placeholder that replaces a reading. Deliberately loud, so a redacted line never
#: looks like a zero.
MASK = "<redacted>"

#: Field names whose values are health measurements. Longer names come first so the
#: alternation cannot match a shorter name inside a longer one.
HEALTH_FIELDS: tuple[str, ...] = (
    "resting_hr",
    "heart_rate",
    "active_minutes",
    "sleep_minutes",
    "awake_minutes",
    "deep_minutes",
    "light_minutes",
    "rem_minutes",
    "body_fat_pct",
    "weight_kg",
    "spo2_min",
    "spo2_avg",
    "spo2_max",
    "calories",
    "hrv_n",
    "sdnn",
    "rmssd",
    "min_hr",
    "max_hr",
    "steps",
    "spo2",
    "weight",
    "bpm",
    "hr",
)

#: Optional unit that may trail the number, so ``hr=61.0 bpm`` is masked whole.
_UNITS = r"(?:bpm|ms|kcal|minutes|mins|min|%)"

#: What may sit between a field name and its number: assignment (``hr=61``), a bare
#: space (``hr 61``), or a copula of being or measure (``resting_hr was 47``). A bare
#: wildcard would mask unrelated numbers - "hrv endpoint over 30 days" is not a reading -
#: so the words are listed rather than open-ended.
_COPULAS = (
    "is|was|were|be|of|to|at|near|around|about|roughly|exactly|mean|median|average|avg|"
    "reads|reading|value"
)

_VALUE_RE = re.compile(
    r"\b(?P<field>" + "|".join(HEALTH_FIELDS) + r")\b"
    rf"(?P<sep>\s*[=:]\s*|\s+(?:{_COPULAS})\s+|\s+)?"
    r"(?P<value>-?\d+(?:\.\d+)?)"
    rf"(?P<unit>\s*{_UNITS}\b)?",
    re.IGNORECASE,
)


def redact(text: str) -> str:
    """Mask health readings in ``text``. Everything else comes back unchanged."""
    if not text or not any(character.isdigit() for character in text):
        return text

    def replace(match: re.Match[str]) -> str:
        field = match.group("field")
        separator = (match.group("sep") or "=").strip().lower()
        if separator in ("", "=", ":"):
            return f"{field}={MASK}"
        return f"{field} {separator} {MASK}"

    return _VALUE_RE.sub(replace, text)


class RedactingFilter(logging.Filter):
    """Mask health values in every record that passes through.

    The record is flattened (``msg % args``) on the way past: a value that arrives as a
    deferred ``%s`` argument is exactly as readable in the log as one already formatted,
    and it is the case a reviewer cannot see by eyeballing a ``log.info`` call.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a broken % call is not this filter's problem
            return True
        redacted = redact(message)
        if redacted != message:
            record.msg = redacted
            record.args = None
        return True


#: Name the CLI gives its handler, so repeated ``basicConfig`` calls do not stack filters.
FILTER_NAME = "fitbit_monitor_redact"


def install(handler: logging.Handler | None = None, *, logger: logging.Logger | None = None) -> RedactingFilter:
    """Attach the filter to ``handler`` (default: every handler on the root logger)."""
    added = RedactingFilter()
    added.name = FILTER_NAME
    targets: list[logging.Handler] = [handler] if handler is not None else list((logger or logging.getLogger()).handlers)
    for target in targets:
        if not any(getattr(existing, "name", None) == FILTER_NAME for existing in target.filters):
            target.addFilter(added)
    return added


__all__ = ["FILTER_NAME", "HEALTH_FIELDS", "MASK", "RedactingFilter", "install", "redact"]
