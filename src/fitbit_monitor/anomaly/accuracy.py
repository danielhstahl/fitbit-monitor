"""The labelled fixture format and the accuracy gate that scores the detector against it.

Without labelled data every threshold in :mod:`fitbit_monitor.anomaly.config` is an
opinion, and the only thing that turns an opinion into an engineering decision is a number
computed against answers somebody wrote down before looking at the output. That is what
this module is for: it holds the *format* of those answers, the code that reads and writes
them, the scorer that compares a :class:`~fitbit_monitor.anomaly.models.DetectionResult`
against them, and the floors the project promises to stay above.

What a label is
---------------
A label is a window, a rule, and an **expected verdict**:

``alert``
    the detector must report a finding of this type overlapping this window;
``held``
    the shipped policy says this window is not reportable - and the engine must still
    *account for* it: a suppression naming the window, not silence. That distinction is the
    whole reason this verdict exists. "We did not tell you, and here is why we stayed quiet"
    is a promise this engine makes, and a label is the only way to test it;
``clear``
    no finding of the named rules may appear in this window at all. This is what a clean
    all-night window is expressed as, and the count of windows that stayed silent is what
    makes "zero false positives" a measured claim instead of an absence of complaints.

Every label is synthetic and says so. The shipped fixture is generated from the deterministic
``fm dev seed`` corpus (:mod:`fitbit_monitor.store.seed`), whose injected episodes are already
written to ``dev_annotation`` as synthetic annotations; a user who later wants the gate to
speak about *their* exported episodes adds a corpus whose ``source`` is a store they own,
and the scorer does not care which.

Why some labels are ``held``
----------------------------
The generator's abnormal episodes are, deliberately, the shapes a wrist recording makes: a
sustained elevation, a sustained bradycardia overnight, a stretch of enormous beat-to-beat
scatter. The elevation it injects (133-155 bpm for 40-90 minutes, mid-morning) is
indistinguishable from the same generator's workouts, and the shipped awake-context policy
subtracts the inferred exercise mask - so a labelled daytime elevation at or above the
exercise floor is expected to be *attributed to activity and reported as suppressed*, not
alerted on. Labelling those windows ``alert`` would demand a detector that alerts on workouts,
which is the opposite of what :mod:`fitbit_monitor.hr.exercise` is for; labelling them
``clear`` would demand that the engine not see them at all, which is the opposite of what
:mod:`fitbit_monitor.anomaly.engine` promises. ``held`` is the honest verdict, and the gate
checks it: found-and-explained passes, silently dropped fails.

The floors
----------
:data:`SHIPPED_FLOORS` states the numbers the project claims. They are measured, not
aspirational, and they are stated with a margin: the current fixture measures 1.000
precision and 1.000 recall on the alertable labels, and the floors sit below that so the
gate fails on a *change in behaviour*, not on a rounding error. See ``docs/detection.md``
for the measured matrix and what each floor is protecting.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..hr.daily import FeatureOptions, daily_features
from ..hr.util import iso
from ..store.reader import ReadOnlyStore
from ..store.seed import SeedPlan, seed_store
from ..store.writer import SeriesStore
from .config import AnomalyConfig
from .errors import AnomalyValueError
from .models import (
    FINDING_IRREGULAR,
    FINDING_SUSTAINED_HIGH,
    FINDING_SUSTAINED_LOW,
    FINDING_TYPES,
    RATE_FINDING_TYPES,
    DetectionResult,
    Finding,
)

#: Format version of the checked-in fixture. Bumped when the JSON shape changes.
FORMAT_VERSION = 1

#: A labelled episode that must be reported.
VERDICT_ALERT = "alert"
#: A labelled episode that must not be reported but must be accounted for.
VERDICT_HELD = "held"
#: A window in which the named rules must stay silent.
VERDICT_CLEAR = "clear"
#: Every verdict a label may carry.
VERDICTS: tuple[str, ...] = (VERDICT_ALERT, VERDICT_HELD, VERDICT_CLEAR)

#: How far a finding may stray from a labelled window and still count as *that* episode.
#:
#: The engine's episodes are trimmed to the readings that crossed the bar and merged across
#: ``merge_gap_minutes``, so a reported window is rarely the same shape as the injected one;
#: 45 minutes is comfortably longer than either adjustment and short enough that a finding
#: from the neighbouring day does not accidentally collect a label.
DEFAULT_TOLERANCE = timedelta(minutes=45)

#: A night shorter than this is a window the fixture will not call an all-night window.
MIN_NIGHT_MINUTES = 120.0

#: Which rules a compact (string-form) clean-window row constrains: the two rate rules,
#: which is the acceptance criterion "clean nights produce no sustained-rate findings".
CLEAR_WINDOW_RULES: tuple[str, ...] = RATE_FINDING_TYPES

#: The label a dev-seed episode of each injected kind gets, and why.
#:
#: ``sustained_high`` is conditional: the generator's high episodes straddle the exercise
#: mask's absolute floor, so the labelling function below looks at the episode's own mean
#: rather than its kind alone.
KIND_TO_FINDING_TYPE: dict[str, str] = {
    "sustained_high": FINDING_SUSTAINED_HIGH,
    "sustained_low": FINDING_SUSTAINED_LOW,
    "irregular": FINDING_IRREGULAR,
}


def _as_aware(value: Any) -> datetime:
    """Parse or pass through an ISO timestamp, forcing UTC-awareness."""
    if isinstance(value, datetime):
        moment = value
    else:
        moment = datetime.fromisoformat(str(value))
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _windows_overlap(
    a_onset: datetime, a_offset: datetime, b_onset: datetime, b_offset: datetime
) -> bool:
    """Do two half-open windows share any time at all?"""
    return a_onset < b_offset and b_onset < a_offset


@dataclass(frozen=True)
class EpisodeLabel:
    """One labelled episode: a window, the rule expected there, and the expected verdict."""

    wearer: str
    type: str
    onset: datetime
    offset: datetime
    verdict: str = VERDICT_ALERT
    detail: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "onset", _as_aware(self.onset))
        object.__setattr__(self, "offset", _as_aware(self.offset))
        if self.type not in FINDING_TYPES:
            raise AnomalyValueError(
                f"label {self.wearer!r} expects unknown finding type {self.type!r}",
                hint=f"a label's type is one of: {', '.join(FINDING_TYPES)}",
            )
        if self.verdict not in VERDICTS:
            raise AnomalyValueError(
                f"label {self.wearer!r} has unknown verdict {self.verdict!r}",
                hint=f"a verdict is one of: {', '.join(VERDICTS)}",
            )
        if self.verdict == VERDICT_CLEAR:
            raise AnomalyValueError(
                f"label {self.wearer!r}: an episode cannot be labelled 'clear'; "
                "only windows can (see WindowLabel)"
            )
        if self.offset <= self.onset:
            raise AnomalyValueError(f"label {self.wearer!r} ends before it starts")

    @property
    def label_id(self) -> str:
        return f"{self.wearer}:{self.type}:{self.onset.isoformat()}"

    @property
    def duration_minutes(self) -> float:
        return round((self.offset - self.onset).total_seconds() / 60.0, 1)

    def overlaps(self, onset: datetime, offset: datetime, tolerance: timedelta) -> bool:
        """Does a finding from ``onset`` to ``offset`` belong to this labelled window?"""
        return _windows_overlap(
            self.onset - tolerance, self.offset + tolerance, onset, offset
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "onset": self.onset.isoformat(),
            "offset": self.offset.isoformat(),
            "verdict": self.verdict,
            "detail": dict(self.detail),
        }

    @classmethod
    def from_dict(cls, wearer: str, row: Mapping[str, Any]) -> "EpisodeLabel":
        return cls(
            wearer=wearer,
            type=str(row["type"]),
            onset=_as_aware(row["onset"]),
            offset=_as_aware(row["offset"]),
            verdict=str(row.get("verdict", VERDICT_ALERT)),
            detail=dict(row.get("detail") or {}),
        )


@dataclass(frozen=True)
class WindowLabel:
    """A window in which the named rules are expected to stay (or not stay) silent.

    The shipped use is a night with no labelled episode in it - the fixture's way of asking
    "did the sustained-rate rules fire on a clean all-night window?" - but nothing here is
    specific to nights, and a user labelling their own exported data can mark any stretch
    they know to be ordinary.
    """

    wearer: str
    onset: datetime
    offset: datetime
    rules: tuple[str, ...] = CLEAR_WINDOW_RULES
    verdict: str = VERDICT_CLEAR
    detail: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "onset", _as_aware(self.onset))
        object.__setattr__(self, "offset", _as_aware(self.offset))
        object.__setattr__(self, "rules", tuple(str(rule) for rule in self.rules))
        if self.verdict != VERDICT_CLEAR:
            raise AnomalyValueError(
                f"window {self.wearer!r}: a window's only verdict is 'clear' "
                "(an episode that should be found is an EpisodeLabel)"
            )
        unknown = [rule for rule in self.rules if rule not in FINDING_TYPES]
        if unknown:
            raise AnomalyValueError(
                f"window {self.wearer!r} names unknown rule(s): {', '.join(unknown)}"
            )
        if self.offset <= self.onset:
            raise AnomalyValueError(f"window {self.wearer!r} ends before it starts")

    @property
    def label_id(self) -> str:
        return f"{self.wearer}:window:{self.onset.isoformat()}"

    @property
    def duration_minutes(self) -> float:
        return round((self.offset - self.onset).total_seconds() / 60.0, 1)

    def constrains(self, finding_type: str) -> bool:
        return finding_type in self.rules

    def contains(self, moment: datetime) -> bool:
        return self.onset <= moment < self.offset

    def overlaps(self, onset: datetime, offset: datetime) -> bool:
        return _windows_overlap(self.onset, self.offset, onset, offset)

    def as_dict(self) -> dict[str, Any]:
        return {
            "onset": self.onset.isoformat(),
            "offset": self.offset.isoformat(),
            "rules": list(self.rules),
            "verdict": self.verdict,
            "detail": dict(self.detail),
        }

    @classmethod
    def from_row(cls, wearer: str, row: Any) -> "WindowLabel":
        """Read either the compact ``"onset..offset"`` string form or the full mapping.

        The compact form is what the recorder writes for nights - one line each keeps a
        fixture with three hundred of them readable - and it carries the default rules and
        verdict, which is all a clean night needs.
        """
        if isinstance(row, str):
            onset, _, offset = row.partition("..")
            if not offset:
                raise AnomalyValueError(
                    f"window row {row!r} is not 'onset..offset'",
                    hint="write the two ISO instants separated by '..'",
                )
            return cls(wearer=wearer, onset=_as_aware(onset), offset=_as_aware(offset))
        return cls(
            wearer=wearer,
            onset=_as_aware(row["onset"]),
            offset=_as_aware(row["offset"]),
            rules=tuple(row.get("rules") or CLEAR_WINDOW_RULES),
            verdict=str(row.get("verdict", VERDICT_CLEAR)),
            detail=dict(row.get("detail") or {}),
        )

    def as_row(self) -> Any:
        """The compact form when it says everything, the full mapping when it does not."""
        if tuple(self.rules) == CLEAR_WINDOW_RULES and not self.detail:
            return f"{self.onset.isoformat()}..{self.offset.isoformat()}"
        return self.as_dict()


# --- the fixture -----------------------------------------------------------
def expected_verdict_for_episode(kind: str, mean_hr: float, exercise_floor_bpm: float) -> tuple[str, str]:
    """What the shipped policy says should happen to one injected episode, and the reason.

    This is a statement about the *policy*, not about the run being scored: it never looks at
    a finding. A daytime elevation that sits at or above the exercise mask's absolute floor is
    a stretch the awake context attributes to activity by design, so the expected verdict is
    ``held``; anything else the generator injects is something the detector is supposed to
    report.
    """
    if kind == "sustained_high" and mean_hr >= exercise_floor_bpm:
        return (
            VERDICT_HELD,
            f"mean {mean_hr:g} bpm is at or above the exercise mask's absolute floor of "
            f"{exercise_floor_bpm:g} bpm, and the awake context subtracts the inferred "
            "exercise mask: the shipped policy attributes it to activity and reports it "
            "as suppressed",
        )
    return (VERDICT_ALERT, "a shape the detector is built to report")


def label_dev_seed(
    wearer: str,
    plan: SeedPlan,
    store: ReadOnlyStore,
    *,
    exercise_floor_bpm: float = 110.0,
    min_night_minutes: float = MIN_NIGHT_MINUTES,
    options: FeatureOptions | None = None,
    config: AnomalyConfig | None = None,
) -> "LabelledCorpus":
    """Derive the labels for a corpus the dev-seed generator just wrote.

    The episodes come from :attr:`~fitbit_monitor.store.seed.SeedPlan.episodes` - the same
    records ``dev_annotation`` holds - and the clean windows are every detected night of at
    least ``min_night_minutes`` that contains no episode of any kind. Both are read from what
    the corpus *is*, so re-recording the fixture after a deliberate generator change is one
    command and a diff, not a hand-edit.
    """
    since = datetime(plan.start.year, plan.start.month, plan.start.day, tzinfo=UTC)
    until = datetime(plan.end.year, plan.end.month, plan.end.day, tzinfo=UTC) + timedelta(days=1)
    episodes = tuple(
        EpisodeLabel(
            wearer=wearer,
            type=KIND_TO_FINDING_TYPE[episode.kind],
            onset=_as_aware(episode.start),
            offset=_as_aware(episode.end) + timedelta(minutes=5),
            verdict=expected_verdict_for_episode(episode.kind, episode.mean_hr, exercise_floor_bpm)[0],
            detail={
                "kind": episode.kind,
                "mean_hr": episode.mean_hr,
                "minutes": episode.minutes,
                "synthetic": True,
                "why": expected_verdict_for_episode(episode.kind, episode.mean_hr, exercise_floor_bpm)[1],
            },
        )
        for episode in plan.episodes
    )
    windows: list[WindowLabel] = []
    for day in daily_features(store, since, until, options=options):
        night = day.sleep
        if not night.detected or night.minutes < min_night_minutes:
            continue
        onset, offset = _as_aware(night.start), _as_aware(night.end)
        if any(
            _windows_overlap(label.onset, label.offset, onset, offset) for label in episodes
        ):
            continue
        # No per-window detail: the compact row form is what keeps a fixture with three
        # hundred of these reviewable, and a night's own minutes are in the store anyway.
        windows.append(WindowLabel(wearer=wearer, onset=onset, offset=offset))
    return LabelledCorpus(
        wearer=wearer,
        source={
            "kind": "dev_seed",
            "seed": plan.seed,
            "days": plan.days,
            "end": plan.end.isoformat(),
            "dropped_days": len(plan.dropped_days),
        },
        episodes=episodes,
        windows=tuple(windows),
        detail={"config": (config or AnomalyConfig()).as_dict()},
    )


@dataclass(frozen=True)
class LabelledCorpus:
    """One wearer: how to get its data, and every label asserted about it."""

    wearer: str
    source: Mapping[str, Any]
    episodes: tuple[EpisodeLabel, ...] = ()
    windows: tuple[WindowLabel, ...] = ()
    detail: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_synthetic(self) -> bool:
        """True when the data came from the dev-seed generator rather than a person."""
        return str(self.source.get("kind", "dev_seed")) == "dev_seed"

    def labels(self) -> tuple[EpisodeLabel | WindowLabel, ...]:
        return (*self.episodes, *self.windows)

    def as_dict(self) -> dict[str, Any]:
        return {
            "wearer": self.wearer,
            "source": dict(self.source),
            "episodes": [label.as_dict() for label in self.episodes],
            "windows": [window.as_row() for window in self.windows],
            "detail": dict(self.detail),
        }

    @classmethod
    def from_dict(cls, row: Mapping[str, Any]) -> "LabelledCorpus":
        wearer = str(row["wearer"])
        return cls(
            wearer=wearer,
            source=dict(row.get("source") or {}),
            episodes=tuple(EpisodeLabel.from_dict(wearer, item) for item in row.get("episodes") or ()),
            windows=tuple(WindowLabel.from_row(wearer, item) for item in row.get("windows") or ()),
            detail=dict(row.get("detail") or {}),
        )

    def build_store(self, where: Path) -> "BuiltCorpus":
        """Materialise this corpus's data under ``where`` and hand back a read-only handle.

        A ``dev_seed`` corpus is regenerated here, deterministically - same seed, same days,
        same end date, same bytes - which is what lets the fixture stay a few kilobytes of
        labels instead of a megabyte of checked-in heart rate. A corpus whose ``source``
        names a ``store_path`` is a store somebody else produced and is simply opened.
        """
        kind = str(self.source.get("kind", "dev_seed"))
        if kind == "store":
            path = Path(str(self.source["store_path"]))
            return BuiltCorpus(corpus=self, store=ReadOnlyStore(path), plan=None)
        if kind != "dev_seed":
            raise AnomalyValueError(
                f"corpus {self.wearer!r} has unknown source kind {kind!r}",
                hint="a source is {'kind': 'dev_seed', ...} or {'kind': 'store', 'store_path': ...}",
            )
        path = Path(where) / f"{self.wearer}.sqlite3"
        writer = SeriesStore(path)
        try:
            plan = seed_store(
                writer,
                days=int(self.source.get("days", 90)),
                seed=int(self.source.get("seed", 1337)),
                end=date.fromisoformat(str(self.source["end"])),
                drop_missing_days=bool(self.source.get("drop_missing_days", True)),
                anomalies=self.source.get("anomalies"),
            )
        finally:
            writer.close()
        return BuiltCorpus(corpus=self, store=ReadOnlyStore(path), plan=plan)


@dataclass(frozen=True)
class BuiltCorpus:
    """A corpus whose data now exists on disk, plus what the generator wrote (if it generated).

    ``plan`` is what makes the fixture's self-check possible without a second seeding pass:
    the same plan the labels were derived from when the fixture was recorded is available to
    re-derive them from, in the same test run that scores the detector.
    """

    corpus: LabelledCorpus
    store: ReadOnlyStore
    plan: SeedPlan | None = None

    def close(self) -> None:
        self.store.close()

    def rederived_labels(self) -> LabelledCorpus:
        """Rebuild this corpus's labels from the data itself (dev-seed corpora only)."""
        if self.plan is None:
            raise AnomalyValueError(
                f"corpus {self.corpus.wearer!r} was opened from a store, so its labels cannot "
                "be re-derived; the fixture is the only record of them"
            )
        return label_dev_seed(self.corpus.wearer, self.plan, self.store)


