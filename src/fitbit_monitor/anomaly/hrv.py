"""Nightly HRV anomalies: a personal baseline over nights, and the two rules on it.

This is a second detector, not another dataset the intraday engine points at, and the
reason is that :mod:`fitbit_monitor.anomaly` detects in *minutes*. It wants contexts,
sustained stretches measured in observed minutes, an exercise mask and a beat-to-beat
scatter index. A nightly HRV record is **one number per night** - ``hrv.rmssd``, roughly
40-90 ms for a healthy wearer, stamped at the civil day it belongs to - so every one of
those knobs would have to be answered with a lie or a zero. A night is not "sustained for
1 440 minutes", there is no exercise mask at 03:00, and RMSSD *is* a scatter, so a
scatter-of-a-scatter index would be noise squared. What is shared with the other detector
is what is genuinely shared: :func:`~fitbit_monitor.anomaly.baseline.robust_sigma`, the
confidence shape (a product of factors in ``0..1`` with a ceiling below 1.0, every factor
reported), the provisional-baseline cap, and the rule that every threshold is a declared
option generated from the dataclass that actually ran.

The statistic
-------------
HRV moves multiplicatively: 60 ms to 45 ms is the same event as 40 ms to 30 ms, and the
same absolute drop means far more to a 70 ms wearer than to a 35 ms one. So the baseline
is the median of ``log(value)`` over the wearer's recent nights and the spread is
:func:`robust_sigma` of the same logs - which makes one sigma a *percentage* of the
wearer's own median rather than a number of milliseconds. On the seeded corpus
(``fm dev seed --days 90``) one sigma came out at 9.4% of the median, so ``z_low = 3.0``
means "at least 24% below your own median" and ``deep_dip_z = 4.5`` means "at least 34%
below". Across that corpus's 80 judgeable nights the most extreme ordinary night scored
-2.8 and none reached -3, which is what the defaults are tuned against: a wearer having an
ordinary fortnight gets no alerts, and can check in ``--json`` that this is a measurement
rather than a silence.

The bar is never closer to the level than ``min_low_delta_ms``, and the spread is never
narrower than ``min_spread_ms``, for the same reason the heart-rate baseline has both: a
wearer whose recorded nights happen to be near-identical (a coarse import, five days of
history, a device that rounds) would otherwise have their rounding error called a finding.

The baseline is history, never the episode
------------------------------------------
A night that breaks the bar does not join the pool the bar is built from. The bar means
"what have my ordinary nights looked like", and a dip that votes on its own baseline would
widen and lower it: a fortnight of low HRV would redefine *low* around itself and then
report nothing. So the scan keeps a list of ordinary nights, judges each night against the
bar they produce, and holds flagged nights aside - which also means a run is judged against
the bar standing when the run *began*, at the strength of its first night rather than an
average dragged down by its own length.

The window is read with ``baseline_nights`` of lead-in for the same reason in reverse:
judging May using only May's history spends most of May with no bar at all, which is a
silent no. Same reasoning as the intraday detector's ``lead_in_days``.

A night with no value is not a normal night
-------------------------------------------
Consecutive means *consecutive calendar days*. A gap breaks a run rather than being
skipped over, because a night with no reading is a watch on the nightstand, and "two
nights 10 days apart both looked low" is a weaker claim than two nights in a row. Every
gap that cost a run is reported as a suppression, not swallowed.

Nothing here makes a causal claim, and the one cross-check it does - resting heart rate
moving the other way - is labelled corroboration and never cause, with the numbers it used
attached. See ``docs/detection.md`` and ``docs/data-policy.md``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, fields, replace
from datetime import UTC, date, datetime, timedelta
from statistics import median
from typing import Any, Mapping, Sequence

from ..plugins.models import Evidence
from ..store.downsample import daily_values
from .baseline import robust_sigma
from .errors import AnomalyConfigError

#: HRV below the wearer's own bar. The one that usually matters: parasympathetic
#: withdrawal has a long list of mundane and non-mundane causes, none of which this
#: program is allowed to name.
FINDING_HRV_LOW = "hrv_low"
#: HRV above the wearer's own bar. Off by default (``alert_on_high``): a high nightly HRV
#: is not a warning sign, and the option exists because some wearers track it as a
#: recovery signal and would rather have it labelled than have the rule quietly dropped.
FINDING_HRV_HIGH = "hrv_high"

HRV_FINDING_TYPES: tuple[str, ...] = (FINDING_HRV_LOW, FINDING_HRV_HIGH)

#: The unit of every value in this module. RMSSD in milliseconds, as the API sends it.
UNIT_MS = "milliseconds"

#: Fields tried in order when ``field = "auto"``. ``rmssd`` first because both sources
#: that exist today carry it (the v4 API as ``averageHeartRateVariabilityMilliseconds``,
#: a Takeout archive as its legacy HRV column); ``deep_sleep_rmssd`` is a narrower and
#: noisier measurement, and ``sdnn`` is here because the legacy importer wrote it and the
#: v4 data type no longer does.
HRV_FIELD_CANDIDATES: tuple[str, ...] = ("rmssd", "deep_sleep_rmssd", "sdnn")

DAY = timedelta(days=1)

#: How much an opposite move in resting heart rate adds to confidence, and why it is a
#: small additive rather than a factor: corroboration strengthens a claim, its absence
#: does not weaken one (a store with no ``daily_summary`` rows is not evidence of health).
CORROBORATION_BONUS = 0.08


@dataclass(frozen=True)
class HrvAnomalyConfig:
    """Declared thresholds for one nightly-HRV run. Frozen; change it with ``replace``."""

    #: Series to read, and which field of it. See :data:`HRV_FIELD_CANDIDATES`.
    dataset: str = "hrv"
    field: str = "auto"
    #: How many nights of the wearer's own history a bar is built from. 28: four weeks,
    #: long enough to contain several weekends and enough nights for a MAD to mean
    #: something, short enough that a change in the wearer's life is not outvoted by
    #: months of history.
    baseline_nights: int = 28
    #: Nights needed before a bar counts as **established**. 10, not the heart-rate
    #: engine's 7: that one has dozens of readings a day and needs a week of *days*,
    #: while here a night is a single sample, so ten nights is the shortest stretch in
    #: which a median is not simply one of the samples.
    min_baseline_nights: int = 10
    #: Fewer nights than this and no bar is computed at all, so no rule runs. 5: below
    #: five, one strange night in four would be the wearer's normal.
    provisional_min_nights: int = 5
    #: Confidence ceiling while the baseline is provisional, and the overall ceiling.
    #: Same shape as the heart-rate engine: a number nobody can mistake for certainty.
    provisional_confidence_cap: float = 0.5
    confidence_ceiling: float = 0.92
    #: Robust-scale z-scores. See the module docstring for what one sigma is worth.
    z_low: float = 3.0
    z_high: float = 4.0
    #: A single night this far below the bar is a finding on its own; any shallower
    #: single night needs ``min_consecutive_nights``. 4.5 is about 34% down on the seeded
    #: corpus, clear of its ordinary tail, whose most extreme night scored -2.8 across 80
    #: judgeable nights there.
    deep_dip_z: float = 4.5
    #: Two consecutive low nights make a finding. One does not: alcohol, a late meal, a
    #: hot room, a bad night of sleep and the beginning of a cold all do it, and a tool
    #: that knocks for each of them trains its user to ignore it.
    min_consecutive_nights: int = 2
    #: Absolute floors, in milliseconds: the bar never sits closer than ``min_low_delta_ms``
    #: to the level, and the spread is never narrower than ``min_spread_ms``.
    min_low_delta_ms: float = 6.0
    min_high_delta_ms: float = 10.0
    min_spread_ms: float = 2.5
    #: Read resting heart rate from ``daily_summary`` and say whether it moved the other
    #: way. Corroboration only; never a gate, and never a cause.
    corroborate: bool = True
    resting_hr_dataset: str = "daily_summary"
    resting_hr_field: str = "resting_hr"
    #: How far resting HR has to move, in its own robust sigmas, to count as moving with
    #: the finding (up while HRV is down).
    resting_hr_z: float = 2.0
    #: The high rule, off by default - see :data:`FINDING_HRV_HIGH`.
    alert_on_high: bool = False

    # --- construction -------------------------------------------------------
    def __post_init__(self) -> None:
        """Refuse nonsense here, once, rather than at three in the morning mid-scan."""
        for name in ("baseline_nights", "min_baseline_nights", "provisional_min_nights", "min_consecutive_nights"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise AnomalyConfigError(f"hrv detection option {name!r} must be a positive integer, got {value!r}")
        if self.min_baseline_nights > self.baseline_nights:
            raise AnomalyConfigError(
                f"min_baseline_nights ({self.min_baseline_nights}) cannot exceed baseline_nights "
                f"({self.baseline_nights}): a baseline would be established before it existed",
                hint=f"raise baseline_nights to at least {self.min_baseline_nights}",
            )
        if self.provisional_min_nights >= self.min_baseline_nights:
            raise AnomalyConfigError(
                f"provisional_min_nights ({self.provisional_min_nights}) must be below "
                f"min_baseline_nights ({self.min_baseline_nights}), or no baseline is ever provisional",
            )
        for name in ("z_low", "z_high", "deep_dip_z"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
                raise AnomalyConfigError(f"hrv detection option {name!r} must be a positive number, got {value!r}")
        if self.deep_dip_z < self.z_low:
            raise AnomalyConfigError(
                f"deep_dip_z ({self.deep_dip_z}) must not be below z_low ({self.z_low}): the single-night "
                "bar has to be the *stricter* one, or every single night is a finding",
                hint="raise deep_dip_z or lower z_low",
            )
        for name in (
            "min_low_delta_ms",
            "min_high_delta_ms",
            "min_spread_ms",
            "resting_hr_z",
            "provisional_confidence_cap",
            "confidence_ceiling",
        ):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
                raise AnomalyConfigError(f"hrv detection option {name!r} must be a non-negative number, got {value!r}")
        if not 0 < self.provisional_confidence_cap <= self.confidence_ceiling <= 1:
            raise AnomalyConfigError(
                f"provisional_confidence_cap ({self.provisional_confidence_cap}) must sit at or below "
                f"confidence_ceiling ({self.confidence_ceiling}), and the ceiling must be in 0..1"
            )
        if self.field != "auto" and self.field not in HRV_FIELD_CANDIDATES:
            raise AnomalyConfigError(
                f"unknown hrv field {self.field!r}",
                hint=f"use 'auto' or one of: {', '.join(HRV_FIELD_CANDIDATES)}",
            )

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "HrvAnomalyConfig":
        """Build from a ``[plugins.<name>]`` mapping. An unknown key is an error, not a shrug."""
        data = dict(raw or {})
        known = {f.name for f in fields(cls)}
        unknown = sorted(key for key in data if key not in known)
        if unknown:
            raise AnomalyConfigError(
                f"unknown hrv detection option(s): {', '.join(unknown)}",
                hint=f"valid options are: {', '.join(sorted(known))}",
            )
        kwargs: dict[str, Any] = {}
        defaults = cls()
        for name, value in data.items():
            kwargs[name] = _coerce(name, value, getattr(defaults, name))
        return cls(**kwargs)

    def replace(self, **changes: Any) -> "HrvAnomalyConfig":
        """A copy with some fields changed, re-validated."""
        return replace(self, **changes)

    def as_dict(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def option_specs(cls) -> tuple[tuple[str, str, Any, str], ...]:
        """The thresholds as ``(name, type, default, help)`` for the plugin's option list.

        Same contract as
        :meth:`fitbit_monitor.anomaly.config.AnomalyConfig.option_specs`, so the option
        surface a plugin advertises and the numbers this detector actually ran cannot drift
        apart - and a classmethod, so a plugin can build its options without inventing a
        throwaway config first.
        """
        defaults = cls()
        return tuple(
            (name, kind, getattr(defaults, name), _OPTION_HELP.get(name, ""))
            for name, kind in _OPTION_KINDS.items()
        )


#: Which config fields a plugin may set, and what type to advertise them as. Deliberately
#: explicit: an option that is not declared here is not advertised, and a test checks the two
#: sets agree. These strings go straight into a ``PluginOption``, so they are the host's
#: vocabulary (``str``, not ``string``).
_OPTION_KINDS: dict[str, str] = {
    "dataset": "str",
    "field": "str",
    "baseline_nights": "int",
    "min_baseline_nights": "int",
    "provisional_min_nights": "int",
    "min_consecutive_nights": "int",
    "z_low": "float",
    "z_high": "float",
    "deep_dip_z": "float",
    "min_low_delta_ms": "float",
    "min_high_delta_ms": "float",
    "min_spread_ms": "float",
    "corroborate": "bool",
    "resting_hr_dataset": "str",
    "resting_hr_field": "str",
    "resting_hr_z": "float",
    "alert_on_high": "bool",
    "provisional_confidence_cap": "float",
    "confidence_ceiling": "float",
}

_OPTION_HELP: dict[str, str] = {
    "dataset": "series to read (default hrv)",
    "field": "which HRV measure: auto, rmssd, deep_sleep_rmssd or sdnn",
    "baseline_nights": "nights of your own history a bar is built from",
    "min_baseline_nights": "nights before a bar counts as established rather than provisional",
    "provisional_min_nights": "fewer nights than this and no bar is built, so nothing is judged",
    "min_consecutive_nights": "low nights in a row needed for an alert on its own",
    "z_low": "how many of your own sigmas below your median night counts as low",
    "z_high": "the same, above - only used when alert_on_high is true",
    "deep_dip_z": "a single night this far down is a finding without a second night",
    "min_low_delta_ms": "the bar never sits closer than this to your median, however tight your nights are",
    "min_high_delta_ms": "the same floor on the high side",
    "min_spread_ms": "floor on your own spread, so a near-constant recording cannot make noise a finding",
    "corroborate": "look at resting heart rate and say whether it moved the other way",
    "resting_hr_dataset": "where to look for a resting heart rate when corroborating",
    "resting_hr_field": "which resting heart rate field to corroborate with",
    "resting_hr_z": "how far resting heart rate must move, in its own sigmas, to count as corroborating",
    "alert_on_high": "alert when HRV is unusually HIGH too (off: that is not a warning sign)",
    "provisional_confidence_cap": "confidence ceiling while the baseline is still provisional",
    "confidence_ceiling": "confidence ceiling, always below 1",
}


def _coerce(name: str, value: Any, template: Any) -> Any:
    """Coerce one option to the type of its default, or say why it will not.

    Typed off the default rather than the annotation because this module (like the rest of
    the package) uses ``from __future__ import annotations``, which turns annotations into
    strings that would have to be parsed back.
    """
    if isinstance(template, bool):
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in ("true", "false"):
            return value.strip().lower() == "true"
        raise AnomalyConfigError(f"hrv detection option {name!r} expects true or false, got {value!r}")
    if isinstance(template, int) and not isinstance(template, bool):
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise AnomalyConfigError(f"hrv detection option {name!r} expects a whole number, got {value!r}") from exc
    if isinstance(template, float):
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise AnomalyConfigError(f"hrv detection option {name!r} expects a number, got {value!r}") from exc
    return str(value)


# --- the readings and the bar they are judged against ---------------------------------


@dataclass(frozen=True)
class HrvNight:
    """One night's HRV, and where it sat against the bar its own history produced."""

    day: date
    value_ms: float
    #: Signed robust-scale z against that night's bar. Negative is below the median.
    z: float
    level_ms: float
    spread_ms: float
    nights_behind_bar: int
    established: bool
    low_bar_ms: float | None
    high_bar_ms: float | None

    #: A night with no reading is absent from :attr:`nights`, so it carries no z at all;
    #: a night with a reading that could not be judged carries ``z = math.nan``.
    @property
    def judgeable(self) -> bool:
        return not math.isnan(self.z)

    def as_dict(self) -> dict[str, Any]:
        return {
            "night": self.day.isoformat(),
            "value_ms": round(self.value_ms, 2),
            "z": None if not self.judgeable else round(self.z, 3),
            "level_ms": round(self.level_ms, 2),
            "spread_ms": round(self.spread_ms, 2),
            "nights_behind_bar": self.nights_behind_bar,
            "baseline": "established" if self.established else "provisional",
            "low_bar_ms": None if self.low_bar_ms is None else round(self.low_bar_ms, 2),
        }


