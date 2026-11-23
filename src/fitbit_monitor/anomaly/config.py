"""Every threshold the anomaly engine can read, in one declarative model.

The ticket this package exists for says the thresholds are config, not code, and this
module is where that promise is kept: no rule in :mod:`fitbit_monitor.anomaly.rules`
compares a rate against a literal. Each number has a default here, a reason in its
comment, and a place to be argued with.

Three things are true of every field:

* **It has a documented default.** ``AnomalyConfig.defaults()`` is the shipped answer,
  :meth:`AnomalyConfig.as_dict` prints it, and :meth:`AnomalyConfig.option_specs`
  renders the same defaults as plugin option declarations so a plugin (the hr-anomaly
  example) advertises exactly these keys with these values instead of re-inventing them.
* **It is validated once, at construction.** A negative duration, a z-score below 1,
  a ``min_baseline_days`` of zero or a confidence cap above 1 is rejected here with the
  field named, rather than producing a rule that fires on everything at 03:00.
* **It is data-driven where the data exists.** The defaults were chosen against the
  deterministic corpus ``fm dev seed`` writes (90 days, 5-minute heart rate, one
  resting-rate distribution and its spread measured over 85 usable resting days, see
  ``docs/detection.md``), not copied out of a textbook: the level a rule compares
  against is *this wearer's* median resting rate, and the scale is *this wearer's*
  own dispersion. What is not personal is only what has to be: the two ``min_delta``
  floors, which stop a degenerate (near-zero) spread from turning sensor jitter into a
  finding, and the irregularity floor, which does the same job for the scatter index.

Personal, not textbook
----------------------
A fixed "over 100 bpm at rest is abnormal" constant fails twice: it never fires for a
wearer whose resting rate is 55 and whose abnormal is 78, and it fires every evening
for a wearer whose ordinary resting rate is 95. So the engine's default thresholds are

``high = level + max(z_high * sigma, min_high_delta_bpm)``
``low  = level - max(z_low  * sigma, min_low_delta_bpm)``

where ``level`` is the median of the wearer's own per-day resting-window means, and
``sigma`` is the combination of their day-to-day spread and their within-window spread
(see :mod:`fitbit_monitor.anomaly.baseline`). ``z_*`` are z-scores on a robust scale, so
they mean the same thing for a 55 bpm wearer and a 95 bpm one.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from datetime import timedelta
from typing import Any, Mapping, Sequence

from ..errors import FmError
from ..timeutil import resolve_timezone
from .errors import AnomalyConfigError


#: The contexts the engine can build a baseline for and detect in. ``rest`` is the
#: awake resting window (:data:`~fitbit_monitor.hr.util.REST_HEURISTIC`), ``sleep`` is
#: the overnight window (:data:`~fitbit_monitor.hr.util.SLEEP_HEURISTIC`).
CONTEXTS: tuple[str, ...] = ("rest", "sleep")

#: Which contexts subtract the exercise mask. An asleep wearer is not exercising, and
#: a rate that would read as a workout at noon is exactly the finding worth making at
#: 03:00, so the overnight context keeps its elevated stretches.
EXERCISE_EXCLUDED_CONTEXTS: tuple[str, ...] = ("rest",)


@dataclass(frozen=True)
class AnomalyConfig:
    """Declared thresholds for one detection run. Frozen; change it with ``replace``."""

    # --- personal baseline --------------------------------------------------
    #: Usable resting days needed before a baseline counts as **established**.
    #:
    #: Seven: one week, which is the shortest stretch that contains a weekend and a
    #: working week and so cannot be an artefact of one kind of day. Below this the
    #: baseline is *provisional*: usable, but every finding it produced is capped at
    #: :attr:`provisional_confidence_cap`, because "your rate is unusual for you" made
    #: after three days is a much weaker sentence than after three weeks.
    min_baseline_days: int = 7
    #: Fewer usable resting days than this and no baseline is computed at all, so no
    #: personal rule runs. Three: below three days a median is one of the samples, and a
    #: single odd night would become the wearer's normal.
    provisional_min_days: int = 3
    #: How many local days the baseline is built from: the most recent N usable days in
    #: the window, so a long back-scan does not let a resting rate from six months ago
    #: outvote last week's.
    baseline_window_days: int = 28
    #: Contexts to build a baseline for and run the rules in. See :data:`CONTEXTS`.
    contexts: tuple[str, ...] = ("rest", "sleep")
    #: Contexts that subtract the exercise mask from their detection domain. Defaults to
    #: ``rest`` only: an asleep wearer is not exercising, so subtracting exercise overnight
    #: would delete the sustained high rates the high rule exists to find (see
    #: :data:`EXERCISE_EXCLUDED_CONTEXTS`). Clearing this - ``contexts = ["rest"],
    #: exclude_exercise_in = []`` - makes the awake rule treat *every* elevated stretch as a
    #: candidate, which is the wide-net alternative and gets measured by this epic's labelled
    #: accuracy gate rather than argued about here.
    exclude_exercise_in: tuple[str, ...] = ("rest",)
    #: Robust-scale z-scores for "abnormally high / low for this wearer".
    #:
    #: On the MAD-derived scale in :mod:`fitbit_monitor.anomaly.baseline`, so they mean the
    #: same thing for a 55 bpm wearer and a 95 bpm one. Both were calibrated against the
    #: deterministic corpus (``fm dev seed``: 90 days at a 5-minute grain with labelled
    #: episodes; the sweep and the numbers are in ``docs/detection.md``) rather than picked for
    #: how round they look. The high side sits higher than a textbook three-sigma rule because
    #: the awake detection domain is wide - ordinary life sits 10-35 bpm above a resting median
    #: - and at ``z_high = 3.5`` the corpus produced a false finding every twelve days or so
    #: while 4.0 produced almost none, at no cost in recall on the labelled episodes. The two
    #: are asymmetric on purpose: day-to-day living (caffeine, a bad night, a warm room) skews
    #: a resting distribution *upward*, so the high side needs more convincing than the low
    #: side, where almost nothing innocent pushes a rate far below the wearer's own quiet level.
    z_high: float = 4.0
    z_low: float = 3.0
    #: Never move the threshold closer than this to the level, however tight the wearer's
    #: own spread is. Wrist heart rate at rest has a couple of bpm of jitter, and a wearer
    #: whose measured spread came out near zero (a still sleeper, a coarse import, three days
    #: of history) would otherwise have their jitter called abnormal. On the awake context
    #: the z term usually dominates and these floors are inert; on the overnight context,
    #: where a quiet wearer's measured spread really is tiny, they are what stops the rule
    #: from firing on noise. This is the one place a non-personal number is allowed to bind,
    #: and it is here for exactly that reason.
    min_high_delta_bpm: float = 10.0
    min_low_delta_bpm: float = 8.0
    #: Floor on the scale itself, in bpm, so a degenerate MAD of zero cannot divide.
    min_spread_bpm: float = 2.0

    # --- sustained-rate rules -----------------------------------------------
    #: How long a stretch must stay past its threshold to be a finding at all.
    #:
    #: Twenty minutes: a single spike, a doorbell, a flight of stairs and a three-minute
    #: artefact are all excluded by this one number, which is the acceptance criterion
    #: "single spikes do not fire" turned into a knob. It is measured in *observed*
    #: minutes inside the episode, so a dropout cannot inflate a five-minute stretch into
    #: an hour-long one.
    min_sustained_minutes: float = 20.0
    #: Width of the moving mean the threshold is compared against (the *reported* values
    #: are still the raw readings). Ten minutes at the corpus grain smooths wrist jitter
    #: without smoothing away a real step change.
    smoothing_minutes: float = 10.0
    #: Two stretches of this close together are one episode. Twenty minutes covers the
    #: gap a twenty-minute episode would otherwise be split by, and keeps one overnight
    #: episode from becoming two findings.
    merge_gap_minutes: float = 20.0

    # --- irregular-rhythm rule ----------------------------------------------
    #: Length of the sliding window each scatter value is computed over, as a *minimum*.
    #:
    #: Thirty minutes is the nominal width; the engine widens it automatically when the
    #: stored grain is coarse (a 5-minute corpus needs 12 intervals, which is already an hour)
    #: because a window thinner than ``irregularity_min_intervals`` intervals cannot hold a
    #: measurement, so this is a floor on the width and the width actually used is reported
    #: with every finding.
    irregularity_block_minutes: float = 30.0
    #: Intervals one scatter window spans before the value means anything. Higher than the
    #: feature library's floor of 8 on purpose: this value is compared against a distribution
    #: of other values of itself, and a thin window is noise with a decimal point.
    irregularity_min_intervals: int = 12
    #: Consecutive over-threshold scatter positions an episode needs. The windows slide, so
    #: two positions overlap and share most of their data: this is a guard against a single
    #: window's noise becoming a finding, not a duration test - the duration gate does that.
    irregularity_min_blocks: int = 2
    #: ...and how long the resulting stretch has to last, in observed minutes.
    irregularity_min_minutes: float = 20.0
    #: Robust-scale z-score of a scatter value against the wearer's own ordinary scatter.
    #:
    #: 4.0, calibrated the same way as :attr:`z_high`: at 3.5 the corpus produced a couple of
    #: findings from ordinary daytime wander of the epoch-to-epoch interval, and lifting the bar
    #: cost nothing on the labelled irregular episodes.
    irregularity_z: float = 4.0
    #: Absolute floor on the index: ``mean |Δinterval| / median interval`` of 0.20 means
    #: the epoch-to-epoch interval moves by 20% of itself on average. Measured over the
    #: corpus's quiet windows the ordinary value is around 0.03-0.06, so the floor sits
    #: clear of it and a wearer whose MAD is near zero still cannot fire on jitter.
    #:
    #: This one was set by the labelled accuracy gate rather than by taste
    #: (``tests/fixtures/anomaly/hr_anomaly_labels.json``, scored by
    #: ``tests/test_anomaly_accuracy_gate.py``, written up in ``docs/detection.md``): at 0.15
    #: the labelled wearers produced scatter findings that matched no labelled episode - all
    #: of them the onset of an inferred workout, all of them under 0.55 confidence; at 0.18
    #: every one of them was gone and every labelled irregular episode was still found; at
    #: 0.25 the first labelled episode started being missed. 0.20 sits inside that window with
    #: room on both sides.
    irregularity_floor: float = 0.20
    #: Floor on the scatter index's own scale, so a MAD of zero cannot divide.
    min_spread_index: float = 0.01

    # --- confidence ---------------------------------------------------------
    #: The engine never reports certainty. 0.95, not 1.0: a heuristic over a wrist-worn
    #: optical sensor is a prompt to look at the data, not a conclusion, and a confidence
    #: of exactly 1.0 invites a reader to treat it as one.
    confidence_ceiling: float = 0.95
    #: Cap for every finding produced from a provisional (not yet established) baseline.
    provisional_confidence_cap: float = 0.55
    #: Readings an episode must be built from to be reported at all. Three: one is a point,
    #: two is a line, three has a middle - below that a "sustained stretch" is a rumour, and
    #: a coarse import that can only offer two readings in twenty minutes is told so.
    min_finding_samples: int = 3
    #: Fraction of an episode's claimed duration that must actually be observed. Below
    #: this the finding is suppressed as thin evidence rather than reported at a
    #: confidence that hides the fact that most of it was invented.
    min_coverage: float = 0.5
    #: Cap on the evidence points attached to a finding. The full series is still in
    #: :attr:`~fitbit_monitor.anomaly.models.Finding.values`; this is the sampled series a
    #: sink can put in one alert row.
    max_evidence_points: int = 12
    #: How far above the wearer's own sleeping level a low stretch may sit and still be
    #: attributed to the night rather than to the day.
    #:
    #: The reason this exists is a boundary, not a threshold: both contexts' domains end where
    #: a *detected window* ends, and a window is a function of the data. A long stretch of
    #: unusually low rate overnight is also the quietest stretch of the night, so it becomes
    #: the seed of the sleep window and the rest of that night falls outside every window -
    #: into the resting domain, where 55 bpm against a resting level of 75 reads as a
    #: seven-hour bradycardia that never happened. A low episode that (a) overlaps the region
    #: one of the wearer's nights was searched in and (b) sits at or below their sleeping level
    #: plus this margin is therefore reported by the sleep context or not at all, and the
    #: suppression says so. Four bpm: less than the difference between "asleep" and "lying on
    #: a sofa reading email" for most wearers, so a genuine daytime bradycardia - which is a
    #: daytime phenomenon at a daytime rate - still clears it.
    nocturnal_low_margin_bpm: float = 4.0

    # --- window selection ---------------------------------------------------
    #: How far past the requested window the engine reads to put its first and last day in
    #: context. Eighteen hours: a day whose previous night was never seen cannot have its
    #: resting window evaluated (the morning sleep is still in it), and a night whose morning
    #: was never seen is a truncated night. Reading a day and a half of context on each side
    #: costs one extra query and stops the boundary days of every scan from producing findings
    #: that only exist because the window started at midnight.
    context_warmup_hours: float = 18.0
    #: IANA name the day boundaries and episode anchors are computed in. ``None`` means
    #: UTC, which is what a store with no configured zone uses; a plugin passes the user's
    #: zone here (or builds its own ``FeatureOptions`` and passes that).
    time_zone: str | None = None
    #: Dataset and field read from the store.
    dataset: str = "heart_rate"
    field: str = "hr"

    # ------------------------------------------------------------------
    def __post_init__(self) -> None:
        object.__setattr__(self, "contexts", _as_contexts(self.contexts))
        object.__setattr__(
            self,
            "exclude_exercise_in",
            tuple(str(item).strip().lower() for item in _as_sequence(self.exclude_exercise_in)),
        )
        unknown = [item for item in self.exclude_exercise_in if item not in CONTEXTS]
        if unknown:
            raise AnomalyConfigError(
                f"exclude_exercise_in names unknown context(s): {', '.join(unknown)}",
                hint=f"valid contexts are {', '.join(CONTEXTS)}",
            )
        object.__setattr__(self, "time_zone", _as_tz_name(self.time_zone))
        for name, minimum in (
            ("min_baseline_days", 1),
            ("provisional_min_days", 1),
            ("baseline_window_days", 1),
            ("irregularity_min_intervals", 2),
            ("irregularity_min_blocks", 1),
            ("min_finding_samples", 2),
            ("max_evidence_points", 2),
        ):
            value = int(getattr(self, name))
            if value < minimum:
                raise AnomalyConfigError(
                    f"{name} must be at least {minimum}, got {value}",
                    hint="these are sample counts; zero would mean 'fire on nothing'",
                )
            object.__setattr__(self, name, value)
        if self.provisional_min_days > self.min_baseline_days:
            raise AnomalyConfigError(
                f"provisional_min_days ({self.provisional_min_days}) cannot exceed "
                f"min_baseline_days ({self.min_baseline_days}): a baseline would be "
                "provisional and established at the same time",
            )
        if self.baseline_window_days < self.min_baseline_days:
            raise AnomalyConfigError(
                f"baseline_window_days ({self.baseline_window_days}) is shorter than "
                f"min_baseline_days ({self.min_baseline_days}): an established baseline "
                "could never be built in the window",
            )
        for name in ("z_high", "z_low"):
            number = float(getattr(self, name))
            if number < 1.0:
                raise AnomalyConfigError(
                    f"{name} must be at least 1.0, got {number}",
                    hint="a z-score below 1 is inside every wearer's ordinary spread",
                )
            object.__setattr__(self, name, number)
        if float(self.irregularity_z) < 1.0:
            raise AnomalyConfigError(
                f"irregularity_z must be at least 1.0, got {self.irregularity_z}"
            )
        for name in (
            "min_high_delta_bpm",
            "min_low_delta_bpm",
            "min_spread_bpm",
            "min_sustained_minutes",
            "smoothing_minutes",
            "merge_gap_minutes",
            "irregularity_block_minutes",
            "irregularity_min_minutes",
            "context_warmup_hours",
            "nocturnal_low_margin_bpm",
        ):
            number = float(getattr(self, name))
            if number < 0:
                raise AnomalyConfigError(f"{name} must not be negative, got {number}")
            object.__setattr__(self, name, number)
        if self.min_spread_bpm <= 0:
            raise AnomalyConfigError(f"min_spread_bpm must be positive, got {self.min_spread_bpm}")
        if self.min_spread_index <= 0:
            raise AnomalyConfigError(
                f"min_spread_index must be positive, got {self.min_spread_index}"
            )
        if self.irregularity_floor < 0:
            raise AnomalyConfigError(
                f"irregularity_floor must not be negative, got {self.irregularity_floor}"
            )
        for name in ("confidence_ceiling", "provisional_confidence_cap", "min_coverage"):
            number = float(getattr(self, name))
            if not 0.0 < number <= 1.0:
                raise AnomalyConfigError(f"{name} must be in (0, 1], got {number}")
            object.__setattr__(self, name, number)
        if self.confidence_ceiling >= 1.0:
            raise AnomalyConfigError(
                f"confidence_ceiling must be below 1.0, got {self.confidence_ceiling}",
                hint="the engine must not be able to report certainty",
            )
        if self.provisional_confidence_cap > self.confidence_ceiling:
            raise AnomalyConfigError(
                f"provisional_confidence_cap ({self.provisional_confidence_cap}) cannot exceed "
                f"confidence_ceiling ({self.confidence_ceiling})",
            )

    # --- construction from config files -------------------------------------
    @classmethod
    def defaults(cls) -> "AnomalyConfig":
        """The shipped configuration, as an instance."""
        return cls()

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "AnomalyConfig":
        """Build from a mapping of ``{field: value}`` - a ``[plugins.<name>]`` section.

        Unknown keys are an error, not a shrug: a typo in ``min_baselines_days`` would
        otherwise silently leave the default in place, and the whole point of declared
        thresholds is that the number in the file is the number that ran.
        """
        data = dict(raw or {})
        known = {f.name for f in fields(cls)}
        unknown = sorted(k for k in data if k not in known)
        if unknown:
            raise AnomalyConfigError(
                f"unknown detection option(s): {', '.join(unknown)}",
                hint=f"valid options are: {', '.join(sorted(known))}",
            )
        kwargs: dict[str, Any] = {}
        for name, value in data.items():
            kwargs[name] = _coerce(name, value)
        return cls(**kwargs)

    def replace(self, **changes: Any) -> "AnomalyConfig":
        """A copy with some fields changed, re-validated."""
        return replace(self, **changes)

    # --- output -------------------------------------------------------------
    def as_dict(self) -> dict[str, Any]:
        """Every threshold, as data - for a metric's ``details`` or a report."""
        out: dict[str, Any] = {}
        for field in fields(self):
            value = getattr(self, field.name)
            if isinstance(value, tuple):
                value = list(value)
            out[field.name] = value
        return out

    def duration(self, name: str) -> timedelta:
        """One of the ``*_minutes`` fields as a :class:`timedelta`."""
        return timedelta(minutes=float(getattr(self, name)))

    @classmethod
    def option_specs(cls) -> tuple[tuple[str, str, Any, str], ...]:
        """The defaults as ``(name, type, default, help)`` tuples.

        A plugin builds :class:`~fitbit_monitor.plugins.contract.PluginOption` objects
        straight out of this, so the option surface a plugin advertises and the
        thresholds the engine actually ran cannot drift apart.
        """
        specs: list[tuple[str, str, Any, str]] = []
        for field in fields(cls):
            spec = _OPTION_KINDS.get(field.name)
            if spec is None:  # pragma: no cover - guarded by the test that every field is declared
                continue
            name, kind, render = spec
            default = getattr(cls(), field.name)
            help_text = _OPTION_HELP.get(field.name, "")
            specs.append((name, kind, render(default), help_text))
        return tuple(specs)