@dataclass(frozen=True)
class LabelledSuite:
    """Every wearer the gate scores, and the tolerance that matches findings to labels."""

    corpora: tuple[LabelledCorpus, ...]
    tolerance: timedelta = DEFAULT_TOLERANCE
    detail: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        names = [corpus.wearer for corpus in self.corpora]
        if not names:
            raise AnomalyValueError("a labelled suite with no wearers scores nothing")
        if len(set(names)) != len(names):
            raise AnomalyValueError(f"corpus names must be unique, got {names}")

    @property
    def labels(self) -> tuple[EpisodeLabel | WindowLabel, ...]:
        return tuple(label for corpus in self.corpora for label in corpus.labels())

    @property
    def episodes(self) -> tuple[EpisodeLabel, ...]:
        return tuple(label for corpus in self.corpora for label in corpus.episodes)

    @property
    def windows(self) -> tuple[WindowLabel, ...]:
        return tuple(label for corpus in self.corpora for label in corpus.windows)

    def build(self, where: Path) -> dict[str, BuiltCorpus]:
        """Materialise every corpus in the suite, keyed by wearer name."""
        Path(where).mkdir(parents=True, exist_ok=True)
        return {corpus.wearer: corpus.build_store(where) for corpus in self.corpora}

    # --- the file -----------------------------------------------------------
    def as_dict(self) -> dict[str, Any]:
        return {
            "format": FORMAT_VERSION,
            "tolerance_minutes": self.tolerance.total_seconds() / 60.0,
            "note": (
                "Synthetic labelled fixtures: generated from `fm dev seed` episodes, each "
                "labelled with the rule it exercises and the verdict the shipped policy owes "
                "it. Nothing here came from an account or a device."
            ),
            "detail": dict(self.detail),
            "corpora": [corpus.as_dict() for corpus in self.corpora],
        }

    def to_json_text(self) -> str:
        return json.dumps(self.as_dict(), indent=2, sort_keys=False) + "\n"

    def save(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json_text(), encoding="utf-8")
        return path

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "LabelledSuite":
        version = int(raw.get("format", FORMAT_VERSION))
        if version != FORMAT_VERSION:
            raise AnomalyValueError(
                f"fixture format {version} is not understood by this build",
                hint=f"this build writes format {FORMAT_VERSION}; re-record the fixture",
            )
        return cls(
            corpora=tuple(LabelledCorpus.from_dict(row) for row in raw.get("corpora") or ()),
            tolerance=timedelta(minutes=float(raw.get("tolerance_minutes", DEFAULT_TOLERANCE.total_seconds() / 60.0))),
            detail=dict(raw.get("detail") or {}),
        )

    @classmethod
    def load(cls, path: Path) -> "LabelledSuite":
        return cls.from_mapping(json.loads(Path(path).read_text(encoding="utf-8")))