@dataclass(frozen=True)
class HrvBaseline:
    """One wearer's nightly level: what a bar was built from, and what it refused to be."""

    field: str
    nights: int
    level_ms: float | None
    #: Robust spread in milliseconds at the median, derived from the log-space spread the
    #: rules actually work in - the number a reader can compare against their own log.
    spread_ms: float | None
    spread_percent: float | None
    established: bool
    usable: bool
    confidence_factor: float
    low_bar_ms: float | None
    high_bar_ms: float | None
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "nights": self.nights,
            "level_ms": None if self.level_ms is None else round(self.level_ms, 2),
            "spread_ms": None if self.spread_ms is None else round(self.spread_ms, 2),
            "spread_percent": None if self.spread_percent is None else round(self.spread_percent, 2),
            "established": self.established,
            "usable": self.usable,
            "confidence_factor": round(self.confidence_factor, 3),
            "low_bar_ms": None if self.low_bar_ms is None else round(self.low_bar_ms, 2),
            "high_bar_ms": None if self.high_bar_ms is None else round(self.high_bar_ms, 2),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class HrvSuppressed:
    """A candidate that did not become a finding, with the reason in the detector's words."""

    day: date
    type: str
    reason: str
    value_ms: float
    z: float
    nights: int = 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "night": self.day.isoformat(),
            "type": self.type,
            "nights": self.nights,
            "value_ms": round(self.value_ms, 2),
            "z": round(self.z, 3),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class HrvFinding:
    """One run of nights that were unusual for this wearer, with everything that said so."""

    type: str
    field: str
    nights: tuple[date, ...]
    onset: datetime
    offset: datetime
    observed_ms: float
    mean_ms: float
    threshold_ms: float
    baseline_level_ms: float
    baseline_spread_ms: float
    baseline_nights: int
    baseline_established: bool
    z_worst: float
    coverage: float
    confidence: float
    confidence_factors: Mapping[str, float] = field(default_factory=dict)
    corroboration: Mapping[str, Any] | None = None
    #: Why this run qualified: ``two_nights`` met ``min_consecutive_nights``, ``deep_dip``
    #: passed ``deep_dip_z`` on its own. Printed, because "how did this become an alert" is
    #: the question a nightly detector invites.
    because: str = ""
    values: tuple[tuple[date, float], ...] = ()

    @property
    def unit(self) -> str:
        return UNIT_MS

    @property
    def heuristic(self) -> str:
        return f"{self.field} vs its own {self.baseline_nights}-night median and MAD, log scale"

    @property
    def night_count(self) -> int:
        return len(self.nights)

    @property
    def occurred_at(self) -> datetime:
        return self.onset

    @property
    def dedup_key(self) -> str:
        """Anchored on the first night, so the same run found twice is the same alert."""
        return f"{self.type}:{self.nights[0].isoformat()}"

    @property
    def margin_ms(self) -> float:
        return self.baseline_level_ms - self.observed_ms

    @property
    def margin_sigma(self) -> float:
        return abs(self.z_worst)

    @property
    def evidence(self) -> tuple[Evidence, ...]:
        return tuple(
            Evidence(
                at=datetime(day.year, day.month, day.day, tzinfo=UTC),
                value=round(value, 2),
                dataset="hrv",
                detail=f"night of {day.isoformat()}",
            )
            for day, value in self.values
        )

    def alert_args(self) -> dict[str, Any]:
        """Everything an :class:`~fitbit_monitor.plugins.models.Alert` needs except the prose."""
        return {
            "key": self.type,
            "occurred_at": self.occurred_at,
            "confidence": round(self.confidence, 3),
            "evidence": self.evidence,
            "window_start": self.onset,
            "window_end": self.offset,
            "dedup_key": self.dedup_key,
            "labels": {
                "finding_type": self.type,
                "heuristic": self.heuristic,
                "baseline": "established" if self.baseline_established else "provisional",
                "corroborated": bool(self.corroboration and self.corroboration.get("corroborated")),
            },
            "details": {
                "nights": [day.isoformat() for day in self.nights],
                "night_count": self.night_count,
                "field": self.field,
                "observed_ms": round(self.observed_ms, 2),
                "mean_ms": round(self.mean_ms, 2),
                "threshold_ms": round(self.threshold_ms, 2),
                "unit": self.unit,
                "baseline_level_ms": round(self.baseline_level_ms, 2),
                "baseline_spread_ms": round(self.baseline_spread_ms, 3),
                "baseline_nights": self.baseline_nights,
                "baseline_established": self.baseline_established,
                "z_worst": round(self.z_worst, 3),
                "margin_ms": round(self.margin_ms, 2),
                "margin_sigma": round(self.margin_sigma, 2),
                "coverage": round(self.coverage, 4),
                "because": self.because,
                "confidence_factors": {k: round(v, 4) for k, v in self.confidence_factors.items()},
                "corroboration": dict(self.corroboration) if self.corroboration else None,
            },
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "nights": [day.isoformat() for day in self.nights],
            "observed_ms": round(self.observed_ms, 2),
            "threshold_ms": round(self.threshold_ms, 2),
            "baseline_level_ms": round(self.baseline_level_ms, 2),
            "z": round(self.z_worst, 3),
            "confidence": round(self.confidence, 3),
            "established": self.baseline_established,
            "because": self.because,
            "alerted": False,
        }


