"""The plugin-facing value models: :class:`MetricResult` and :class:`Alert`.

Both are frozen, both validate on construction, and both round-trip to JSON. The
validation is the contract: a plugin that emits a naive timestamp, a confidence of
``1.4``, an unparseable severity, a non-finite number, or evidence out of temporal
order fails at the ``ctx.metric(...)`` call with a message that names the field -
not three layers later in a sink. Reading a payload back with ``from_dict`` raises
the same :class:`ValidationError`, naming the field, so a malformed history line is
reported as a bad field rather than a bare ``KeyError``.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from ..errors import FmError
from ..timeutil import ensure_utc


class ValidationError(FmError, ValueError):
    """A model was built with a field that violates the contract."""

    exit_code = 1


class Severity(str, Enum):
    """Alert severity. Three levels, parsed from the usual aliases."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK[self]

    def at_least(self, other: "Severity | str") -> bool:
        return self.rank >= Severity.parse(other).rank

    @classmethod
    def parse(cls, value: "Severity | str") -> "Severity":
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            key = value.strip().lower()
            if key in _ALIASES:
                return _ALIASES[key]
            try:
                return cls(key)
            except ValueError:
                pass
        raise ValidationError(
            f"severity must be one of info, warning, critical (got {value!r})",
            hint="Severity is an enum: Severity.WARNING, or the strings 'info'/'warning'/'critical'",
        )


_SEVERITY_RANK = {Severity.INFO: 0, Severity.WARNING: 1, Severity.CRITICAL: 2}
_ALIASES: dict[str, Severity] = {
    "info": Severity.INFO,
    "informational": Severity.INFO,
    "notice": Severity.INFO,
    "warn": Severity.WARNING,
    "warning": Severity.WARNING,
    "moderate": Severity.WARNING,
    "severe": Severity.CRITICAL,
    "error": Severity.CRITICAL,
    "critical": Severity.CRITICAL,
    "high": Severity.CRITICAL,
}


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field_name} must be a non-empty string, got {value!r}")
    return value.strip()


def _one_line(value: Any, field_name: str) -> str:
    """A non-empty string with no line break in it.

    Titles are printed inline - one line of the console sink, one column of
    ``fm alerts list`` - so a line break inside one is a rendering bug the host
    cannot fix after the fact.
    """
    text = _text(value, field_name)
    if "\n" in text or "\r" in text:
        raise ValidationError(
            f"{field_name} must be a single line, got a value with a line break in it",
            hint=f"keep {field_name} short and put the detail in the message",
        )
    return text


def _aware(value: Any, field_name: str) -> datetime:
    try:
        return ensure_utc(value, field=field_name)
    except (ValueError, TypeError) as exc:
        raise ValidationError(str(exc)) from exc


def _stamp(value: Any, field_name: str) -> datetime:
    """Parse one field of a round-tripped payload into an aware UTC instant.

    ``from_dict`` reads JSON and stored rows, so a bad value here is somebody
    else's data, not a plugin's: it still has to fail by field name.
    """
    if isinstance(value, datetime):
        return _aware(value, field_name)
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field_name} is not an ISO-8601 timestamp, got {value!r}") from exc
    return _aware(parsed, field_name)