# --- scoring ---------------------------------------------------------------
#: An ``alert`` label that a matching finding turned up for.
STATUS_REPORTED = "reported"
#: An ``alert`` label with nothing reported in it.
STATUS_MISSED = "missed"
#: A ``held`` label the engine saw, explained and held back - the good outcome for it.
STATUS_ACCOUNTED = "accounted"
#: A ``held`` label that was reported anyway: the policy was not kept.
STATUS_REPORTED_ANYWAY = "reported_anyway"
#: A ``held`` label that produced neither a finding nor a suppression - the one answer this
#: engine is not allowed to give, and the reason this verdict is worth scoring at all.
STATUS_UNACCOUNTED = "unaccounted"
#: A clean window in which none of its rules spoke.
STATUS_KEPT_SILENT = "kept_silent"
#: A clean window a rule spoke inside.
STATUS_VIOLATED = "violated"


@dataclass(frozen=True)
class LabelOutcome:
    """What happened to one label: the verdict it asked for, the verdict it got."""

    label_id: str
    wearer: str
    kind: str
    rule: str
    verdict: str
    status: str
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label_id,
            "wearer": self.wearer,
            "kind": self.kind,
            "rule": self.rule,
            "expected": self.verdict,
            "status": self.status,
            **({"detail": self.detail} if self.detail else {}),
        }