@dataclass(frozen=True)
class HrvRuleNote:
    """A rule that could not run, and why - so a quiet run can be told from a blind one."""

    rule: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {"rule": self.rule, "reason": self.reason}


# --- the bar ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Bar:
    """One wearer's nightly median and the two thresholds it implies, held in log space.

    Everything is computed on ``log(ms)`` and converted back on the way out, so the spread
    means the same fraction of the level whatever the wearer's absolute HRV is. The two
    millisecond floors are converted into log space rather than compared afterwards, so a
    tight recording moves the bar itself instead of producing a second, contradicting test.
    """

    nights: int
    level_log: float
    spread_log: float
    z_low: float
    z_high: float
    min_low_delta_ms: float
    min_high_delta_ms: float
    established: bool
    usable: bool
    reason: str = ""

    @classmethod
    def build(cls, values: Sequence[float], config: HrvAnomalyConfig) -> "_Bar":
        empty = cls(
            nights=0,
            level_log=0.0,
            spread_log=0.0,
            z_low=config.z_low,
            z_high=config.z_high,
            min_low_delta_ms=config.min_low_delta_ms,
            min_high_delta_ms=config.min_high_delta_ms,
            established=False,
            usable=False,
            reason=f"only {len(values)} night(s) of {config.field} history",
        )
        if len(values) < config.provisional_min_nights:
            return empty
        logs = [math.log(value) for value in values if value > 0]
        if len(logs) < config.provisional_min_nights:
            return empty
        level_log = median(logs)
        level_ms = math.exp(level_log)
        # A recording with no spread at all (a coarse import, a device that rounds) would
        # otherwise divide by zero, and a slightly-too-tight one would call its own noise a
        # finding: hence the floor, expressed as a fraction of this wearer's own level.
        spread_log = max(robust_sigma(logs), math.log1p(config.min_spread_ms / level_ms))
        return cls(
            nights=len(logs),
            level_log=level_log,
            spread_log=spread_log,
            z_low=config.z_low,
            z_high=config.z_high,
            min_low_delta_ms=config.min_low_delta_ms,
            min_high_delta_ms=config.min_high_delta_ms,
            established=len(logs) >= config.min_baseline_nights,
            usable=True,
        )

    @property
    def level_ms(self) -> float:
        return math.exp(self.level_log)

    @property
    def spread_ms(self) -> float:
        """The log spread expressed in milliseconds at this level - a reportable number."""
        return self.level_ms * (math.exp(self.spread_log) - 1.0)

    @property
    def spread_percent(self) -> float:
        return (math.exp(self.spread_log) - 1.0) * 100.0

    def confidence_factor(self, minimum: int) -> float:
        """Baseline maturity: 1.0 once established, linear before it."""
        if self.established:
            return 1.0
        return max(0.0, min(1.0, self.nights / max(1, minimum)))

    @property
    def low_bar_ms(self) -> float:
        floor = math.log1p(self.min_low_delta_ms / self.level_ms)
        return math.exp(self.level_log - max(self.z_low * self.spread_log, floor))

    @property
    def high_bar_ms(self) -> float:
        floor = math.log1p(self.min_high_delta_ms / self.level_ms)
        return math.exp(self.level_log + max(self.z_high * self.spread_log, floor))

    def z(self, value_ms: float) -> float:
        return (math.log(value_ms) - self.level_log) / self.spread_log


