"""The engine: baselines, rules, merging, gates and confidence, in one pass over a range.

:func:`detect` is the whole library's front door. Give it a store, a half-open window and a
configuration and it answers with a
:class:`~fitbit_monitor.anomaly.models.DetectionResult`: the findings, the candidates it
held back, the rules that could not run, and the baselines everything was judged against.
It reads the range **once** (:func:`fitbit_monitor.hr.daily.day_features` is handed the
already-read series) and does no writing of any kind, so a caller can run it over ninety
days inside one plugin's wall-clock budget.

The pipeline, in the order it runs
----------------------------------
``read``
    One :class:`~fitbit_monitor.hr.epochs.EpochSeries` for the range, then one
    :class:`~fitbit_monitor.hr.daily.DayFeatures` per local day - windows, exercise mask
    and HRV included, from the feature library, with no second read.
``select``
    Per context, the in-context samples of each day
    (:mod:`fitbit_monitor.anomaly.context`): the resting window minus exercise, or the
    night.
``baseline``
    Per context, a :class:`~fitbit_monitor.anomaly.baseline.RateBaseline` from the wearer's
    own per-day resting levels and a
    :class:`~fitbit_monitor.anomaly.baseline.ScatterBaseline` from their own scatter blocks
    (:mod:`fitbit_monitor.anomaly.baseline`). Not usable yet is a reported outcome, not a
    silent skip.
``rules``
    :mod:`fitbit_monitor.anomaly.rules` produces candidate episodes per day - sustained
    high, sustained low, irregular - against those personal thresholds.
``merge``
    Candidates of one kind closer together than ``merge_gap_minutes`` become one episode,
    across day boundaries too. This is where "one overnight episode is one finding" is
    decided, and it happens *before* the duration gate so that two stretches the data
    happened to break apart are not both discarded for being short.
``gate``
    A merged episode must clear three stated bars - long enough (``min_sustained_minutes``,
    or ``irregularity_min_minutes`` for the rhythm rule), built from enough readings
    (``min_finding_samples``), and observed often enough (``min_coverage``). Anything that
    crosses a threshold and then fails a bar is reported as a
    :class:`~fitbit_monitor.anomaly.models.SuppressedEpisode` with the reason, so a plugin
    can show *why* it stayed quiet.
``score``
    :func:`confidence_for` turns the episode and the baseline that judged it into a number
    in ``0..1`` - see below.

Confidence, and what it is not
------------------------------
Four factors multiplied together, all of them printed in the finding's ``detail`` so a
reader can redo the arithmetic:

``coverage``
    How much of the claimed stretch was actually observed. A finding that mostly came from
    a sensor that had stopped is worth less than one backed reading by reading.
``margin``
    How far past the threshold the episode went, measured in units of *the excess the
    threshold itself required*: 1.0 is twice the excess required, and a finding that
    scraped past its threshold gets less than one that doubled it. Personal by construction,
    because both the excess and the requirement came from the wearer's own spread.
``baseline``
    Baseline maturity: 1.0 once the wearer has supplied ``min_baseline_days`` usable resting
    days, and ``days / min_baseline_days`` before that. In addition, and as a hard rule
    rather than a factor, nothing produced from a provisional baseline may exceed
    ``provisional_confidence_cap`` - the acceptance criterion "refuse to claim high
    confidence before the baseline is established", implemented as a cap rather than as a
    comment.
``ceiling``
    ``confidence_ceiling`` (0.95 by default) multiplies everything, so the engine can never
    report certainty about a person's body from a wrist sensor.

What confidence is *not*: it is not a probability of disease, and it is not comparable
between wearers (two findings at 0.6 can be 5 bpm and 40 bpm past their own thresholds). It
is a rank order inside one wearer's data, which is exactly what a notification threshold and
a labelled accuracy gate need and nothing more.

Deduplication and cooldown
--------------------------
The engine merges its own episodes and stops there. Every finding carries a stable
:attr:`~fitbit_monitor.anomaly.models.Finding.dedup_key` (rule, context and the local day
the episode began on) and an ``occurred_at`` of its onset, which is all the Epic 5 sink
rules need to do their job: :class:`~fitbit_monitor.plugins.sinks.AlertDispatcher` hands it
to :meth:`fitbit_monitor.store.alerts.AlertHistory.record`, which decides - by cooldown, and
by nothing else - whether this is a new alert, a repeat of one it already delivered, or a
replay of a range that was already processed. Deliberately, the engine keeps no notification
state of its own: it would be a second, always-disagreeing copy of the alert history, and a
detector that forgets what it has already said is worse than one that never decides.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Sequence

from ..hr.daily import DayFeatures, FeatureOptions, day_features, day_window
from ..hr.epochs import EpochSeries, Sample, epoch_series
from ..hr.util import day_sequence, local_date
from ..store.reader import ReadOnlyStore
from ..timeutil import resolve_timezone
from .baseline import RateBaseline, ScatterBaseline, build_rate_baseline, build_scatter_baseline
from .config import AnomalyConfig
from .context import context_days
from .models import (
    FINDING_IRREGULAR,
    FINDING_SUSTAINED_HIGH,
    FINDING_SUSTAINED_LOW,
    DetectionResult,
    Finding,
    RuleNote,
    SuppressedEpisode,
    sample_points,
)
from .rules import (
    DIRECTION_HIGH,
    DIRECTION_LOW,
    Episode,
    scatter_episodes,
    scatter_windows,
    sustained_rate_episodes,
)


@dataclass(frozen=True)
class Reference:
    """What a finding was compared against - the same shape whichever rule made it.

    A rate finding and a scatter finding are judged by different statistics but reported by
    the same fields, so the scoring below has one definition of "how far past the bar".
    """

    level: float
    threshold: float
    spread: float
    days: int
    established: bool
    confidence_factor: float

    @property
    def required_excess(self) -> float:
        """How far past the level the threshold itself was: the bar an episode had to clear."""
        return abs(self.threshold - self.level)


def _contiguous_runs(
    samples: Sequence[Sample], *, epoch_seconds: float, merge_gap_minutes: float
) -> list[tuple[Sample, ...]]:
    """Split masked readings into stretches that are actually next to each other in time.

    A day's exercise overlap is the concatenation of every span the mask took out of it, and
    those spans are usually hours apart. Thresholding the concatenation as though it were one
    stretch is wrong twice over: the centred smoother averages the last reading of one span
    with the first reading of the next and so *bridges* the hole, and the resulting
    "episode" then claims most of the day, fails its own coverage gate, and is dropped -
    which is how a 75-minute sustained elevation inside a workout-shaped span vanished from
    a run's output without a finding, without a suppression and without a word. One probe
    per stretch, and the split is at the engine's own definition of "two stretches rather
    than one": further apart than ``merge_gap_minutes``.
    """
    limit = max(float(epoch_seconds) * 2.0, float(merge_gap_minutes) * 60.0)
    runs: list[list[Sample]] = []
    previous: datetime | None = None
    for sample in samples:
        if previous is not None and (sample.ts - previous).total_seconds() > limit:
            runs.append([])
        runs[-1].append(sample) if runs else runs.append([sample])
        previous = sample.ts
    return [tuple(run) for run in runs if run]


def _night_like_reason(
    episode: Episode,
    night_level: float,
    night_bounds: Sequence[tuple[datetime, datetime]],
    config: AnomalyConfig,
) -> str | None:
    """Whether a low resting-context episode is really the night, and why that is not a finding.

    Two conditions, both of which have to hold. The stretch has to overlap the region one of
    the wearer's nights was searched in - a wall-clock fact about the day, not about the
    readings - and its mean has to be at or below the wearer's own sleeping level plus the
    configured margin. A stretch that is merely *inside* the night region but well above their
    sleeping rate is something else (fever, alcohol, a bad night) and stays reportable; a
    stretch at night level that happens to fall in the afternoon is not suppressed, because
    the afternoon is not where a night was searched.

    The reason this is worth a check at all: a long low stretch overnight is also the quietest
    thing about that night, so it becomes the seed of the sleep window and the *rest* of the
    night falls outside every window - into the resting domain, where a normal 55 bpm against a
    75 bpm resting level reads as a seven-hour bradycardia that never happened.
    """
    if not night_bounds:
        return None
    if episode.mean > night_level + config.nocturnal_low_margin_bpm:
        return None
    for start, stop in night_bounds:
        if episode.onset < stop and episode.offset > start:
            return (
                f"mean {episode.mean:.1f} bpm against the wearer's sleeping level of "
                f"{night_level:.1f} bpm, inside the region their night was searched in: "
                "attributed to the night, which the sleep context reports on"
            )
    return None


def _rate_reference(baseline: RateBaseline, threshold: float, spread: float) -> Reference:
    return Reference(
        level=float(baseline.level_bpm or 0.0),
        threshold=threshold,
        spread=spread,
        days=baseline.days,
        established=baseline.established,
        confidence_factor=baseline.confidence_factor,
    )


def _scatter_reference(baseline: ScatterBaseline, threshold: float) -> Reference:
    return Reference(
        level=float(baseline.median_index or 0.0),
        threshold=threshold,
        spread=baseline.sigma_index,
        days=baseline.days,
        established=baseline.established,
        confidence_factor=baseline.confidence_factor,
    )


def confidence_for(
    episode: Episode, reference: Reference, config: AnomalyConfig
) -> tuple[float, dict[str, float]]:
    """The finding's confidence, and every factor that went into it.

    See the module docstring. Returned with the factors because a number nobody can take
    apart is a number nobody should act on.
    """
    coverage = episode.coverage
    required = reference.required_excess
    margin = (
        episode.observed - reference.level
        if episode.type != FINDING_SUSTAINED_LOW
        else reference.level - episode.observed
    )
    if required > 0:
        margin_factor = max(0.0, min(1.0, margin / required / 2.0))
    else:  # pragma: no cover - a threshold equal to the level is rejected by config validation
        margin_factor = 1.0
    factors = {
        "coverage": round(coverage, 4),
        "margin": round(margin_factor, 4),
        "baseline": round(reference.confidence_factor, 4),
        "ceiling": config.confidence_ceiling,
    }
    confidence = config.confidence_ceiling * coverage * margin_factor * reference.confidence_factor
    if not reference.established:
        confidence = min(confidence, config.provisional_confidence_cap)
        factors["provisional_cap"] = config.provisional_confidence_cap
    return max(0.0, min(config.confidence_ceiling, confidence)), factors


def merge_episodes(episodes: Sequence[Episode], config: AnomalyConfig) -> list[Episode]:
    """One kind, one context, one episode per stretch of overlapping-or-nearby candidates.

    Threshold is part of the grouping key so two rules that happen to share a finding type
    but were judged against different baselines cannot be merged into each other.
    """
    grouped: dict[tuple[str, str, float], list[Episode]] = {}
    for episode in sorted(episodes, key=lambda item: item.onset):
        grouped.setdefault((episode.type, episode.context, round(episode.threshold, 3)), []).append(
            episode
        )
    gap = timedelta(minutes=config.merge_gap_minutes)
    out: list[Episode] = []
    for group in grouped.values():
        current = group[0]
        for following in group[1:]:
            if following.onset - current.offset <= gap:
                current = current.merged_with(following)
            else:
                out.append(current)
                current = following
        out.append(current)
    return sorted(out, key=lambda episode: (episode.onset, episode.type))


class AnomalyEngine:
    """One configuration, applied to as many ranges as you like.

    Cheap to build, safe to reuse: nothing is cached between runs, so a long-lived engine
    re-derives its baselines from whatever range it is pointed at.
    """

    def __init__(
        self,
        config: AnomalyConfig | None = None,
        *,
        options: FeatureOptions | None = None,
    ) -> None:
        self.config = config or AnomalyConfig()
        #: The feature library's options. Defaults to the config's zone, dataset and field;
        #: a plugin that already builds its own ``FeatureOptions`` passes it here so the
        #: engine reads exactly what the plugin would have read.
        self.options = options or self._default_options()

    def _default_options(self) -> FeatureOptions:
        tz = resolve_timezone(self.config.time_zone) if self.config.time_zone else None
        return FeatureOptions(tz=tz, dataset=self.config.dataset, field=self.config.field)

    @property
    def timezone(self):
        return self.options.tz

    # --- reading ------------------------------------------------------------
    def read(
        self, store: ReadOnlyStore, since: datetime, until: datetime
    ) -> tuple[list[DayFeatures], EpochSeries]:
        """The range as one series plus one :class:`DayFeatures` per local day, one read.

        The read reaches ``context_warmup_hours`` past both ends and the returned days are
        still only the days the caller asked about. That is not padding: a day whose previous
        night was never seen cannot have its resting window evaluated - the morning sleep is
        still sitting in it - so a scan that starts at midnight would otherwise read the
        night's 55 bpm as "awake resting time" and report a bradycardia that exists only
        because of where the window began. Context on both sides costs one wider query and
        makes the boundary days mean the same thing as the middle ones.
        """
        opts = self.options
        warmup = timedelta(hours=self.config.context_warmup_hours)
        whole = epoch_series(
            store,
            since - warmup,
            until + warmup,
            epoch_seconds=opts.epoch_seconds,
            tolerance=opts.tolerance,
            impute_seconds=opts.impute_seconds,
            min_plausible_bpm=opts.min_plausible_bpm,
            max_plausible_bpm=opts.max_plausible_bpm,
            dataset=opts.dataset,
            field=opts.field,
        )
        asked_for = set(_days_in_window(since, until, opts))
        days: list[DayFeatures] = []
        for day in _days_in_window(whole.since, whole.until, opts):
            if day not in asked_for:
                continue
            days.append(day_features(store, day, options=opts, series=whole, overnight=whole))
        return days, whole

    # --- running ------------------------------------------------------------
    def detect(self, store: ReadOnlyStore, since: datetime, until: datetime) -> DetectionResult:
        """Read the range and return everything the rules found in it."""
        days, whole = self.read(store, since, until)
        return self.detect_features(days, whole, window=(since, until))

    def detect_features(
        self,
        days: Sequence[DayFeatures],
        series: EpochSeries,
        *,
        window: tuple[datetime, datetime] | None = None,
    ) -> DetectionResult:
        """Run every rule over already-computed features. No store, no reads, no writes.

        ``window`` is the range the caller asked about, which the reported result is stamped
        with; when it is omitted the series' own range is used. A caller that read its own
        warm-up context passes the real window here so the result does not claim to cover
        hours it only read for context.
        """
        config = self.config
        tz = self.options.tz
        findings: list[Finding] = []
        suppressed: list[SuppressedEpisode] = []
        notes: list[RuleNote] = []
        rate_baselines: dict[str, RateBaseline] = {}
        scatter_baselines: dict[str, ScatterBaseline] = {}

        # The night's own level, needed to decide whether a low stretch belongs to the night
        # or to the day. Scanned first and reused below rather than computed twice: the resting
        # context usually runs before the sleeping one, and the question it has to answer - is
        # this low stretch the wearer's night, or their day? - cannot be answered without it.
        scans: dict[str, list] = {}
        if tuple(config.contexts) != ("sleep",):
            scans["sleep"] = context_days(days, series, "sleep", config)
        night_scan = scans.get("sleep", [])
        night_baseline = build_rate_baseline("sleep", night_scan, config) if night_scan else None
        night_level = (
            night_baseline.level_bpm
            if night_baseline is not None and night_baseline.usable
            else None
        )
        night_bounds: dict = {item.day: item.night_bounds for item in night_scan}

        for context in config.contexts:
            scanned = scans.get(context) or context_days(days, series, context, config)
            baseline = build_rate_baseline(context, scanned, config)
            rate_baselines[context] = baseline
            usable_days = [item for item in scanned if item.usable]

            if not baseline.usable:
                reason = baseline.reason or "no personal baseline"
                notes.append(RuleNote(FINDING_SUSTAINED_HIGH, context, reason))
                notes.append(RuleNote(FINDING_SUSTAINED_LOW, context, reason))
            else:
                for direction, threshold in (
                    (DIRECTION_HIGH, baseline.high_threshold_bpm),
                    (DIRECTION_LOW, baseline.low_threshold_bpm),
                ):
                    if threshold is None:  # pragma: no cover - guarded by baseline.usable
                        continue
                    candidates: list[Episode] = []
                    for item in usable_days:
                        if not item.has_domain:
                            continue
                        candidates.extend(
                            sustained_rate_episodes(
                                item, config, direction=direction, threshold=threshold
                            )
                        )
                    reference = _rate_reference(baseline, threshold, baseline.spread_for(direction))
                    found, held = self._collect(
                        candidates,
                        reference,
                        tz,
                        night=(night_level, night_bounds)
                        if direction == DIRECTION_LOW and context != "sleep"
                        else None,
                    )
                    findings.extend(found)
                    suppressed.extend(held)
                if context in config.exclude_exercise_in:
                    note, held = self._activity_note(usable_days, baseline, config)
                    if note is not None:
                        notes.append(note)
                    suppressed.extend(held)

            windows_by_day = [
                (item, scatter_windows(item, config)) for item in scanned if item.has_domain
            ]
            scatter = build_scatter_baseline(
                context,
                [(item.day, [point.index for point in points]) for item, points in windows_by_day],
                config,
            )
            scatter_baselines[context] = scatter
            threshold = scatter.threshold
            if threshold is None:
                notes.append(
                    RuleNote(
                        FINDING_IRREGULAR, context, scatter.reason or "no irregularity baseline"
                    )
                )
                continue
            reference = _scatter_reference(scatter, threshold)
            candidates = []
            for item, points in windows_by_day:
                candidates.extend(
                    scatter_episodes(item, points, threshold=threshold, config=config)
                )
            found, held = self._collect(candidates, reference, tz)
            findings.extend(found)
            suppressed.extend(held)

        findings.sort(key=lambda finding: (finding.onset, finding.type))
        suppressed.sort(key=lambda episode: (episode.onset, episode.type))
        since, until = window if window is not None else (series.since, series.until)
        # Nothing reported may start outside the window that was asked about, whatever the
        # caller handed in as `days`.
        findings = [finding for finding in findings if since <= finding.onset < until]
        suppressed = [episode for episode in suppressed if since <= episode.onset < until]
        return DetectionResult(
            since=since,
            until=until,
            config=config,
            findings=tuple(findings),
            suppressed=tuple(suppressed),
            notes=tuple(notes),
            baselines=rate_baselines,
            scatter_baselines=scatter_baselines,
            days_scanned=len(days),
            samples_scanned=len(series.measured_samples),
        )

    # --- one candidate's fate ----------------------------------------------
    def _activity_note(
        self, days: Sequence, baseline: RateBaseline, config: AnomalyConfig
    ) -> tuple[RuleNote | None, list[SuppressedEpisode]]:
        """Report - without reporting it - what the exercise exclusion set aside.

        Heart rate alone cannot tell a workout from a sustained elevated rate: both are a high
        rate that lasts. The engine's answer is to subtract the exercise mask in the awake
        context and say plainly how much it did that with. Each stretch that crossed the high
        threshold *inside* an exercise span is recorded as a suppressed episode with the
        reason, and one note counts them for the run summary, so a plugin can print "we saw N
        sustained stretches and attributed them to activity" - or show all N, with the rates -
        instead of leaving the reader to wonder whether the detector was blind or just quiet.

        This matters more than it looks on data with no workout records, which is most of what
        this store holds: the mask is then inferred from the rate itself, so the exercise
        heuristic and the sustained-high rule are competing for the same signal, and a run of
        145 bpm for an hour is claimed by whichever of the two is asked first.
        """
        threshold = baseline.high_threshold_bpm
        if threshold is None:
            return None, []
        stretches = 0
        longest = 0.0
        held: list[SuppressedEpisode] = []
        for item in days:
            for run in _contiguous_runs(
                item.exercise_overlap,
                epoch_seconds=item.epoch_seconds,
                merge_gap_minutes=config.merge_gap_minutes,
            ):
                probe = replace(item, detection_samples=run)
                episodes = sustained_rate_episodes(
                    probe, config, direction=DIRECTION_HIGH, threshold=threshold
                )
                for episode in merge_episodes(episodes, config):
                    reason = self._gate(episode)
                    if reason is not None:
                        continue
                    stretches += 1
                    longest = max(longest, episode.duration_minutes)
                    held.append(
                        SuppressedEpisode(
                            type=episode.type,
                            context=episode.context,
                            onset=episode.onset,
                            offset=episode.offset,
                            reason=(
                                f"{episode.observed:.0f} bpm sustained for {episode.duration_minutes:g} min "
                                "inside an exercise span: attributed to activity rather than reported"
                            ),
                            observed=episode.observed,
                            threshold=episode.threshold,
                            mean=episode.mean,
                            detail={
                                "readings": episode.reading_count,
                                "coverage": round(episode.coverage, 4),
                                "exercise_overlap": True,
                                **(episode.detail or {}),
                            },
                        )
                    )
        if not stretches:
            return None, []
        return (
            RuleNote(
                FINDING_SUSTAINED_HIGH,
                baseline.context,
                f"{stretches} sustained stretch(es) above the high threshold ({longest:g} min longest) "
                "fell inside an exercise span and were attributed to activity, not reported",
            ),
            held,
        )

    def _collect(
        self,
        candidates: Sequence[Episode],
        reference: Reference,
        tz,
        *,
        night: tuple[float | None, dict] | None = None,
    ) -> tuple[list[Finding], list[SuppressedEpisode]]:
        """Merge, gate and score: the three decisions that turn a candidate into a finding.

        ``night`` is the ``(sleeping level, bounds by day)`` pair the low rule uses to hand a
        nocturnal stretch to the sleep context instead of reporting it as a daytime one; see
        :attr:`~fitbit_monitor.anomaly.config.AnomalyConfig.nocturnal_low_margin_bpm`.
        """
        config = self.config
        findings: list[Finding] = []
        suppressed: list[SuppressedEpisode] = []
        for episode in merge_episodes(candidates, config):
            reason = self._gate(episode)
            if reason is None and night is not None and night[0] is not None:
                reason = _night_like_reason(
                    episode, night[0], night[1].get(episode.day, ()), config
                )
            if reason is not None:
                suppressed.append(
                    SuppressedEpisode(
                        type=episode.type,
                        context=episode.context,
                        onset=episode.onset,
                        offset=episode.offset,
                        reason=reason,
                        observed=episode.observed,
                        threshold=episode.threshold,
                        mean=episode.mean,
                        detail={
                            "readings": episode.reading_count,
                            "coverage": round(episode.coverage, 4),
                            "mean": round(episode.mean, 3),
                            **(episode.detail or {}),
                        },
                    )
                )
                continue
            findings.append(self._finding(episode, reference, tz))
        return findings, suppressed

    def _gate(self, episode: Episode) -> str | None:
        """The bar the episode has to clear after merging, or ``None`` when it cleared them."""
        config = self.config
        minimum = (
            config.irregularity_min_minutes
            if episode.type == FINDING_IRREGULAR
            else config.min_sustained_minutes
        )
        if episode.duration_minutes < minimum:
            return (
                f"sustained for {episode.duration_minutes:g} min, below the {minimum:g}-min "
                "minimum duration for a finding"
            )
        if episode.reading_count < config.min_finding_samples:
            return f"only {episode.reading_count} reading(s), need {config.min_finding_samples}"
        if episode.coverage < config.min_coverage:
            return (
                f"only {episode.coverage:.0%} of the stretch was observed, "
                f"below the {config.min_coverage:.0%} minimum"
            )
        return None

    def _finding(self, episode: Episode, reference: Reference, tz) -> Finding:
        config = self.config
        confidence, factors = confidence_for(episode, reference, config)
        keep = "min" if episode.type == FINDING_SUSTAINED_LOW else "max"
        note = {
            FINDING_SUSTAINED_HIGH: "peak rate of the episode",
            FINDING_SUSTAINED_LOW: "lowest rate of the episode",
            FINDING_IRREGULAR: "largest scatter block of the episode",
        }[episode.type]
        detail: dict[str, object] = {
            "confidence": factors,
            "reference_level": round(reference.level, 3),
            "reference_spread": round(reference.spread, 3),
            "required_excess": round(reference.required_excess, 3),
            "readings": episode.reading_count,
            **(episode.detail or {}),
        }
        return Finding(
            type=episode.type,
            context=episode.context,
            onset=episode.onset,
            offset=episode.offset,
            confidence=confidence,
            heuristic=episode.heuristic,
            observed=episode.observed,
            threshold=episode.threshold,
            unit=episode.unit,
            baseline_level=reference.level,
            baseline_spread=reference.spread,
            baseline_days=reference.days,
            baseline_established=reference.established,
            samples=episode.samples,
            coverage=episode.coverage,
            mean=episode.mean,
            values=tuple(value for _stamp, value in episode.points),
            evidence=sample_points(
                episode.points,
                limit=config.max_evidence_points,
                dataset=self.options.dataset,
                keep=keep,
                note=note,
            ),
            anchor=_anchor(episode, tz),
            detail=detail,
        )


def _anchor(episode: Episode, tz) -> str:
    """The local day the episode began on - the stable half of its dedup key."""
    return local_date(episode.onset, tz).isoformat()


def _days_in_window(since: datetime, until: datetime, opts: FeatureOptions) -> list:
    """Every local day the range touches, skipping days outside it.

    The same rule :func:`fitbit_monitor.hr.daily.daily_features` uses, applied to a series
    that was already read: a range that stops at 06:00 does not get a day row for the day
    that starts at midnight after it.
    """
    out = []
    for day in day_sequence(since, until, opts.tz):
        start, end = day_window(day, opts)
        if end <= since or start >= until:
            continue
        out.append(day)
    return out


def detect(
    store: ReadOnlyStore,
    since: datetime,
    until: datetime,
    *,
    config: AnomalyConfig | None = None,
    options: FeatureOptions | None = None,
) -> DetectionResult:
    """One-shot: build an engine from ``config`` and run it over ``[since, until)``.

    The convenience wrapper the plugin and the tests use; :class:`AnomalyEngine` is the same
    thing with the config kept around.
    """
    return AnomalyEngine(config, options=options).detect(store, since, until)


__all__ = [
    "AnomalyEngine",
    "Reference",
    "confidence_for",
    "detect",
    "merge_episodes",
]