@dataclass(frozen=True)
class RuleCounts:
    """One rule's slice of the matrix, as numbers that go in a table."""

    alert_labels: int = 0
    reported_labels: int = 0
    held_labels: int = 0
    held_accounted: int = 0
    held_violations: int = 0
    held_unaccounted: int = 0
    true_positive_findings: int = 0
    false_positive_findings: int = 0
    repeat_findings: int = 0

    @property
    def recall(self) -> float | None:
        """Alert labels reported over alert labels; ``None`` when this rule has none."""
        if not self.alert_labels:
            return None
        return self.reported_labels / self.alert_labels

    @property
    def precision(self) -> float | None:
        denominator = self.true_positive_findings + self.false_positive_findings
        if not denominator:
            return None
        return self.true_positive_findings / denominator

    @property
    def held_shortfall(self) -> int:
        """Held labels that were reported against policy or dropped without a word."""
        return self.held_violations + self.held_unaccounted

    def as_dict(self) -> dict[str, Any]:
        return {
            "alert_labels": self.alert_labels,
            "reported_labels": self.reported_labels,
            "recall": None if self.recall is None else round(self.recall, 4),
            "held_labels": self.held_labels,
            "held_accounted": self.held_accounted,
            "held_violations": self.held_violations,
            "held_unaccounted": self.held_unaccounted,
            "true_positive_findings": self.true_positive_findings,
            "false_positive_findings": self.false_positive_findings,
            "repeat_findings": self.repeat_findings,
            "precision": None if self.precision is None else round(self.precision, 4),
        }