def _finite(value: Any, field_name: str) -> float:
    """A real, finite number. ``bool`` is not a measurement and ``nan`` is not a value."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{field_name} must be a number, got {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise ValidationError(
            f"{field_name} must be a finite number, got {value!r}",
            hint="a nan or infinite value means a computation over an empty or zero-sized window",
        )
    return number


def _confidence(value: Any, field_name: str = "confidence") -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{field_name} must be a number between 0 and 1, got {value!r}")
    number = float(value)
    if not 0.0 <= number <= 1.0:
        raise ValidationError(
            f"{field_name} must be within 0..1, got {number}",
            hint="confidence is a probability: 0.0 = no confidence, 1.0 = certain",
        )
    return number


def _frozen(values: Any) -> Mapping[str, Any]:
    if not values:
        return MappingProxyType({})
    if not isinstance(values, Mapping):
        raise ValidationError(f"expected a mapping, got {type(values).__name__}")
    return MappingProxyType(dict(values))


def _field(raw: Any, name: str, model_name: str) -> Any:
    """A required key of a parsed payload, with the model and field in the message."""
    if not isinstance(raw, Mapping):
        raise ValidationError(f"{model_name}: expected a mapping, got {type(raw).__name__}")
    if name not in raw:
        raise ValidationError(f"{model_name}.{name} is required, got keys {sorted(map(str, raw))}")
    return raw[name]


def _int(value: Any, field_name: str) -> int:
    """An int read out of a parsed payload, with the field named when it is not one."""
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field_name} must be an integer, got {value!r}") from exc


@dataclass(frozen=True)
class Evidence:
    """One data point quoted by an alert: *when*, *what value*, *from what*."""

    at: datetime
    value: float
    dataset: str | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "at", _aware(self.at, "evidence.at"))
        object.__setattr__(self, "value", _finite(self.value, "evidence.value"))

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"at": self.at.isoformat(), "value": self.value}
        if self.dataset:
            out["dataset"] = self.dataset
        if self.detail:
            out["detail"] = self.detail
        return out

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Evidence":
        return cls(
            at=_stamp(_field(raw, "at", "Evidence"), "evidence.at"),
            value=_finite(_field(raw, "value", "Evidence"), "evidence.value"),
            dataset=raw.get("dataset"),
            detail=raw.get("detail"),
        )


def _evidence(items: Any) -> tuple[Evidence, ...]:
    if not items:
        return ()
    if isinstance(items, Evidence):
        items = [items]
    out: list[Evidence] = []
    for item in items:
        if isinstance(item, Mapping):
            out.append(Evidence.from_dict(item))
        elif isinstance(item, Evidence):
            out.append(item)
        elif isinstance(item, (tuple, list)) and len(item) == 2:
            out.append(Evidence(at=item[0], value=item[1]))
        else:
            raise ValidationError(
                f"evidence items must be Evidence objects, {{at, value}} mappings or (at, value) pairs, got {item!r}"
            )
    ordered = tuple(sorted(out, key=lambda e: e.at))
    if len(out) > 1 and [e.at for e in out] != [e.at for e in ordered]:
        # Sorting silently would hide a plugin that built its timeline backwards.
        # Name the pair that actually breaks, which is not necessarily the first two.
        inversion = next(
            ((prev, nxt) for prev, nxt in zip(out, out[1:]) if nxt.at < prev.at),
            (out[0], out[1]),
        )
        raise ValidationError(
            "evidence must be in chronological order (got "
            f"{inversion[1].at.isoformat()} after {inversion[0].at.isoformat()})",
            hint="sort evidence oldest-first before emitting an alert",
        )
    return ordered


@dataclass(frozen=True)
class MetricResult:
    """A value a plugin computed over a window: the "metrics" half of the contract."""

    plugin: str
    key: str
    value: float | int | str
    window_start: datetime
    window_end: datetime
    unit: str | None = None
    confidence: float | None = None
    labels: Mapping[str, str] = field(default_factory=dict)
    details: Mapping[str, Any] = field(default_factory=dict)
    produced_at: datetime | None = None
    summary: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "plugin", _text(self.plugin, "MetricResult.plugin"))
        object.__setattr__(self, "key", _text(self.key, "MetricResult.key"))
        if isinstance(self.value, str):
            _text(self.value, "MetricResult.value")
        else:
            object.__setattr__(self, "value", _finite(self.value, "MetricResult.value"))
        object.__setattr__(self, "window_start", _aware(self.window_start, "MetricResult.window_start"))
        object.__setattr__(self, "window_end", _aware(self.window_end, "MetricResult.window_end"))
        if self.window_end < self.window_start:
            raise ValidationError(
                "MetricResult.window_end must not precede window_start "
                f"({self.window_end.isoformat()} < {self.window_start.isoformat()})"
            )
        object.__setattr__(self, "confidence", _confidence(self.confidence, "MetricResult.confidence"))
        object.__setattr__(self, "labels", _frozen(self.labels))
        object.__setattr__(self, "details", _frozen(self.details))
        if self.produced_at is not None:
            object.__setattr__(self, "produced_at", _aware(self.produced_at, "MetricResult.produced_at"))

    def with_window(self, since: datetime, until: datetime) -> "MetricResult":
        return replace(self, window_start=since, window_end=until)

    def as_dict(self) -> dict[str, Any]:
        return {
            "plugin": self.plugin,
            "key": self.key,
            "value": self.value,
            "unit": self.unit,
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "confidence": self.confidence,
            "labels": dict(self.labels),
            "details": dict(self.details),
            "produced_at": self.produced_at.isoformat() if self.produced_at else None,
            "summary": self.summary,
        }

    def as_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "MetricResult":
        return cls(
            plugin=_field(raw, "plugin", "MetricResult"),
            key=_field(raw, "key", "MetricResult"),
            value=_field(raw, "value", "MetricResult"),
            unit=raw.get("unit"),
            window_start=_stamp(_field(raw, "window_start", "MetricResult"), "MetricResult.window_start"),
            window_end=_stamp(_field(raw, "window_end", "MetricResult"), "MetricResult.window_end"),
            confidence=raw.get("confidence"),
            labels=raw.get("labels") or {},
            details=raw.get("details") or {},
            produced_at=_stamp(raw["produced_at"], "MetricResult.produced_at") if raw.get("produced_at") else None,
            summary=raw.get("summary"),
        )


@dataclass(frozen=True)
class Alert:
    """Something a plugin wants the user to know about: the "alerts" half."""

    plugin: str
    key: str
    title: str
    message: str
    occurred_at: datetime
    severity: Severity = Severity.WARNING
    confidence: float | None = None
    evidence: tuple[Evidence, ...] = ()
    window_start: datetime | None = None
    window_end: datetime | None = None
    dedup_key: str | None = None
    labels: Mapping[str, str] = field(default_factory=dict)
    details: Mapping[str, Any] = field(default_factory=dict)
    # Filled in by the alert store, not by the plugin:
    alert_id: int | None = None
    first_emitted_at: datetime | None = None
    last_seen_at: datetime | None = None
    occurrences: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "plugin", _text(self.plugin, "Alert.plugin"))
        object.__setattr__(self, "key", _text(self.key, "Alert.key"))
        object.__setattr__(self, "title", _one_line(self.title, "Alert.title"))
        object.__setattr__(self, "message", _text(self.message, "Alert.message"))
        object.__setattr__(self, "occurred_at", _aware(self.occurred_at, "Alert.occurred_at"))
        object.__setattr__(self, "severity", Severity.parse(self.severity))
        object.__setattr__(self, "confidence", _confidence(self.confidence, "Alert.confidence"))
        object.__setattr__(self, "evidence", _evidence(self.evidence))
        if self.window_start is not None:
            object.__setattr__(self, "window_start", _aware(self.window_start, "Alert.window_start"))
        if self.window_end is not None:
            object.__setattr__(self, "window_end", _aware(self.window_end, "Alert.window_end"))
        if self.window_start is not None and self.window_end is not None and self.window_end < self.window_start:
            raise ValidationError("Alert.window_end must not precede Alert.window_start")
        object.__setattr__(self, "labels", _frozen(self.labels))
        object.__setattr__(self, "details", _frozen(self.details))

    @property
    def effective_dedup_key(self) -> str:
        """What the sinks treat as "the same alert" for cooldown purposes."""
        return self.dedup_key or f"{self.plugin}/{self.key}/{self.severity.value}"

    def rendered(self) -> str:
        """The one-line human form printed by the console sink."""
        when = self.occurred_at.strftime("%Y-%m-%d %H:%M UTC")
        bits = [f"[{self.severity.value.upper()}]", f"{self.plugin}: {self.title}"]
        if self.confidence is not None:
            bits.append(f"(confidence {self.confidence:.2f})")
        bits.append(f"@ {when}")
        line = " ".join(bits)
        return f"{line}\n    {self.message}"

    def as_dict(self, *, full: bool = True) -> dict[str, Any]:
        """``full=False`` is the webhook body: the alert itself, nothing else."""
        out: dict[str, Any] = {
            "plugin": self.plugin,
            "key": self.key,
            "title": self.title,
            "message": self.message,
            "severity": self.severity.value,
            "occurred_at": self.occurred_at.isoformat(),
            "confidence": self.confidence,
            "evidence": [e.as_dict() for e in self.evidence],
            "dedup_key": self.dedup_key,
        }
        if full:
            out.update(
                {
                    "window_start": self.window_start.isoformat() if self.window_start else None,
                    "window_end": self.window_end.isoformat() if self.window_end else None,
                    "labels": dict(self.labels),
                    "details": dict(self.details),
                    "alert_id": self.alert_id,
                    "first_emitted_at": self.first_emitted_at.isoformat() if self.first_emitted_at else None,
                    "last_seen_at": self.last_seen_at.isoformat() if self.last_seen_at else None,
                    "occurrences": self.occurrences,
                }
            )
        return out

    def as_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Alert":
        if not isinstance(raw, Mapping):
            raise ValidationError(f"Alert: expected a mapping, got {type(raw).__name__}")
        fields = dict(raw)
        occurrences = _int(fields.get("occurrences", 1), "Alert.occurrences")
        return cls(
            plugin=_field(fields, "plugin", "Alert"),
            key=_field(fields, "key", "Alert"),
            title=_field(fields, "title", "Alert"),
            message=_field(fields, "message", "Alert"),
            occurred_at=_stamp(_field(fields, "occurred_at", "Alert"), "Alert.occurred_at"),
            severity=Severity.parse(fields.get("severity", "warning")),
            confidence=fields.get("confidence"),
            evidence=tuple(Evidence.from_dict(e) for e in fields.get("evidence") or ()),
            window_start=_stamp(fields["window_start"], "Alert.window_start") if fields.get("window_start") else None,
            window_end=_stamp(fields["window_end"], "Alert.window_end") if fields.get("window_end") else None,
            dedup_key=fields.get("dedup_key"),
            labels=fields.get("labels") or {},
            details=fields.get("details") or {},
            alert_id=_int(fields["alert_id"], "Alert.alert_id") if fields.get("alert_id") is not None else None,
            first_emitted_at=(
                _stamp(fields["first_emitted_at"], "Alert.first_emitted_at")
                if fields.get("first_emitted_at")
                else None
            ),
            last_seen_at=_stamp(fields["last_seen_at"], "Alert.last_seen_at") if fields.get("last_seen_at") else None,
            occurrences=occurrences,
        )


def coerce_evidence(items: Sequence[Any]) -> tuple[Evidence, ...]:
    """Public helper used by plugins that build evidence from tuples."""
    return _evidence(items)