@dataclass(frozen=True)
class HrvDetectionResult:
    """One run over one wearer's nights: what was seen, judged, found and held back."""

    field: str
    field_note: str | None
    since: datetime
    until: datetime
    nights_expected: int
    nights_seen: int
    coverage: float
    baseline: HrvBaseline
    nights: tuple[HrvNight, ...]
    findings: tuple[HrvFinding, ...]
    suppressed: tuple[HrvSuppressed, ...]
    notes: tuple[HrvRuleNote, ...] = ()
    #: The resting-HR series the corroboration looked at, if there was one. Reported even
    #: when unused, because "no corroboration" and "nothing to corroborate with" differ.
    corroboration_days: int = 0

    def by_type(self, finding_type: str) -> tuple[HrvFinding, ...]:
        return tuple(finding for finding in self.findings if finding.type == finding_type)

    def as_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "field_note": self.field_note,
            "since": self.since.isoformat(),
            "until": self.until.isoformat(),
            "nights_expected": self.nights_expected,
            "nights_seen": self.nights_seen,
            "coverage": round(self.coverage, 4),
            "baseline": self.baseline.as_dict(),
            "nights": [night.as_dict() for night in self.nights],
            "findings": [finding.alert_args()["details"] for finding in self.findings],
            "suppressed": [item.as_dict() for item in self.suppressed],
            "notes": [note.as_dict() for note in self.notes],
            "corroboration_days": self.corroboration_days,
        }