@dataclass(frozen=True)
class AccuracyReport:
    """One scoring pass: every label's outcome, the numbers derived from them, the matrix.

    Episodes, not messages, are the unit. Two findings for one labelled episode - the same
    night reported by both contexts is a real shape here, and the engine's ``dedup_key``
    carries the context precisely because of it - count as that episode being reported once;
    the second is listed in :attr:`repeats` rather than charged as a false alarm a sink would
    have deduplicated anyway.
    """

    config_name: str
    tolerance: timedelta
    outcomes: tuple[LabelOutcome, ...] = ()
    true_positives: tuple[Finding, ...] = ()
    false_positives: tuple[Finding, ...] = ()
    repeats: tuple[Finding, ...] = ()
    clean_window_violations: tuple[tuple[Finding, WindowLabel], ...] = ()
    clean_windows: int = 0
    #: False positives of a rate rule inside a labelled clean window. This is the number the
    #: acceptance criterion says must be zero, so it is carried on its own rather than buried
    #: in a ratio - it is not a trade-off, it is a promise.
    clean_window_rate_false_positives: int = 0

    @property
    def findings(self) -> tuple[Finding, ...]:
        return (*self.true_positives, *self.repeats, *self.false_positives)

    def counts_by_rule(self) -> dict[str, RuleCounts]:
        """Every rule's slice of the matrix, including rules that only show up as findings."""
        tallies: dict[str, dict[str, int]] = {name: {} for name in FINDING_TYPES}

        def bump(rule: str, field_name: str) -> None:
            row = tallies.setdefault(rule, {})
            row[field_name] = row.get(field_name, 0) + 1

        for outcome in self.outcomes:
            if outcome.kind == "window":
                continue
            if outcome.verdict == VERDICT_ALERT:
                bump(outcome.rule, "alert_labels")
                if outcome.status == STATUS_REPORTED:
                    bump(outcome.rule, "reported_labels")
            elif outcome.status == STATUS_ACCOUNTED:
                bump(outcome.rule, "held_labels")
                bump(outcome.rule, "held_accounted")
            elif outcome.status == STATUS_REPORTED_ANYWAY:
                bump(outcome.rule, "held_labels")
                bump(outcome.rule, "held_violations")
            else:
                bump(outcome.rule, "held_labels")
                bump(outcome.rule, "held_unaccounted")
        for role, findings in (
            ("true_positive_findings", self.true_positives),
            ("false_positive_findings", self.false_positives),
            ("repeat_findings", self.repeats),
        ):
            for finding in findings:
                bump(finding.type, role)
        return {
            rule: RuleCounts(**values) for rule, values in ((r, tallies.get(r, {})) for r in tallies)
        }

    # --- the headline numbers ------------------------------------------------
    @property
    def false_positives_count(self) -> int:
        """Findings that belong to no labelled episode - the FP in precision's denominator."""
        return len(self.false_positives)

    @property
    def alert_labels(self) -> int:
        return sum(counts.alert_labels for counts in self.counts_by_rule().values())

    @property
    def reported_labels(self) -> int:
        return sum(counts.reported_labels for counts in self.counts_by_rule().values())

    @property
    def held_shortfall(self) -> int:
        return sum(counts.held_shortfall for counts in self.counts_by_rule().values())

    @property
    def precision(self) -> float:
        """Labelled episodes over everything the run spoke about: 1.0 when nothing was a false alarm."""
        tp = len(self.true_positives)
        denominator = tp + len(self.false_positives)
        return 1.0 if not denominator else tp / denominator

    @property
    def recall(self) -> float:
        """1.0 when there is nothing to recall, so a rule with no alert labels cannot fail it."""
        return 1.0 if not self.alert_labels else self.reported_labels / self.alert_labels

    def recall_for(self, finding_type: str) -> float | None:
        return self.counts_by_rule()[finding_type].recall

    @property
    def clean_windows_kept_silent(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.status == STATUS_KEPT_SILENT)

    @property
    def confidence_ranking(self) -> float:
        """How well confidence separates a true finding from a false one (pairwise, ties 0.5).

        This is "confidence ranks true findings above false ones" as a number that does not
        vanish when precision is perfect: with nothing to mis-rank it is 1.0 by definition,
        and :attr:`best_is_true` still says whether the run's loudest finding was a real one.
        """
        return rank_confidences(self.true_positives, self.false_positives)

    @property
    def best_is_true(self) -> bool:
        """Was the run's highest-confidence finding a labelled one?"""
        best = max(self.findings, key=lambda finding: finding.confidence, default=None)
        return best is None or best not in self.false_positives

    def outcomes_with(self, *statuses: str) -> tuple[LabelOutcome, ...]:
        return tuple(outcome for outcome in self.outcomes if outcome.status in statuses)

    def as_dict(self) -> dict[str, Any]:
        return {
            "config": self.config_name,
            "tolerance_minutes": self.tolerance.total_seconds() / 60.0,
            "labels": len(self.outcomes),
            "alert_labels": self.alert_labels,
            "reported_labels": self.reported_labels,
            "findings": len(self.findings),
            "true_positives": len(self.true_positives),
            "false_positives": len(self.false_positives),
            "repeat_findings": len(self.repeats),
            "held_labels_unaccounted": self.held_shortfall,
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "confidence_ranking": round(self.confidence_ranking, 4),
            "best_finding_is_true": self.best_is_true,
            "clean_windows": self.clean_windows,
            "clean_windows_kept_silent": self.clean_windows_kept_silent,
            "clean_window_rate_false_positives": self.clean_window_rate_false_positives,
            "by_rule": {name: counts.as_dict() for name, counts in sorted(self.counts_by_rule().items())},
            "outcomes": [outcome.as_dict() for outcome in self.outcomes],
            "false_positive_findings": [
                {
                    "type": finding.type,
                    "context": finding.context,
                    "onset": iso(finding.onset),
                    "offset": iso(finding.offset),
                    "confidence": round(finding.confidence, 3),
                }
                for finding in self.false_positives
            ],
        }

    def confusion_matrix(self) -> str:
        """The results as a confusion matrix: what was labelled, what the run made of it.

        Rows are labels by the verdict they asked for; the columns are what came back. An
        ``expected`` row counts labelled episodes the policy says must be reported; a ``held``
        row counts episodes the policy says must not be reported but must be explained, and
        counts a "reported as" cell as a policy breach and the last column as *neither found
        nor explained* - the failure mode that motivated this whole gate. The last row is the
        clean windows, where silence is the correct answer and the number printed is how many
        of them stayed silent.
        """
        counts = self.counts_by_rule()
        headers = [f"as {name}" for name in FINDING_TYPES] + ["nothing"]
        widths = [max(len(header), 8) for header in headers]
        columns = len(headers)
        rows: list[tuple[str, list[str]]] = []
        for name in FINDING_TYPES:
            rule = counts[name]
            if rule.alert_labels:
                cells = ["-"] * columns
                cells[FINDING_TYPES.index(name)] = str(rule.reported_labels)
                cells[-1] = str(rule.alert_labels - rule.reported_labels)
                rows.append((f"expected {name}", cells))
        for name in FINDING_TYPES:
            rule = counts[name]
            if rule.held_labels:
                cells = ["-"] * columns
                cells[FINDING_TYPES.index(name)] = str(rule.held_violations)
                cells[-1] = f"{rule.held_accounted}/{rule.held_labels} explained"
                rows.append((f"held {name}", cells))
        title_width = max([len(title) for title, _ in rows] + [len("clean night windows")])
        header = " " * (title_width + 2) + "  ".join(
            f"{header:>{width}}" for header, width in zip(headers, widths)
        )
        lines = [f"confusion matrix - {self.config_name}", header]
        for title, cells in rows:
            lines.append(
                f"{title:<{title_width}}  "
                + "  ".join(f"{cell:>{width}}" for cell, width in zip(cells, widths))
            )
        lines.append(
            f"{'clean night windows':<{title_width}}  "
            + f"{self.clean_windows_kept_silent} silent of {self.clean_windows}"
            + (f", {self.clean_window_rate_false_positives} false positive(s)" if self.clean_window_rate_false_positives else "")
        )
        lines.append(
            f"{'findings':<{title_width}}  "
            f"{len(self.findings)} reported = {len(self.true_positives)} labelled, "
            f"{len(self.repeats)} repeat(s) of a labelled episode, {len(self.false_positives)} with no label"
        )
        return "\n".join(lines)


