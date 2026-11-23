"""The personal baseline: robust statistics over the wearer's own resting days.

Nothing in this module knows what a normal heart rate is. It knows how to describe *one
person's* resting rate well enough that a rule can say "far from this", and it does it with
statistics chosen for the two ways a heart-rate baseline goes wrong.

**Median, not mean; MAD, not standard deviation.** The episodes this engine looks for are
outliers - a night at 43 bpm in a wearer whose ordinary nights are 57, an afternoon at 130
in someone whose ordinary resting rate is 60. A mean and a standard deviation are dragged
by exactly those points, so a detector whose baseline is a mean learns to tolerate its own
false positives: three abnormal weeks in a row and "abnormal" becomes the average. The
median and the median absolute deviation have a 50% breakdown point - up to half the days
can be pathological before the level moves - and they are what is used here
(:data:`MAD_TO_SIGMA` turns the MAD into a scale comparable with a standard deviation for a
normal-ish distribution, so a z-score of 3.5 in this module means roughly what it means
anywhere else).

**Spreads, combined in quadrature.** A rate varies for three independent reasons: the
wearer's level on Tuesday genuinely differs from their level on Saturday (day to day), the
readings *inside* one quiet window jitter around that window's mean (resting jitter), and -
dominating everything - the part of the day the rules actually scan is wide, because an awake
human being stands up, walks, climbs stairs and gets stressed. :attr:`RateBaseline.sigma_bpm`
is ``sqrt(day_to_day ** 2 + rule_spread ** 2)`` where ``rule_spread`` is the wider of the
resting and detection-domain spreads, so a z-score is measured against the variation of the
set a rule is looking at. Use the quiet-window spread alone and an ordinary Tuesday
afternoon looks abnormal; use the day spread and still ask only 3 bpm of the wearer and a
brisk walk becomes a finding. The measured spread is what makes that trade-off data-driven
instead of a constant argued about forever.

**A refusal is an answer.** Below ``provisional_min_days`` usable resting days there is no
baseline: :attr:`RateBaseline.usable` is False, :attr:`~RateBaseline.reason` says how many
days there were, and the engine reports a :class:`~fitbit_monitor.anomaly.models.RuleNote`
instead of a finding. Between that and ``min_baseline_days`` the baseline is *provisional*:
usable, but every finding it produced is capped at
:attr:`~fitbit_monitor.anomaly.config.AnomalyConfig.provisional_confidence_cap`. A detector
that calls three days of data "unusual for you" is lying about how much it knows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Mapping, Sequence

from ..hr.util import mean, median
from .config import AnomalyConfig

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .context import ContextDay as ContextDayLike


#: Multiply a median absolute deviation by this and it estimates a standard deviation for
#: normally distributed data (``1 / Phi^-1(0.75)``). It is the usual Iglewicz-Hoaglin
#: constant, and it is here so that ``z_high = 3.5`` in the config means "about a 1-in-2000
#: outlier" rather than "three times whatever this arbitrary number happens to be".
MAD_TO_SIGMA = 1.4826


def median_absolute_deviation(values: Sequence[float]) -> float:
    """``median(|x - median(x)|)`` - the 50%-breakdown companion to the median."""
    if not values:
        return 0.0
    centre = median(values)
    return median([abs(float(value) - centre) for value in values])


def robust_sigma(values: Sequence[float]) -> float:
    """MAD scaled to a standard deviation. ``0.0`` for fewer than two values."""
    if len(values) < 2:
        return 0.0
    return MAD_TO_SIGMA * median_absolute_deviation(values)


@dataclass(frozen=True)
class RateBaseline:
    """One context's personal resting level, its spread, and the thresholds they imply.

    Built by :func:`build_rate_baseline`. Read :attr:`usable` before asking for a
    threshold: a baseline that refused is not a baseline of zero, and comparing rates
    against ``None`` is how a detector invents findings.
    """

    context: str
    #: ``(local day, that day's mean rate in the context)``, oldest first, trimmed to the
    #: baseline window and with the unusable days already dropped.
    daily: tuple[tuple[date, float], ...] = ()
    #: Median of the per-day spreads *inside* the resting windows, in bpm: how much a
    #: quiet stretch of this wearer's day jitters.
    within_spread_bpm: float = 0.0
    #: Median of the per-day spreads of the detection domain (the awake, non-exercise part
    #: of the day for ``rest``; the night for ``sleep``), in bpm. This is usually the much
    #: larger number, and it is the one the thresholds are built from: the bar has to be
    #: measured in the variation of the set the rule actually scans, not of the quietest
    #: window the level came from. See :mod:`fitbit_monitor.anomaly.context`.
    domain_spread_bpm: float = 0.0
    config: AnomalyConfig = field(default_factory=AnomalyConfig)
    #: How many days the window actually covered (before dropping the unusable ones), so a
    #: report can say "3 of 28 days had a usable resting window".
    days_scanned: int = 0

    # --- size ---------------------------------------------------------------
    @property
    def days(self) -> int:
        return len(self.daily)

    @property
    def values(self) -> tuple[float, ...]:
        return tuple(value for _day, value in self.daily)

    # --- level and scale ----------------------------------------------------
    @property
    def level_bpm(self) -> float | None:
        """The wearer's personal resting rate: the median of their per-day resting means."""
        if not self.values:
            return None
        return median(self.values)

    @property
    def day_spread_bpm(self) -> float:
        """Day-to-day robust spread of the per-day means, in bpm."""
        return robust_sigma(self.values)

    @property
    def rule_spread_bpm(self) -> float:
        """The spread the high rule is built from: the wider of resting and domain spread."""
        return max(self.domain_spread_bpm, self.within_spread_bpm)

    @property
    def quiet_spread_bpm(self) -> float:
        """The spread the low rule is built from: day-to-day and *resting* jitter only.

        Asymmetric on purpose. Being active raises a heart rate and never lowers one, so the
        wide swing of an awake day is no evidence that a rate well below the resting level is
        ordinary; measuring "too low" against the awake spread would push the low threshold
        below anything short of a medical emergency and make the bradycardia half of the rule
        decorative. The quiet spread is the right yardstick for "lower than this person ever
        goes".
        """
        return self._sigma(self.within_spread_bpm)

    def _sigma(self, rule_spread: float) -> float:
        combined = (self.day_spread_bpm**2 + rule_spread**2) ** 0.5
        return max(combined, self.config.min_spread_bpm)

    @property
    def sigma_bpm(self) -> float:
        """Total spread the high rule divides by: day-to-day and within-domain, in quadrature."""
        return self._sigma(self.rule_spread_bpm)

    def spread_for(self, direction: int) -> float:
        """The spread a direction's threshold was built from (``+1`` high, ``-1`` low).

        Reported with every finding, because "3 spreads past your baseline" is the only
        scale-free way two different wearers' findings can be compared at all.
        """
        return self.sigma_bpm if direction > 0 else self.quiet_spread_bpm

    @property
    def spread_bpm(self) -> float:
        """Alias for :attr:`sigma_bpm` - the high-side scale, the wider of the two."""
        return self.sigma_bpm

    # --- usability ----------------------------------------------------------
    @property
    def usable(self) -> bool:
        return self.level_bpm is not None and self.days >= self.config.provisional_min_days

    @property
    def established(self) -> bool:
        return self.usable and self.days >= self.config.min_baseline_days

    @property
    def provisional(self) -> bool:
        return self.usable and not self.established

    @property
    def reason(self) -> str | None:
        """Why there is no baseline, or why it is only provisional. ``None`` when established."""
        if not self.usable:
            return (
                f"only {self.days} usable resting day(s) in the {self.days_scanned or self.config.baseline_window_days}"
                f"-day window, need {self.config.provisional_min_days} for any personal baseline"
            )
        if not self.established:
            return (
                f"{self.days} of {self.config.min_baseline_days} resting days needed for an established "
                "baseline: findings are capped as provisional"
            )
        return None

    @property
    def confidence_factor(self) -> float:
        """Baseline maturity, ``0..1``: 1.0 once established, linear before it."""
        if self.established:
            return 1.0
        return max(0.0, min(1.0, self.days / self.config.min_baseline_days))

    # --- thresholds ---------------------------------------------------------
    def _threshold(self, z: float, min_delta: float, *, direction: int) -> float | None:
        level = self.level_bpm
        if level is None or not self.usable:
            return None
        delta = max(z * self.spread_for(direction), min_delta)
        return level + direction * delta

    @property
    def high_threshold_bpm(self) -> float | None:
        """``level + max(z_high * sigma, min_high_delta_bpm)`` - see the module doc."""
        return self._threshold(self.config.z_high, self.config.min_high_delta_bpm, direction=1)

    @property
    def low_threshold_bpm(self) -> float | None:
        """``level - max(z_low * sigma, min_low_delta_bpm)``."""
        return self._threshold(self.config.z_low, self.config.min_low_delta_bpm, direction=-1)

    def as_dict(self) -> dict[str, object]:
        level = self.level_bpm
        return {
            "context": self.context,
            "usable": self.usable,
            "established": self.established,
            "provisional": self.provisional,
            "reason": self.reason,
            "days": self.days,
            "days_scanned": self.days_scanned,
            "days_required": self.config.min_baseline_days,
            "level_bpm": None if level is None else round(level, 2),
            "day_spread_bpm": round(self.day_spread_bpm, 3),
            "within_spread_bpm": round(self.within_spread_bpm, 3),
            "domain_spread_bpm": round(self.domain_spread_bpm, 3),
            "rule_spread_bpm": round(self.rule_spread_bpm, 3),
            "sigma_bpm": round(self.sigma_bpm, 3),
            "quiet_sigma_bpm": round(self.quiet_spread_bpm, 3),
            "confidence_factor": round(self.confidence_factor, 3),
            "high_threshold_bpm": None
            if self.high_threshold_bpm is None
            else round(self.high_threshold_bpm, 2),
            "low_threshold_bpm": None
            if self.low_threshold_bpm is None
            else round(self.low_threshold_bpm, 2),
            "unit": "beats per minute",
            "daily": [
                {"day": day.isoformat(), "mean_bpm": round(value, 2)} for day, value in self.daily
            ],
        }