# --- reading ---------------------------------------------------------------------------


def read_nights(store: Any, since: datetime, until: datetime, config: HrvAnomalyConfig) -> tuple[str, dict[date, float], str | None]:
    """The HRV series to judge, the values of it, and in prose why that one.

    ``field = "auto"`` exists because the two sources this store can have carry different
    columns (v4 sends ``rmssd`` and ``deep_sleep_rmssd``; a legacy Takeout archive carried
    ``rmssd`` and ``sdnn``), and a detector that reports nothing because it asked for a
    column this wearer's device never wrote is worse than one that picks the column that
    exists and says which it picked.
    """
    candidates = list(HRV_FIELD_CANDIDATES) if config.field == "auto" else [config.field]
    tried: list[str] = []
    for name in candidates:
        values = daily_values(store.query(config.dataset, since, until, fields=[name]), name)
        if values:
            note = (
                f"{', '.join(tried)} had no values in this range; judging {name}, which does"
                if tried
                else None
            )
            return name, values, note
        tried.append(name)
    note = (
        f"no {config.dataset} rows carrying {' or '.join(tried)} in this range"
        if config.field == "auto"
        else f"{config.dataset}.{config.field} has no values in this range"
    )
    return candidates[0], {}, note


def _aware(moment: datetime) -> datetime:
    """UTC-aware copy of ``moment``; a naive stamp is read as UTC rather than local time.

    Same contract the rest of the package uses (see ``anomaly.accuracy._as_aware``): the
    store rejects naive stamps outright, so a naive window here would otherwise die in a
    SQL comparison minutes later.
    """
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def expected_days(since: datetime, until: datetime) -> tuple[date, ...]:
    """The calendar days the window actually covers.

    Half-open, like every other window here: a run that ends at midnight on the 1st does
    not owe a night for the 1st. A run that ends mid-day does, because that night has
    started and may already be stored.
    """
    since, until = _aware(since), _aware(until)
    first = since.date()
    last = until.date()
    if until.time() == datetime.min.time():
        last = last - DAY
    if last < first:
        return ()
    return tuple(first + timedelta(days=i) for i in range((last - first).days + 1))


