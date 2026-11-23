"""What the engine produces: typed findings, suppressed candidates, and a run summary.

A **finding** is an episode, not an opinion about an episode. It carries:

* a ``type`` from :data:`FINDING_TYPES` - which rule fired, and nothing more;
* an ``onset``/``offset`` pair (aware UTC, half-open ``[onset, offset)``) and the observed
  duration that follows from them;
* a ``confidence`` in ``0..1`` built from stated factors (see
  :func:`fitbit_monitor.anomaly.engine.confidence_for`), never a bare "high/medium/low";
* the **evidence series** that produced it: the sampled readings as
  :class:`~fitbit_monitor.plugins.models.Evidence` objects, the full value series in
  :attr:`Finding.values`, and the threshold, the personal level it was compared against
  and the spread that threshold was built from.

What a finding deliberately does **not** carry is any claim about what it *means*. There
is no "possible arrhythmia", no "see a doctor", no severity, no title, no message: those
are words about a person's health, and choosing them is the job of the plugin that owns
the user relationship (the hr-anomaly example). This library's promise is narrower and
entirely mechanical - *this stretch of your own heart-rate data is far from your own
ordinary values, for this long, on these readings* - and keeping the wording out is what
makes that promise auditable: :meth:`Finding.alert_args` hands a plugin every technical
field an :class:`~fitbit_monitor.plugins.models.Alert` needs and none of the prose.

Findings are also the deduplication unit. :attr:`Finding.dedup_key` is stable for the same
episode across overlapping runs (rule, context and the local day the episode started on),
so an engine run whose window moved by a day, or a re-run of yesterday's range, arrives at
the alert sink with the key the sink's cooldown already knows. See
:mod:`fitbit_monitor.plugins.sinks` and :mod:`fitbit_monitor.store.alerts` - the engine
merges its own overlapping episodes and stops there; *who gets told again, and when* is
the sink's decision, and only the sink's.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence

from ..hr.util import iso, minutes
from ..plugins.models import Evidence
from .config import AnomalyConfig
from .errors import AnomalyValueError

#: One sustained stretch well above the wearer's own resting level.
FINDING_SUSTAINED_HIGH = "sustained_high"
#: One sustained stretch well below it.
FINDING_SUSTAINED_LOW = "sustained_low"
#: One stretch whose epoch-to-epoch scatter is far above the wearer's own ordinary scatter.
#: The dev-seed label for the same shape is ``irregular``
#: (:data:`~fitbit_monitor.store.seed.ANOMALY_KINDS`), which is what a labelled fixture
#: suite maps on.
FINDING_IRREGULAR = "irregular_rhythm"

#: Every type a finding may carry, in the order a report should list them.
FINDING_TYPES: tuple[str, ...] = (
    FINDING_SUSTAINED_HIGH,
    FINDING_SUSTAINED_LOW,
    FINDING_IRREGULAR,
)

#: The rate rules, which share everything except the direction they look in.
RATE_FINDING_TYPES: tuple[str, ...] = (FINDING_SUSTAINED_HIGH, FINDING_SUSTAINED_LOW)

#: The unit of :attr:`Finding.observed` for each type. The irregularity index is a ratio of
#: two millisecond quantities, which is why it is dimensionless rather than ``ms``.
FINDING_UNITS: dict[str, str] = {
    FINDING_SUSTAINED_HIGH: "beats per minute",
    FINDING_SUSTAINED_LOW: "beats per minute",
    FINDING_IRREGULAR: "irregularity index (dimensionless)",
}


@dataclass(frozen=True)
class Finding:
    """One episode, with the numbers that made it and the evidence that backs it."""

    type: str
    context: str
    onset: datetime
    offset: datetime
    confidence: float
    heuristic: str
    #: The value that carried the finding: the peak rate (high), the lowest rate (low),
    #: or the largest scatter index (irregular). Always in :attr:`unit`.
    observed: float
    #: The value the rule compared ``observed`` against - personal, and printed so a
    #: reader can see how far past it the episode actually was.
    threshold: float
    unit: str
    #: The wearer's own level the threshold was built from, and its robust spread.
    baseline_level: float
    baseline_spread: float
    baseline_days: int
    baseline_established: bool
    #: Readings the episode is made of, and how much of its claimed duration was observed.
    samples: int
    coverage: float
    #: Mean of the values in the episode (rates, or block indices for an irregular one).
    mean: float
    #: Every value in the episode, oldest first. Full fidelity; :attr:`evidence` is the
    #: sampled form a sink can carry.
    values: tuple[float, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    #: The local date (in the run's timezone) the episode started on - the dedup anchor.
    anchor: str = ""
    detail: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.type not in FINDING_TYPES:
            raise AnomalyValueError(
                f"unknown finding type {self.type!r}",
                hint=f"a finding's type is one of: {', '.join(FINDING_TYPES)}",
            )
        for name in ("onset", "offset"):
            value = getattr(self, name)
            if not isinstance(value, datetime) or value.tzinfo is None:
                raise AnomalyValueError(
                    f"Finding.{name} must be a timezone-aware datetime, got {value!r}"
                )
        if self.offset <= self.onset:
            raise AnomalyValueError(
                f"Finding.offset must follow onset ({iso(self.offset)} <= {iso(self.onset)})",
                hint="an episode is half-open [onset, offset) and must have positive length",
            )
        if not isinstance(self.confidence, (int, float)) or isinstance(self.confidence, bool):
            raise AnomalyValueError(f"Finding.confidence must be a number, got {self.confidence!r}")
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise AnomalyValueError(
                f"Finding.confidence must be within 0..1, got {self.confidence}",
                hint="confidence is a probability: see engine.confidence_for",
            )
        object.__setattr__(self, "confidence", float(self.confidence))
        if self.samples < 0 or self.coverage < 0:
            raise AnomalyValueError("Finding.samples and Finding.coverage must not be negative")

    # --- shape --------------------------------------------------------------
    @property
    def duration(self) -> timedelta:
        return self.offset - self.onset

    @property
    def duration_minutes(self) -> float:
        """Observed duration in minutes, to 0.1 - the reporting unit for a duration."""
        return minutes(self.duration.total_seconds())

    @property
    def occurred_at(self) -> datetime:
        """What an alert should stamp this episode with: its onset."""
        return self.onset

    @property
    def dedup_key(self) -> str:
        """The key the alert sinks treat as "the same alert"."""
        anchor = self.anchor or self.onset.date().isoformat()
        return f"{self.type}/{self.context}/{anchor}"

    @property
    def margin(self) -> float:
        """How far past the threshold the episode went, in the finding's own unit.

        Positive by construction for a finding that fired: the high and irregular rules
        measure upwards, the low rule measures downwards.
        """
        if self.type == FINDING_SUSTAINED_LOW:
            return self.threshold - self.observed
        return self.observed - self.threshold

    @property
    def margin_sigma(self) -> float:
        """:attr:`margin` in units of the wearer's own spread. Scale-free, so two findings
        from different wearers can be compared."""
        if self.baseline_spread <= 0:
            return 0.0
        return self.margin / self.baseline_spread

    # --- output -------------------------------------------------------------
    def as_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "context": self.context,
            "onset": iso(self.onset),
            "offset": iso(self.offset),
            "duration_minutes": self.duration_minutes,
            "confidence": round(self.confidence, 3),
            "heuristic": self.heuristic,
            "observed": round(self.observed, 3),
            "threshold": round(self.threshold, 3),
            "unit": self.unit,
            "mean": round(self.mean, 3),
            "samples": self.samples,
            "coverage": round(self.coverage, 4),
            "baseline": {
                "level": round(self.baseline_level, 2),
                "spread": round(self.baseline_spread, 3),
                "days": self.baseline_days,
                "established": self.baseline_established,
            },
            "anchor": self.anchor,
            "dedup_key": self.dedup_key,
            "evidence": [item.as_dict() for item in self.evidence],
            "values": [round(value, 3) for value in self.values],
            **({"detail": dict(self.detail)} if self.detail else {}),
        }

    def alert_args(self) -> dict[str, Any]:
        """Every technical keyword argument an :class:`Alert` needs, minus the prose.

        ``key``, ``occurred_at``, ``confidence``, ``evidence``, the window, the
        ``dedup_key`` and the machine-readable ``labels``/``details`` come from here; the
        plugin supplies ``plugin``, ``title``, ``message`` and - because that is a claim
        about a person rather than a measurement - ``severity``.
        """
        return {
            "key": self.type,
            "occurred_at": self.occurred_at,
            "confidence": round(self.confidence, 3),
            "evidence": tuple(self.evidence),
            "window_start": self.onset,
            "window_end": self.offset,
            "dedup_key": self.dedup_key,
            "labels": {
                "finding_type": self.type,
                "context": self.context,
                "heuristic": self.heuristic,
                "baseline": "established" if self.baseline_established else "provisional",
            },
            "details": {
                "duration_minutes": self.duration_minutes,
                "observed": round(self.observed, 2),
                "threshold": round(self.threshold, 2),
                "unit": self.unit,
                "mean": round(self.mean, 2),
                "samples": self.samples,
                "coverage": round(self.coverage, 4),
                "margin": round(self.margin, 2),
                "margin_sigma": round(self.margin_sigma, 2),
                "baseline_level": round(self.baseline_level, 2),
                "baseline_spread": round(self.baseline_spread, 3),
                "baseline_days": self.baseline_days,
                "baseline_established": self.baseline_established,
                "values": [round(value, 2) for value in self.values[:_MAX_DETAIL_VALUES]],
                **dict(self.detail),
            },
        }


#: ``Alert.details`` ends up in a database row and a webhook body, so the raw series in
#: it is capped; :attr:`Finding.values` and :attr:`Finding.evidence` are the full series.
_MAX_DETAIL_VALUES = 40


@dataclass(frozen=True)
class SuppressedEpisode:
    """A stretch that crossed a rule and was then held back, with the reason.

    A plugin's ``--json`` output is required to show suppression, and the only way it can
    is if the engine says which candidates it dropped instead of dropping them silently.
    """

    type: str
    context: str
    onset: datetime
    offset: datetime
    reason: str
    observed: float | None = None
    threshold: float | None = None
    #: The stretch's own mean, which is the number the nocturnal-attribution decision is made
    #: on - and useful to a reader anyway: "held back, and here is what it averaged".
    mean: float | None = None
    detail: Mapping[str, Any] = field(default_factory=dict)

    @property
    def duration_minutes(self) -> float:
        return minutes((self.offset - self.onset).total_seconds())

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "context": self.context,
            "onset": iso(self.onset),
            "offset": iso(self.offset),
            "duration_minutes": self.duration_minutes,
            "reason": self.reason,
            "observed": None if self.observed is None else round(self.observed, 3),
            "threshold": None if self.threshold is None else round(self.threshold, 3),
            "mean": None if self.mean is None else round(self.mean, 3),
            **({"detail": dict(self.detail)} if self.detail else {}),
        }


@dataclass(frozen=True)
class RuleNote:
    """A rule that did not run, and why: "no baseline yet" has to be a reported answer."""

    rule: str
    context: str
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {"rule": self.rule, "context": self.context, "reason": self.reason}


@dataclass(frozen=True)
class DetectionResult:
    """Everything one engine run produced.

    ``findings`` are ordered by onset and already merged and deduplicated; ``suppressed``
    are the candidates held back (thin coverage, too short, below the duration floor);
    ``notes`` are the rules that never ran for lack of a baseline. All three are part of
    the result a plugin prints - a run that says nothing and explains nothing is the
    failure mode this package exists to avoid.
    """

    since: datetime
    until: datetime
    config: AnomalyConfig
    findings: tuple[Finding, ...] = ()
    suppressed: tuple[SuppressedEpisode, ...] = ()
    notes: tuple[RuleNote, ...] = ()
    #: One baseline per context that was asked for (established, provisional or refused).
    baselines: Mapping[str, Any] = field(default_factory=dict)
    #: Per-context scatter-index baselines, the same shape as ``baselines``.
    scatter_baselines: Mapping[str, Any] = field(default_factory=dict)
    days_scanned: int = 0
    samples_scanned: int = 0

    # --- queries ------------------------------------------------------------
    def by_type(self, finding_type: str) -> tuple[Finding, ...]:
        return tuple(finding for finding in self.findings if finding.type == finding_type)

    def by_context(self, context: str) -> tuple[Finding, ...]:
        return tuple(finding for finding in self.findings if finding.context == context)

    @property
    def highest_confidence(self) -> Finding | None:
        return max(self.findings, key=lambda finding: finding.confidence) if self.findings else None

    def counts(self) -> dict[str, int]:
        out = {f"by_type:{name}": len(self.by_type(name)) for name in FINDING_TYPES}
        out.update(
            {
                "findings": len(self.findings),
                "suppressed": len(self.suppressed),
                "notes": len(self.notes),
            }
        )
        return out

    # --- output -------------------------------------------------------------
    def as_dict(self) -> dict[str, Any]:
        return {
            "since": iso(self.since),
            "until": iso(self.until),
            "timezone": self.config.time_zone or "UTC",
            "days_scanned": self.days_scanned,
            "samples_scanned": self.samples_scanned,
            **self.counts(),
            "baselines": {name: baseline.as_dict() for name, baseline in self.baselines.items()},
            "scatter_baselines": {
                name: baseline.as_dict() for name, baseline in self.scatter_baselines.items()
            },
            "findings": [finding.as_dict() for finding in self.findings],
            "suppressed": [episode.as_dict() for episode in self.suppressed],
            "notes": [note.as_dict() for note in self.notes],
            "config": self.config.as_dict(),
        }


def sample_points(
    points: Sequence[tuple[datetime, float]],
    *,
    limit: int,
    dataset: str | None = None,
    keep: str | None = None,
    note: str | None = None,
) -> tuple[Evidence, ...]:
    """Turn ``(instant, value)`` pairs into at most ``limit`` :class:`Evidence` points.

    Evenly spaced, with the first and last kept; when ``keep`` names a direction the
    extreme in that direction is forced into the sample (replacing whichever sampled point
    it is nearest) and carries ``note`` in its ``detail``, so a reader always sees the
    episode's edges *and* the reading that actually crossed the threshold. Chronological
    order is preserved, which the ``Evidence`` contract insists on anyway.
    """
    if not points:
        return ()
    extreme: int | None = None
    if keep == "max":
        extreme = max(range(len(points)), key=lambda index: points[index][1])
    elif keep == "min":
        extreme = min(range(len(points)), key=lambda index: points[index][1])
    if len(points) <= limit:
        indices = list(range(len(points)))
    else:
        step = (len(points) - 1) / (limit - 1)
        indices = sorted({round(index * step) for index in range(limit)})
    if extreme is not None and extreme not in indices:
        nearest = min(indices, key=lambda index: abs(index - extreme))
        indices[indices.index(nearest)] = extreme
        indices = sorted(indices)
    return tuple(
        Evidence(
            at=points[index][0],
            value=points[index][1],
            dataset=dataset,
            detail=(note if index == extreme else None),
        )
        for index in indices
    )


__all__ = [
    "FINDING_IRREGULAR",
    "FINDING_SUSTAINED_HIGH",
    "FINDING_SUSTAINED_LOW",
    "FINDING_TYPES",
    "FINDING_UNITS",
    "RATE_FINDING_TYPES",
    "DetectionResult",
    "Finding",
    "RuleNote",
    "SuppressedEpisode",
    "sample_points",
]
