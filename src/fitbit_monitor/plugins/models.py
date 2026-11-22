"""The plugin-facing value models: :class:`MetricResult` and :class:`Alert`.

Both are frozen, both validate on construction, and both round-trip to JSON. The
validation is the contract: a plugin that emits a naive timestamp, a confidence of
``1.4``, an unparseable severity, or evidence out of temporal order fails at the
``ctx.metric(...)`` call with a message that names the field - not three layers
later in a sink.
"""

from __future__ import annotations

import json
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


def _aware(value: Any, field_name: str) -> datetime:
    try:
        return ensure_utc(value, field=field_name)
    except (ValueError, TypeError) as exc:
        raise ValidationError(str(exc)) from exc


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


@dataclass(frozen=True)
class Evidence:
    """One data point quoted by an alert: *when*, *what value*, *from what*."""

    at: datetime
    value: float
    dataset: str | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "at", _aware(self.at, "evidence.at"))
        if isinstance(self.value, bool) or not isinstance(self.value, (int, float)):
            raise ValidationError(f"evidence.value must be a number, got {self.value!r}")
        object.__setattr__(self, "value", float(self.value))

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
            at=datetime.fromisoformat(str(raw["at"])),
            value=float(raw["value"]),
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
        raise ValidationError(
            "evidence must be in chronological order "
            f"(got {out[0].at.isoformat()} before {out[1].at.isoformat()})",
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
        if isinstance(self.value, bool) or not isinstance(self.value, (int, float, str)):
            raise ValidationError(
                f"MetricResult.value must be a number or string, got {type(self.value).__name__}"
            )
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
            plugin=raw["plugin"],
            key=raw["key"],
            value=raw["value"],
            unit=raw.get("unit"),
            window_start=datetime.fromisoformat(str(raw["window_start"])),
            window_end=datetime.fromisoformat(str(raw["window_end"])),
            confidence=raw.get("confidence"),
            labels=raw.get("labels") or {},
            details=raw.get("details") or {},
            produced_at=datetime.fromisoformat(str(raw["produced_at"])) if raw.get("produced_at") else None,
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
        object.__setattr__(self, "title", _text(self.title, "Alert.title"))
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
        return cls(
            plugin=raw["plugin"],
            key=raw["key"],
            title=raw["title"],
            message=raw["message"],
            occurred_at=datetime.fromisoformat(str(raw["occurred_at"])),
            severity=Severity.parse(raw.get("severity", "warning")),
            confidence=raw.get("confidence"),
            evidence=tuple(Evidence.from_dict(e) for e in raw.get("evidence", [])),
            window_start=datetime.fromisoformat(str(raw["window_start"])) if raw.get("window_start") else None,
            window_end=datetime.fromisoformat(str(raw["window_end"])) if raw.get("window_end") else None,
            dedup_key=raw.get("dedup_key"),
            labels=raw.get("labels") or {},
            details=raw.get("details") or {},
            alert_id=raw.get("alert_id"),
            first_emitted_at=(
                datetime.fromisoformat(str(raw["first_emitted_at"])) if raw.get("first_emitted_at") else None
            ),
            last_seen_at=datetime.fromisoformat(str(raw["last_seen_at"])) if raw.get("last_seen_at") else None,
            occurrences=int(raw.get("occurrences", 1)),
        )


def coerce_evidence(items: Sequence[Any]) -> tuple[Evidence, ...]:
    """Public helper used by plugins that build evidence from tuples."""
    return _evidence(items)