def build_rate_baseline(
    context: str,
    days: Sequence["ContextDayLike"],
    config: AnomalyConfig | None = None,
) -> RateBaseline:
    """Build one context's baseline from that context's per-day samples.

    ``days`` is the :class:`~fitbit_monitor.anomaly.context.ContextDay` list for the
    context, oldest first. A day with no usable resting window is dropped from the level
    here rather than being carried as a zero-rate day, and ``days_scanned`` records how many
    were offered, because "3 of 28" and "3 of 3" mean different things to a reader deciding
    whether to believe a finding.
    """
    cfg = config or AnomalyConfig()
    usable: list[tuple[date, float]] = []
    within: list[float] = []
    domain: list[float] = []
    for item in days:
        readings = list(item.values)
        if len(readings) >= 2:
            usable.append((item.day, mean(readings)))
            within.append(robust_sigma(readings))
        scanned = list(item.detection_values)
        if len(scanned) >= 4:
            domain.append(robust_sigma(scanned))
    trimmed = usable[-cfg.baseline_window_days :] if cfg.baseline_window_days > 0 else usable
    return RateBaseline(
        context=context,
        daily=tuple(trimmed),
        within_spread_bpm=median(within) if within else 0.0,
        domain_spread_bpm=median(domain) if domain else 0.0,
        config=cfg,
        days_scanned=len(days),
    )