# --- the rules -------------------------------------------------------------------------


def detect_hrv(
    store: Any, since: datetime, until: datetime, *, config: HrvAnomalyConfig | None = None
) -> HrvDetectionResult:
    """Judge every night in ``[since, until)`` against the wearer's own prior nights.

    One chronological scan, and two details in it are the whole design:

    **The window is read with ``baseline_nights`` of lead-in.** Judging May with only May's
    history would spend most of the month with no bar at all, which is a silent no, so the
    series is fetched from before ``since`` and only the nights inside the window are
    judged. Same reason as the intraday detector's ``lead_in_days``.

    **A night that breaks the bar does not join the pool the bar is built from.** The bar is
    "what have my ordinary nights looked like", and a dip that voted on its own baseline
    would widen and lower the bar it is being judged against - a fortnight of low HRV would
    redefine "low" and then report nothing. So flagged nights are held aside, and the bar a
    run is re-judged against is the one standing when the run began.
    """
    cfg = config or HrvAnomalyConfig()
    since, until = _aware(since), _aware(until)
    field, values, field_note = read_nights(store, since - DAY * cfg.baseline_nights, until, cfg)
    days = expected_days(since, until)
    # A non-positive value is not a low night, it is a broken row: importing 0 ms of RMSSD
    # would otherwise become the strongest finding in the run.
    usable_values = {day: value for day, value in values.items() if value > 0}
    malformed = sorted(set(values) - set(usable_values))
    seen = [day for day in days if day in usable_values]
    coverage = (len(seen) / len(days)) if days else 0.0
    notes: list[HrvRuleNote] = []
    if field_note:
        notes.append(HrvRuleNote(rule="field", reason=field_note))
    if malformed:
        notes.append(
            HrvRuleNote(
                rule="field",
                reason=(
                    f"{len(malformed)} night(s) carried a non-positive {field} and were not judged "
                    f"(first: {malformed[0].isoformat()})"
                ),
            )
        )

    lead_in = expected_days(since - DAY * cfg.baseline_nights, since)
    window = set(days)
    ordinary: list[float] = []
    #: The bar standing at the moment each candidate night broke it, reused to judge its run.
    bars: dict[date, _Bar] = {}
    nights: list[HrvNight] = []
    low_candidates: list[date] = []
    high_candidates: list[date] = []
    unusable_nights = 0

    for day in list(lead_in) + list(days):
        bar = _Bar.build(ordinary[-cfg.baseline_nights :], cfg)
        value = usable_values.get(day)
        if value is None:
            continue
        if not bar.usable:
            # Counted only for nights inside the window: a lead-in night with no bar behind
            # it is a fact about where the store starts, not something the run declined to judge.
            unusable_nights += 1 if day in window else 0
            # An unjudgeable night still counts as ordinary history: it happened.
            ordinary.append(value)
            if day in window:
                nights.append(
                    HrvNight(day, value, math.nan, value, 0.0, bar.nights, False, None, None)
                )
            continue
        z = bar.z(value)
        breaks_low = value <= bar.low_bar_ms
        breaks_high = cfg.alert_on_high and value >= bar.high_bar_ms
        if day in window:
            nights.append(
                HrvNight(
                    day=day,
                    value_ms=value,
                    z=z,
                    level_ms=bar.level_ms,
                    spread_ms=bar.spread_ms,
                    nights_behind_bar=bar.nights,
                    established=bar.established,
                    low_bar_ms=bar.low_bar_ms,
                    high_bar_ms=bar.high_bar_ms,
                )
            )
            if breaks_low:
                low_candidates.append(day)
            if breaks_high:
                high_candidates.append(day)
        if breaks_low or breaks_high:
            bars[day] = bar
        else:
            ordinary.append(value)

    if not low_candidates and not high_candidates and unusable_nights:
        notes.append(
            HrvRuleNote(
                rule="hrv_low",
                reason=(
                    f"no bar for {unusable_nights} night(s): fewer than provisional_min_nights = "
                    f"{cfg.provisional_min_nights} ordinary nights behind them"
                ),
            )
        )

    runs = _runs(low_candidates) + _runs(high_candidates)
    if not usable_values:
        notes.append(HrvRuleNote(rule="hrv_low", reason=f"nothing to judge: {field_note or 'no values'}"))

    findings: list[HrvFinding] = []
    suppressed: list[HrvSuppressed] = []
    for run in runs:
        finding, held = _judge(store, cfg, field, usable_values, run, bars[run[0]], coverage)
        if finding is not None:
            findings.append(finding)
        suppressed.extend(held)

    baseline = _reporting_baseline(cfg, field, usable_values, days)
    corroboration_days = 0
    if cfg.corroborate:
        corroboration_days = sum(1 for finding in findings if (finding.corroboration or {}).get("available"))
    return HrvDetectionResult(
        field=field,
        field_note=field_note,
        since=since,
        until=until,
        nights_expected=len(days),
        nights_seen=len(seen),
        coverage=coverage,
        baseline=baseline,
        nights=tuple(nights),
        findings=tuple(sorted(findings, key=lambda item: item.onset)),
        suppressed=tuple(sorted(suppressed, key=lambda item: (item.day, item.type))),
        notes=tuple(notes),
        corroboration_days=corroboration_days,
    )