def rank_confidences(
    true_positives: Sequence[Finding], false_positives: Sequence[Finding]
) -> float:
    """Fraction of (true, false) pairs the confidence score got the right way round.

    A U-statistic - the Mann-Whitney form of an AUC - because "true findings rank above false
    ones" is a statement about order, not about any one threshold, and a single number that
    keeps its meaning when one side of the comparison is empty (then there is nothing to get
    wrong, and 1.0 is the only honest answer).
    """
    truth = [finding.confidence for finding in true_positives]
    lies = [finding.confidence for finding in false_positives]
    if not truth or not lies:
        return 1.0
    wins = sum(1.0 for good in truth for bad in lies if good > bad)
    ties = sum(1.0 for good in truth for bad in lies if good == bad)
    return (wins + 0.5 * ties) / (len(truth) * len(lies))


def score_suite(
    suite: LabelledSuite,
    results: Mapping[str, DetectionResult],
    *,
    config_name: str = "shipped defaults",
) -> AccuracyReport:
    """Compare one detection run per wearer against every label in the suite.

    Matching is deliberate and boring: a finding belongs to a label when the two windows
    overlap within the suite's tolerance and the finding's type is the one the label asked
    for. A finding that matches several labels takes the first (they are in corpus order, so
    that is the earliest one); a label that matches several findings reports the episode once
    and lists the rest as repeats.
    """
    outcomes: list[LabelOutcome] = []
    true_positives: list[Finding] = []
    false_positives: list[Finding] = []
    repeats: list[Finding] = []
    violations: list[tuple[Finding, WindowLabel]] = []
    clean_windows = 0
    for corpus in suite.corpora:
        result = results.get(corpus.wearer)
        if result is None:
            raise AnomalyValueError(
                f"no detection result was supplied for wearer {corpus.wearer!r}",
                hint="score_suite wants one DetectionResult per corpus in the suite",
            )
        findings = tuple(result.findings)
        claimed: set[int] = set()

        def matches(label: EpisodeLabel, finding: Finding) -> bool:
            return finding.type == label.type and label.overlaps(finding.onset, finding.offset, suite.tolerance)

        for label in corpus.episodes:
            hits = [index for index, finding in enumerate(findings) if matches(label, finding)]
            if label.verdict == VERDICT_ALERT:
                if hits:
                    claimed.add(hits[0])
                    true_positives.append(findings[hits[0]])
                    repeats.extend(findings[index] for index in hits[1:] if index not in claimed)
                    claimed.update(hits[1:])
                    status = STATUS_REPORTED
                    detail = ""
                else:
                    status = STATUS_MISSED
                    detail = "no finding of this type overlapped the labelled window"
            else:
                explained = any(
                    episode.type == label.type
                    and label.overlaps(episode.onset, episode.offset, suite.tolerance)
                    for episode in result.suppressed
                )
                if hits:
                    claimed.update(hits)
                    false_positives.extend(findings[index] for index in hits)
                    status = STATUS_REPORTED_ANYWAY
                    detail = "reported although the shipped policy holds this shape back"
                elif explained:
                    status = STATUS_ACCOUNTED
                    detail = "held back by the engine, with the reason reported"
                else:
                    status = STATUS_UNACCOUNTED
                    detail = "neither reported nor suppressed: the engine was silent about it"
            outcomes.append(
                LabelOutcome(
                    label_id=label.label_id,
                    wearer=corpus.wearer,
                    kind="episode",
                    rule=label.type,
                    verdict=label.verdict,
                    status=status,
                    detail=detail,
                )
            )
        leftovers = [finding for index, finding in enumerate(findings) if index not in claimed]
        false_positives.extend(leftovers)
        for window in corpus.windows:
            clean_windows += 1
            inside = [
                finding
                for finding in leftovers
                if window.constrains(finding.type) and window.overlaps(finding.onset, finding.offset)
            ]
            violations.extend((finding, window) for finding in inside)
            outcomes.append(
                LabelOutcome(
                    label_id=window.label_id,
                    wearer=corpus.wearer,
                    kind="window",
                    rule=",".join(window.rules),
                    verdict=VERDICT_CLEAR,
                    status=STATUS_VIOLATED if inside else STATUS_KEPT_SILENT,
                    detail=(
                        f"{len(inside)} rate finding(s) spoke inside a window labelled clear"
                        if inside
                        else ""
                    ),
                )
            )
    rate_violations = {
        (finding.type, iso(finding.onset))
        for finding, window in violations
        if finding.type in RATE_FINDING_TYPES
    }
    return AccuracyReport(
        config_name=config_name,
        tolerance=suite.tolerance,
        outcomes=tuple(outcomes),
        true_positives=tuple(true_positives),
        false_positives=tuple(false_positives),
        repeats=tuple(repeats),
        clean_window_violations=tuple(violations),
        clean_windows=clean_windows,
        clean_window_rate_false_positives=len(rate_violations),
    )