@dataclass(frozen=True)
class ScatterBaseline:
    """The wearer's ordinary epoch-to-epoch scatter, pooled over their baseline windows.

    The irregular rule compares a block's scatter index against this, so "irregular for
    you" is measured against your own quiet windows rather than against a published
    HRV norm - which the coarse epoch-derived index in
    :mod:`fitbit_monitor.hr.hrv` would not mean anything next to anyway.
    """

    context: str
    values: tuple[float, ...] = ()
    days: int = 0
    config: AnomalyConfig = field(default_factory=AnomalyConfig)
    #: How many blocks each contributing day supplied, so a report can say where the
    #: ``days`` count came from.
    blocks_per_day: Mapping[str, int] = field(default_factory=dict)

    @property
    def blocks(self) -> int:
        return len(self.values)

    @property
    def median_index(self) -> float | None:
        if not self.values:
            return None
        return median(self.values)

    @property
    def sigma_index(self) -> float:
        return max(robust_sigma(self.values), self.config.min_spread_index)

    @property
    def usable(self) -> bool:
        return self.median_index is not None and self.days >= self.config.provisional_min_days

    @property
    def established(self) -> bool:
        return self.usable and self.days >= self.config.min_baseline_days

    @property
    def provisional(self) -> bool:
        return self.usable and not self.established

    @property
    def threshold(self) -> float | None:
        """``max(median + irregularity_z * sigma, irregularity_floor)``.

        The floor matters as much as the personal part: a wearer whose quiet windows are
        very steady has a MAD of a few thousandths, and without the floor their sensor
        jitter would be an episode.
        """
        centre = self.median_index
        if centre is None or not self.usable:
            return None
        return max(
            centre + self.config.irregularity_z * self.sigma_index, self.config.irregularity_floor
        )

    @property
    def reason(self) -> str | None:
        if not self.usable:
            return (
                f"only {self.days} day(s) with a usable scatter block, need "
                f"{self.config.provisional_min_days} for a personal irregularity baseline"
            )
        if not self.established:
            return (
                f"{self.days} of {self.config.min_baseline_days} days for an established irregularity "
                "baseline: findings are capped as provisional"
            )
        return None

    @property
    def confidence_factor(self) -> float:
        if self.established:
            return 1.0
        return max(0.0, min(1.0, self.days / self.config.min_baseline_days))

    def as_dict(self) -> dict[str, object]:
        centre = self.median_index
        return {
            "context": self.context,
            "usable": self.usable,
            "established": self.established,
            "provisional": self.provisional,
            "reason": self.reason,
            "days": self.days,
            "blocks": self.blocks,
            "days_required": self.config.min_baseline_days,
            "median_index": None if centre is None else round(centre, 4),
            "sigma_index": round(self.sigma_index, 4),
            "threshold": None if self.threshold is None else round(self.threshold, 4),
            "confidence_factor": round(self.confidence_factor, 3),
            "unit": "irregularity index (dimensionless)",
        }


def build_scatter_baseline(
    context: str,
    days: Sequence[tuple[date, Sequence[float]]],
    config: AnomalyConfig | None = None,
) -> ScatterBaseline:
    """Pool per-day block scatter indices into one baseline."""
    cfg = config or AnomalyConfig()
    pooled: list[float] = []
    used: dict[str, int] = {}
    for day, values in days:
        block = [
            float(value)
            for value in values
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        ]
        if not block:
            continue
        pooled.extend(block)
        used[day.isoformat()] = len(block)
    if cfg.baseline_window_days > 0:
        # Keep the most recent N days' worth of blocks, not the most recent N blocks: the
        # window is stated in days because that is how a wearer thinks about their history.
        keep = sorted(used)[-cfg.baseline_window_days :]
        used = {key: count for key, count in used.items() if key in keep}
        pooled = pooled[-sum(used.values()) :] if used else []
    return ScatterBaseline(
        context=context,
        values=tuple(pooled),
        days=len(used),
        config=cfg,
        blocks_per_day=used,
    )


__all__ = [
    "MAD_TO_SIGMA",
    "RateBaseline",
    "ScatterBaseline",
    "build_rate_baseline",
    "build_scatter_baseline",
    "median_absolute_deviation",
    "robust_sigma",
]