def _runs(days: Sequence[date]) -> list[list[date]]:
    """Group candidate nights into runs of **consecutive calendar days**.

    A gap ends a run rather than being stepped over: a night with no reading is a watch on
    the nightstand, and "two nights a week apart were both low" is a weaker sentence than
    two nights in a row. A run broken by a gap is reported as a suppression, not merged.
    """
    runs: list[list[date]] = []
    for day in sorted(days):
        if runs and day - runs[-1][-1] == DAY:
            runs[-1].append(day)
        else:
            runs.append([day])
    return runs


def _breaks(bar: _Bar, value: float, *, low: bool) -> bool:
    return value <= bar.low_bar_ms if low else value >= bar.high_bar_ms


def _judge(
    store: Any,
    cfg: HrvAnomalyConfig,
    field: str,
    values: Mapping[date, float],
    run: Sequence[date],
    bar: _Bar,
    coverage: float,
) -> tuple[HrvFinding | None, list[HrvSuppressed]]:
    """Turn one candidate run into a finding, or into a suppression that says why not.

    Every night in the run is judged against the bar standing when the run began - the one
    the scan already built, so nothing here re-derives it and nothing in the run can have
    influenced it. A run that fails here is reported with the bar it missed rather than
    dropped: "one low night" is the most common thing a nightly detector sees, and the
    reason it stayed quiet is the interesting part.
    """
    first = run[0]
    low = values[first] <= bar.low_bar_ms
    kept = [day for day in run if _breaks(bar, values[day], low=low)]
    if not kept:
        # Only a bar that included the dip itself could have called these nights low: the
        # wearer's own history says they are ordinary. Say so rather than drop it quietly.
        return None, [
            HrvSuppressed(
                day=first,
                type=FINDING_HRV_LOW if low else FINDING_HRV_HIGH,
                reason=(
                    f"judged against the {bar.nights} nights before it ({bar.level_ms:.0f} ms, "
                    f"bar {bar.low_bar_ms:.0f} ms), these nights are inside your own ordinary spread"
                ),
                value_ms=min(values[day] for day in run) if low else max(values[day] for day in run),
                z=bar.z(min(values[day] for day in run) if low else max(values[day] for day in run)),
                nights=len(run),
            )
        ]

    # Nights normally all survive here - a run was assembled from one shared bar - but a
    # low night can sit next to a high one when ``alert_on_high`` is on, and the direction
    # of the run is decided by the night that opened it. The claim is also about *consecutive*
    # nights, so the longest surviving stretch is what is reported, not an average of a
    # scatter of nights that happened to be nearby.
    longest = max(_runs(kept), key=lambda item: (len(item), item[0]))
    in_run = [values[day] for day in longest]
    observed = min(in_run) if low else max(in_run)
    worst_z = bar.z(observed)
    finding_type = FINDING_HRV_LOW if low else FINDING_HRV_HIGH
    away = "down" if low else "up"
    if len(longest) >= cfg.min_consecutive_nights:
        because = (
            f"{len(longest)} nights in a row, at or above min_consecutive_nights = "
            f"{cfg.min_consecutive_nights}"
        )
    elif abs(worst_z) >= cfg.deep_dip_z:
        because = f"one night {abs(worst_z):.1f}\u03c3 {away}, past deep_dip_z = {cfg.deep_dip_z:g}"
    else:
        return None, [
            HrvSuppressed(
                day=longest[0],
                type=finding_type,
                reason=(
                    f"{len(longest)} night(s) at {abs(worst_z):.1f}\u03c3 {away} from your own median: a "
                    f"single night needs {cfg.deep_dip_z:g}\u03c3 (deep_dip_z), or "
                    f"{cfg.min_consecutive_nights} in a row (min_consecutive_nights). One low night is a "
                    "common and usually ordinary thing"
                ),
                value_ms=observed,
                z=worst_z,
                nights=len(longest),
            )
        ]

    corroboration = _corroboration(store, cfg, tuple(longest), low=low)
    onset = datetime(longest[0].year, longest[0].month, longest[0].day, tzinfo=UTC)
    offset = datetime(longest[-1].year, longest[-1].month, longest[-1].day, tzinfo=UTC) + DAY
    level = bar.level_ms
    threshold = bar.low_bar_ms if low else bar.high_bar_ms
    required = abs(level - threshold)
    margin_factor = _clamp((abs(level - observed) / (required * 2.0)) if required > 0 else 1.0)
    baseline_factor = bar.confidence_factor(cfg.min_baseline_nights)
    coverage_factor = _clamp(coverage)
    confidence = cfg.confidence_ceiling * margin_factor * baseline_factor * coverage_factor
    if corroboration.get("corroborated"):
        confidence += CORROBORATION_BONUS
    if not bar.established:
        confidence = min(confidence, cfg.provisional_confidence_cap)
    confidence = _clamp(confidence, cfg.confidence_ceiling)
    factors = {
        "margin": round(margin_factor, 4),
        "baseline": round(baseline_factor, 4),
        "coverage": round(coverage_factor, 4),
        "ceiling": cfg.confidence_ceiling,
        "corroboration": CORROBORATION_BONUS if corroboration.get("corroborated") else 0.0,
    }
    return (
        HrvFinding(
            type=finding_type,
            field=field,
            nights=tuple(longest),
            onset=onset,
            offset=offset,
            observed_ms=observed,
            mean_ms=sum(in_run) / len(in_run),
            threshold_ms=threshold,
            baseline_level_ms=level,
            baseline_spread_ms=bar.spread_ms,
            baseline_nights=bar.nights,
            baseline_established=bar.established,
            z_worst=worst_z,
            coverage=coverage,
            confidence=confidence,
            confidence_factors=factors,
            corroboration=corroboration,
            because=because,
            values=tuple((day, values[day]) for day in longest),
        ),
        [],
    )