def _as_sequence(value: Any) -> tuple[Any, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(value)
    raise AnomalyConfigError(f"expected a list, got {value!r}")


def _as_contexts(value: Any) -> tuple[str, ...]:
    items = _as_sequence(value)
    if not items:
        raise AnomalyConfigError("contexts must name at least one context")
    out = tuple(str(item).strip().lower() for item in items)
    bad = [item for item in out if item not in CONTEXTS]
    if bad:
        raise AnomalyConfigError(
            f"unknown context(s): {', '.join(bad)}",
            hint=f"valid contexts are {', '.join(CONTEXTS)}",
        )
    # Order matters downstream (first context wins a tie on confidence), so duplicates
    # would double-count one context: collapse them, keep the order given.
    return tuple(dict.fromkeys(out))


def _as_tz_name(value: Any) -> str | None:
    if value is None:
        return None
    name = str(value).strip()
    if not name:
        return None
    try:
        resolve_timezone(name)
    except FmError as exc:  # a zone the tz database does not have
        raise AnomalyConfigError(
            f"unknown time_zone {name!r}: {exc}", hint=exc.hint or None
        ) from exc
    return name


def _coerce(name: str, value: Any) -> Any:
    """Cast one config value to the declared type of the field it belongs to."""
    declared = {f.name: f.type for f in fields(AnomalyConfig)}
    kind = str(declared[name])
    try:
        if kind == "int":
            if isinstance(value, bool):
                raise ValueError("a bool is not a count")
            return int(value)
        if kind == "float":
            if isinstance(value, bool):
                raise ValueError("a bool is not a number")
            return float(value)
        if kind == "bool":
            return bool(value)
        if kind == "tuple[str, ...]":
            return value if isinstance(value, (list, tuple)) else [value]
        if kind in ("str", "str | None"):
            if value is None:
                # Not every string field takes a value: ``time_zone = None`` is a real setting
                # (it means UTC), and ``str(None)`` would turn it into a zone called "None".
                return None
            return str(value)
    except (TypeError, ValueError) as exc:
        raise AnomalyConfigError(f"{name} must be {kind}, got {value!r} ({exc})") from exc
    return value


#: ``field -> (option name, PluginOption type, default renderer)`` for
#: :meth:`AnomalyConfig.option_specs`. Durations stay plain float minutes: a plugin can
#: declare them as ``duration`` if it wants ``"20m"`` in a file, and minutes keep the
#: printed default identical to the number the rule divides by.
_OPTION_KINDS: dict[str, tuple[str, str, Any]] = {
    "min_baseline_days": ("min_baseline_days", "int", int),
    "provisional_min_days": ("provisional_min_days", "int", int),
    "baseline_window_days": ("baseline_window_days", "int", int),
    "contexts": ("contexts", "list", lambda v: list(v)),
    "exclude_exercise_in": ("exclude_exercise_in", "list", lambda v: list(v)),
    "z_high": ("z_high", "float", float),
    "z_low": ("z_low", "float", float),
    "min_high_delta_bpm": ("min_high_delta_bpm", "float", float),
    "min_low_delta_bpm": ("min_low_delta_bpm", "float", float),
    "min_sustained_minutes": ("min_sustained_minutes", "float", float),
    "smoothing_minutes": ("smoothing_minutes", "float", float),
    "merge_gap_minutes": ("merge_gap_minutes", "float", float),
    "irregularity_block_minutes": ("irregularity_block_minutes", "float", float),
    "irregularity_min_intervals": ("irregularity_min_intervals", "int", int),
    "irregularity_min_blocks": ("irregularity_min_blocks", "int", int),
    "irregularity_min_minutes": ("irregularity_min_minutes", "float", float),
    "irregularity_z": ("irregularity_z", "float", float),
    "irregularity_floor": ("irregularity_floor", "float", float),
    "provisional_confidence_cap": ("provisional_confidence_cap", "float", float),
    "confidence_ceiling": ("confidence_ceiling", "float", float),
    "min_coverage": ("min_coverage", "float", float),
    "min_finding_samples": ("min_finding_samples", "int", int),
    "min_spread_bpm": ("min_spread_bpm", "float", float),
    "min_spread_index": ("min_spread_index", "float", float),
    "max_evidence_points": ("max_evidence_points", "int", int),
    "nocturnal_low_margin_bpm": ("nocturnal_low_margin_bpm", "float", float),
    "time_zone": ("time_zone", "str", lambda v: v or "UTC"),
    "dataset": ("dataset", "str", str),
    "field": ("field", "str", str),
    "context_warmup_hours": ("context_warmup_hours", "float", float),
}

#: One line each, shown by ``fm plugins show``.
_OPTION_HELP: dict[str, str] = {
    "min_baseline_days": "resting days needed before a finding may claim full confidence",
    "provisional_min_days": "resting days below which no personal baseline is built at all",
    "baseline_window_days": "how many days of history the personal baseline uses",
    "contexts": "which windows to build baselines and findings in: rest, sleep",
    "exclude_exercise_in": "contexts whose detection domain subtracts exercise spans",
    "z_high": "robust z-score above the personal resting level that counts as sustained-high",
    "z_low": "robust z-score below the personal resting level that counts as sustained-low",
    "min_high_delta_bpm": "smallest elevation above the personal level the high rule may use",
    "min_low_delta_bpm": "smallest drop below the personal level the low rule may use",
    "min_sustained_minutes": "how long a rate must stay past its threshold to be a finding",
    "smoothing_minutes": "moving-mean width the rate rules threshold on",
    "merge_gap_minutes": "two episodes closer than this are one finding",
    "irregularity_block_minutes": "minimum width of a scatter window (widened for coarse data)",
    "irregularity_min_intervals": "intervals a scatter window needs before it counts",
    "irregularity_min_blocks": "consecutive over-threshold scatter positions an episode needs",
    "irregularity_min_minutes": "how long an irregular episode must last to be a finding",
    "irregularity_z": "robust z-score over the wearer's own ordinary scatter",
    "irregularity_floor": "absolute floor on the scatter index, so a steady wearer cannot fire on jitter",
    "provisional_confidence_cap": "confidence cap while the baseline is not yet established",
    "confidence_ceiling": "the engine never reports a confidence above this",
    "min_coverage": "observed fraction of an episode below which it is suppressed",
    "min_finding_samples": "readings an episode needs before it is reportable at all",
    "min_spread_bpm": "floor on a wearer's bpm spread, so a measured-to-zero spread cannot fire on nothing",
    "min_spread_index": "floor on a wearer's scatter spread, same reason in scatter units",
    "max_evidence_points": "how many sampled readings a finding carries as evidence",
    "time_zone": "timezone for day boundaries and episode anchors (default UTC)",
    "dataset": "which stored series the engine reads",
    "field": "which field of that series is the heart rate",
    "context_warmup_hours": "how far past the window the engine reads to put its edge days in context",
    "nocturnal_low_margin_bpm": (
        "bpm above the wearer's sleeping level a low stretch may sit at and still be the night, not the day"
    ),
}


def declared_option_names() -> Sequence[str]:
    """Every option name the engine can read, for a plugin that wants to check itself."""
    return tuple(name for name, _kind, _default, _help in AnomalyConfig.option_specs())


__all__ = [
    "CONTEXTS",
    "EXERCISE_EXCLUDED_CONTEXTS",
    "AnomalyConfig",
    "AnomalyConfigError",
    "declared_option_names",
]