# --- the floors ------------------------------------------------------------
@dataclass(frozen=True)
class AccuracyFloors:
    """What the project promises about accuracy, as data a build can fail on.

    Each floor is measured, not hoped for, and each is stated with a margin below the current
    measurement so that the gate fails on a change in behaviour rather than on a rounding
    error. The numbers and the reasoning for each one are in ``docs/detection.md``; changing
    one here means changing it there in the same commit.

    There is deliberately no recall floor for a rule whose labels are all ``held``: the
    shipped fixture's sustained-high episodes are attributed to activity by policy, so the
    floor that protects that rule is ``held_labels_unaccounted_max`` - the engine must be able
    to say why it stayed quiet. Re-label one of those episodes ``alert`` and
    ``overall_recall_min`` starts covering it.
    """

    overall_recall_min: float = 0.90
    irregular_recall_min: float = 0.90
    low_recall_min: float = 0.90
    precision_min: float = 0.90
    clean_window_false_positives_max: int = 0
    held_labels_unaccounted_max: int = 0
    confidence_ranking_min: float = 0.75
    require_best_is_true: bool = True

    def checks(self, report: AccuracyReport) -> list[tuple[str, str, bool]]:
        """``(what is measured, the number, whether it clears its floor)``, in report order."""
        irregular = report.recall_for(FINDING_IRREGULAR)
        low = report.recall_for(FINDING_SUSTAINED_LOW)
        high = report.recall_for(FINDING_SUSTAINED_HIGH)
        checks: list[tuple[str, str, bool]] = [
            (
                "recall (irregular labels)",
                _pct(irregular),
                irregular is None or irregular >= self.irregular_recall_min,
            ),
            (
                "recall (sustained_low labels)",
                _pct(low),
                low is None or low >= self.low_recall_min,
            ),
            (
                "recall (sustained_high labels)",
                _pct(high),
                high is None or high >= self.overall_recall_min,
            ),
            ("recall (all alert labels)", _pct(report.recall), report.recall >= self.overall_recall_min),
            ("precision", _pct(report.precision), report.precision >= self.precision_min),
            (
                "clean-window rate false positives",
                str(report.clean_window_rate_false_positives),
                report.clean_window_rate_false_positives <= self.clean_window_false_positives_max,
            ),
            (
                "held labels unaccounted",
                str(report.held_shortfall),
                report.held_shortfall <= self.held_labels_unaccounted_max,
            ),
            (
                "confidence ranking (true>false)",
                f"{report.confidence_ranking:.3f}",
                report.confidence_ranking >= self.confidence_ranking_min,
            ),
            (
                "loudest finding is a labelled one",
                "yes" if report.best_is_true else "no",
                report.best_is_true or not self.require_best_is_true,
            ),
        ]
        return checks

    def failures(self, report: AccuracyReport) -> list[str]:
        """The floors this report fell through, in words - empty means the gate passes."""
        return [
            f"{name} {measured} is below/outside its floor"
            for name, measured, ok in self.checks(report)
            if not ok
        ]

    def summary(self, report: AccuracyReport) -> list[str]:
        """One aligned line per floor, each ending in ``ok`` or ``FAIL``."""
        floors = {
            "recall (irregular labels)": f">= {self.irregular_recall_min:.2f}",
            "recall (sustained_low labels)": f">= {self.low_recall_min:.2f}",
            "recall (sustained_high labels)": f">= {self.overall_recall_min:.2f}",
            "recall (all alert labels)": f">= {self.overall_recall_min:.2f}",
            "precision": f">= {self.precision_min:.2f}",
            "clean-window rate false positives": f"<= {self.clean_window_false_positives_max}",
            "held labels unaccounted": f"<= {self.held_labels_unaccounted_max}",
            "confidence ranking (true>false)": f">= {self.confidence_ranking_min:.2f}",
            "loudest finding is a labelled one": "required",
        }
        return [
            f"{name:<36} {measured:>10}   floor {floors[name]:>8}   {'ok' if ok else 'FAIL'}"
            for name, measured, ok in self.checks(report)
        ]