def _clamp(value: float, ceiling: float = 1.0) -> float:
    return max(0.0, min(ceiling, value))


def _corroboration(store: Any, cfg: HrvAnomalyConfig, nights: tuple[date, ...], *, low: bool) -> dict[str, Any]:
    """Did resting heart rate move the other way over these nights?

    Corroboration, never cause and never a gate: an HRV dip with a rising resting rate is a
    stronger *description* of the same two weeks, not a different diagnosis, and a store
    with no `daily_summary` rows is not evidence of health. Resting HR is judged on its own
    prior nights and its own robust spread, with a one-bpm floor, because a wearer whose
    stored resting HR never moves by more than a bpm would otherwise have every bpm called
    a swing.
    """
    if not cfg.corroborate:
        return {"available": False, "corroborated": False, "note": "corroborate = false: resting HR not consulted"}
    since = datetime(nights[0].year, nights[0].month, nights[0].day, tzinfo=UTC) - DAY * cfg.baseline_nights
    until = datetime(nights[-1].year, nights[-1].month, nights[-1].day, tzinfo=UTC) + DAY
    try:
        series = daily_values(
            store.query(cfg.resting_hr_dataset, since, until, fields=[cfg.resting_hr_field]),
            cfg.resting_hr_field,
        )
    except Exception:  # a store with no such dataset is a fact about the data, not a crash
        series = {}
    prior = [
        value
        for day, value in sorted(series.items())
        if nights[0] - DAY * cfg.baseline_nights <= day < nights[0]
    ]
    observed = [series[day] for day in nights if day in series]
    if not series or len(prior) < cfg.provisional_min_nights or not observed:
        return {
            "available": bool(series),
            "corroborated": False,
            "nights_with_resting_hr": len(observed),
            "note": (
                f"only {len(observed)} night(s) of {cfg.resting_hr_dataset}.{cfg.resting_hr_field} in "
                f"range, with {len(prior)} prior nights of history: nothing to corroborate with, "
                "which is not a contradiction"
            ),
        }
    centre = median(prior)
    sigma = max(robust_sigma(prior), 1.0)
    scores = [(value - centre) / sigma for value in observed]
    worst = max(scores) if low else min(scores)
    moved = worst >= cfg.resting_hr_z if low else worst <= -cfg.resting_hr_z
    return {
        "available": True,
        "corroborated": bool(moved),
        "field": f"{cfg.resting_hr_dataset}.{cfg.resting_hr_field}",
        "nights_with_resting_hr": len(observed),
        "usual_bpm": round(centre, 1),
        "observed_bpm": round(max(observed) if low else min(observed), 1),
        "z": round(worst, 2),
        "bar_z": cfg.resting_hr_z,
        "note": (
            f"resting HR moved from {centre:.0f} to {max(observed) if low else min(observed):.0f} bpm over "
            f"these nights ({abs(worst):.1f}\u03c3): recorded as corroboration, not as a cause"
            if moved
            else (
                f"resting HR stayed within {abs(worst):.1f}\u03c3 of its own usual {centre:.0f} bpm over "
                "these nights: uncorroborated, which does not make the HRV reading wrong"
            )
        ),
    }


def _reporting_baseline(cfg: HrvAnomalyConfig, field: str, values: Mapping[date, float], days: tuple[date, ...]) -> HrvBaseline:
    """One baseline for the run's report: the freshest history, whether or not a rule used it.

    The rules judge each run against the history before that run, which is right for a
    finding and useless for a summary line - a 30-night scan would print thirty bars. This is
    the one a report quotes: every night before the last expected day, capped at
    ``baseline_nights``.
    """
    if not days:
        bar = _Bar.build([], cfg)
        return _baseline_of(cfg, field, bar, "the window is empty")
    last_expected = days[-1] + DAY
    history = [
        value
        for day, value in sorted(values.items())
        if day < last_expected and day >= last_expected - DAY * cfg.baseline_nights
    ]
    return _baseline_of(cfg, field, _Bar.build(history, cfg), "")


def _baseline_of(cfg: HrvAnomalyConfig, field: str, bar: _Bar, reason: str) -> HrvBaseline:
    if not bar.usable:
        return HrvBaseline(
            field=field,
            nights=bar.nights,
            level_ms=None,
            spread_ms=None,
            spread_percent=None,
            established=False,
            usable=False,
            confidence_factor=0.0,
            low_bar_ms=None,
            high_bar_ms=None,
            reason=reason or bar.reason,
        )
    return HrvBaseline(
        field=field,
        nights=bar.nights,
        level_ms=bar.level_ms,
        spread_ms=bar.spread_ms,
        spread_percent=bar.spread_percent,
        established=bar.established,
        usable=True,
        confidence_factor=bar.confidence_factor(cfg.min_baseline_nights),
        low_bar_ms=bar.low_bar_ms,
        high_bar_ms=bar.high_bar_ms,
        reason=(
            "" if bar.established else f"provisional: {bar.nights} of {cfg.min_baseline_nights} nights"
        ),
    )


__all__ = [
    "CORROBORATION_BONUS",
    "FINDING_HRV_HIGH",
    "FINDING_HRV_LOW",
    "HRV_FIELD_CANDIDATES",
    "HRV_FINDING_TYPES",
    "UNIT_MS",
    "HrvAnomalyConfig",
    "HrvBaseline",
    "HrvDetectionResult",
    "HrvFinding",
    "HrvNight",
    "HrvRuleNote",
    "HrvSuppressed",
    "detect_hrv",
    "read_nights",
]