#: The floors the shipped plugin is held to on every CI run.
SHIPPED_FLOORS = AccuracyFloors()


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def format_report(report: AccuracyReport, floors: AccuracyFloors = SHIPPED_FLOORS) -> str:
    """The whole gate's answer in one block: the matrix, then every floor."""
    return "\n".join([report.confusion_matrix(), "", *floors.summary(report)])


__all__ = [
    "CLEAR_WINDOW_RULES",
    "DEFAULT_TOLERANCE",
    "FORMAT_VERSION",
    "MIN_NIGHT_MINUTES",
    "SHIPPED_FLOORS",
    "STATUS_ACCOUNTED",
    "STATUS_KEPT_SILENT",
    "STATUS_MISSED",
    "STATUS_REPORTED",
    "STATUS_REPORTED_ANYWAY",
    "STATUS_UNACCOUNTED",
    "STATUS_VIOLATED",
    "VERDICT_ALERT",
    "VERDICT_CLEAR",
    "VERDICT_HELD",
    "VERDICTS",
    "AccuracyFloors",
    "AccuracyReport",
    "BuiltCorpus",
    "EpisodeLabel",
    "LabelledCorpus",
    "LabelledSuite",
    "LabelOutcome",
    "RuleCounts",
    "WindowLabel",
    "expected_verdict_for_episode",
    "format_report",
    "label_dev_seed",
    "rank_confidences",
    "score_suite",
]
