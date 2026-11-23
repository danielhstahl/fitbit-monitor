"""The engine end to end: hand-crafted days in, typed findings out, every gate named.

Every case here is a store full of plateau days built by ``anomaly_support`` plus one
deliberate abnormality, and the assertion is the whole point of the ticket: the rule that
should fire fires with a duration, a confidence and an evidence series, and the near-miss
next to it does not - or is reported as held back, with the bar it missed.

The fixtures stay flat (no jitter) unless a case is specifically about a measured spread,
because a flat day gives the engine a measured spread of zero and so exercises the floor
constants that keep a degenerate spread from firing on nothing.
"""

from __future__ import annotations

import json
from datetime import timedelta

from fitbit_monitor.anomaly import AnomalyConfig, detect
from fitbit_monitor.anomaly.models import (
    FINDING_IRREGULAR,
    FINDING_SUSTAINED_HIGH,
    FINDING_SUSTAINED_LOW,
)
from hr_support import HrStoreTestCase

from anomaly_support import (
    HIGH_EPISODE,
    LOW_EPISODE,
    QUIET_NIGHT,
    START,
    day_points,
    irregular_pattern,
    scenario_points,
    span_of,
)

#: Five days is enough to build a provisional baseline (>= provisional_min_days) and not
#: enough to establish one (min_baseline_days is 7), which is what most of these cases want:
#: a finding exists, and its confidence is capped because of it.
DAYS = 5


class EngineRunTestCase(HrStoreTestCase):
    """Store helpers over a hand-built scenario."""

    days = DAYS
    episodes: list = []
    config_overrides: dict = {}
    step_minutes = 1
    #: Real readings wobble. A perfectly flat fixture gives every robust spread in the engine
    #: a measured value of zero, and a rule calibrated on a spread of zero is a rule that has
    #: never been asked to work; a bpm or two of wobble is what a Fitbit actually reports
    #: while a wearer sits still. Cases that are specifically about a degenerate spread say so
    #: by putting their own jitter-free scenario.
    jitter = 1.5

    def setUp(self) -> None:
        super().setUp()
        points = scenario_points(
            self.days,
            step_minutes=self.step_minutes,
            jitter=self.jitter,
            episodes=self.episodes,
        )
        self.put(points)
        self.since, self.until = span_of(points)
        self.result = self.detect_run()

    def detect_run(self, **overrides):
        config = AnomalyConfig(**{**self.config_overrides, **overrides})
        return detect(self.reader(), self.since, self.until, config=config)

    def findings_of(self, kind: str):
        return [finding for finding in self.result.findings if finding.type == kind]

    def assertNoFindings(self, *, include_suppressed: bool = False) -> None:
        self.assertEqual([finding.type for finding in self.result.findings], [])
        if include_suppressed:
            self.assertEqual([episode.type for episode in self.result.suppressed], [])

    def assertBaselineCounts(self, established: bool) -> None:
        for context, baseline in self.result.baselines.items():
            self.assertTrue(
                baseline.usable, f"{context} baseline should be usable: {baseline.reason}"
            )
            self.assertEqual(baseline.established, established, context)


class TestEstablishedBaseline(EngineRunTestCase):
    """Seven usable resting days is the line between provisional and established."""

    days = 9

    def test_a_nightly_history_builds_an_established_personal_baseline(self) -> None:
        for context in ("rest", "sleep"):
            self.assertIn(context, self.result.baselines)
        self.assertBaselineCounts(established=True)
        rest = self.result.baselines["rest"]
        # The resting window is the quietest sustained stretch of the awake plateau, so the
        # personal level *is* that plateau - not the night, and not the whole day's mean. A
        # tolerance of half a bpm because the window's edge is allowed to grow a few minutes
        # into the evening's descent, and those readings are honestly part of what it saw.
        self.assertAlmostEqual(rest.level_bpm, 75.0, delta=0.5)
        self.assertEqual(rest.days, 9)
        self.assertIsNone(rest.reason)

    def test_contexts_can_be_narrowed_to_one(self) -> None:
        result = self.detect_run(contexts=["sleep"])
        self.assertEqual(list(result.baselines), ["sleep"])
        self.assertNotIn("rest", result.scatter_baselines)


class TestNotEnoughHistory(EngineRunTestCase):
    """Two days is not a baseline, and the engine says so instead of guessing."""

    days = 2
    episodes = [(1, (10, 11, HIGH_EPISODE))]

    def test_the_rules_do_not_run_and_the_run_says_which_and_why(self) -> None:
        self.assertNoFindings()
        rules = {(note.rule, note.context) for note in self.result.notes}
        self.assertIn((FINDING_SUSTAINED_HIGH, "rest"), rules)
        self.assertIn((FINDING_SUSTAINED_LOW, "rest"), rules)
        self.assertIn((FINDING_IRREGULAR, "rest"), rules)
        for note in self.result.notes:
            self.assertIn("need", note.reason)

    def test_no_baseline_is_reported_in_the_result_document(self) -> None:
        document = self.result.as_dict()
        self.assertEqual(document["notes"][0]["rule"], FINDING_SUSTAINED_HIGH)
        json.dumps(document)  # the plugin's --json output has to be serialisable


class TestProvisionalBaseline(EngineRunTestCase):
    """A finding from a provisional baseline is allowed, and capped."""

    episodes = [(2, (10.0, 11.0, HIGH_EPISODE))]

    def test_the_finding_fires_with_provisional_confidence(self) -> None:
        findings = self.findings_of(FINDING_SUSTAINED_HIGH)
        self.assertEqual(len(findings), 1, [f.as_dict() for f in self.result.findings])
        finding = findings[0]
        self.assertFalse(finding.baseline_established)
        self.assertEqual(finding.baseline_days, DAYS)
        self.assertLessEqual(finding.confidence, AnomalyConfig().provisional_confidence_cap)
        self.assertEqual(finding.detail["confidence"]["provisional_cap"], 0.55)
        self.assertLess(finding.confidence, 1.0)

    def test_the_same_episode_from_an_established_baseline_is_more_confident(self) -> None:
        longer = scenario_points(9, episodes=[(6, (10.0, 11.0, HIGH_EPISODE))])
        self.put(longer)
        since, until = span_of(longer)
        result = detect(self.reader(), since, until, config=AnomalyConfig())
        matches = [
            finding
            for finding in result.findings
            if finding.type == FINDING_SUSTAINED_HIGH
            and finding.onset.date() == (START + timedelta(days=6)).date()
        ]
        self.assertEqual(len(matches), 1)
        self.assertTrue(matches[0].baseline_established)
        self.assertGreater(
            matches[0].confidence, self.findings_of(FINDING_SUSTAINED_HIGH)[0].confidence
        )


class TestSustainedHigh(EngineRunTestCase):
    """The sustained-high rule: fires on a sustained elevation, not on a spike."""

    episodes = [(2, (10.0, 11.0, HIGH_EPISODE))]

    def test_a_sustained_elevation_above_the_personal_level_fires(self) -> None:
        findings = self.findings_of(FINDING_SUSTAINED_HIGH)
        self.assertEqual(len(findings), 1)
        finding = findings[0]
        self.assertEqual(finding.context, "rest")
        self.assertEqual(finding.onset, START + timedelta(days=2, hours=10))
        self.assertEqual(finding.duration_minutes, 60.0)
        self.assertAlmostEqual(finding.observed, HIGH_EPISODE, delta=2.0)
        self.assertGreater(finding.threshold, 75.0)
        self.assertLess(finding.threshold, HIGH_EPISODE)
        self.assertEqual(finding.unit, "bpm")
        self.assertEqual(finding.heuristic, "personal-baseline-sustained-rate")
        self.assertEqual(finding.anchor, (START + timedelta(days=2)).date().isoformat())

    def test_the_finding_carries_its_evidence_series(self) -> None:
        finding = self.findings_of(FINDING_SUSTAINED_HIGH)[0]
        self.assertGreater(len(finding.values), 50)
        stamps = [item.at for item in finding.evidence]
        self.assertEqual(stamps, sorted(stamps))
        self.assertLessEqual(len(finding.evidence), AnomalyConfig().max_evidence_points)
        self.assertTrue(any(item.detail for item in finding.evidence), "the peak should be marked")
        peak = max(item.value for item in finding.evidence)
        self.assertAlmostEqual(peak, finding.observed, delta=2.0)

    def test_a_single_spike_does_not_fire_and_says_which_bar_it_missed(self) -> None:
        # Twelve minutes of 105 bpm: long enough to clear the bar the smoother looks at, too
        # short to be a finding. The point of the case is the second half - the engine says
        # which bar it missed instead of silently dropping it.
        points = scenario_points(
            DAYS, episodes=[(2, (10.0, 10.2, HIGH_EPISODE))], jitter=self.jitter
        )
        self.put(points)
        since, until = span_of(points)
        result = detect(self.reader(), since, until, config=AnomalyConfig())
        self.assertEqual([f.type for f in result.findings], [])
        held = [episode for episode in result.suppressed if episode.type == FINDING_SUSTAINED_HIGH]
        self.assertEqual(len(held), 1, [episode.as_dict() for episode in result.suppressed])
        self.assertIn("minimum duration", held[0].reason)
        self.assertLess(held[0].duration_minutes, AnomalyConfig().min_sustained_minutes)

    def test_an_elevation_inside_the_personal_spread_does_not_fire_at_all(self) -> None:
        points = scenario_points(DAYS, episodes=[(2, (10.0, 11.0, 80.0))])
        self.put(points)
        since, until = span_of(points)
        result = detect(self.reader(), since, until, config=AnomalyConfig())
        self.assertEqual([f.type for f in result.findings], [])
        self.assertEqual(
            [e.type for e in result.suppressed if e.type == FINDING_SUSTAINED_HIGH], []
        )

    def test_the_threshold_comes_from_config_not_from_code(self) -> None:
        # Same 80 bpm stretch, one notch of config: the bar moves and the finding appears.
        points = scenario_points(DAYS, episodes=[(2, (10.0, 11.0, 80.0))])
        self.put(points)
        since, until = span_of(points)
        loosened = detect(
            self.reader(),
            since,
            until,
            config=AnomalyConfig(min_high_delta_bpm=1.0, z_high=1.0),
        )
        self.assertEqual([f.type for f in loosened.findings], [FINDING_SUSTAINED_HIGH])

    def test_the_minimum_duration_is_configurable_too(self) -> None:
        points = scenario_points(DAYS, episodes=[(2, (10.0, 10.25, HIGH_EPISODE))])
        self.put(points)
        since, until = span_of(points)
        store = self.reader()
        strict = detect(store, since, until, config=AnomalyConfig())
        self.assertEqual([f.type for f in strict.findings], [])
        lenient = detect(store, since, until, config=AnomalyConfig(min_sustained_minutes=15.0))
        self.assertEqual([f.type for f in lenient.findings], [FINDING_SUSTAINED_HIGH])


class TestSustainedLow(EngineRunTestCase):
    """The sustained-low rule, in the context where a low rate is meaningful."""

    episodes = [(3, (1.0, 2.0, LOW_EPISODE))]

    def test_a_sustained_low_rate_overnight_fires_in_the_sleep_context(self) -> None:
        findings = self.findings_of(FINDING_SUSTAINED_LOW)
        self.assertEqual(len(findings), 1, [f.as_dict() for f in self.result.findings])
        finding = findings[0]
        self.assertEqual(finding.context, "sleep")
        self.assertEqual(finding.onset.date(), (START + timedelta(days=3)).date())
        # Not exactly the 60 minutes that were inserted: the low stretch becomes the seed of
        # the night's window and the window is grown from there, so the reported episode is
        # the part of it the night's own window kept. Longer than the minimum duration is the
        # assertion that matters; the exact edge belongs to the window library, not this rule.
        self.assertGreaterEqual(finding.duration_minutes, AnomalyConfig().min_sustained_minutes)
        self.assertAlmostEqual(finding.observed, LOW_EPISODE, delta=2.0)
        self.assertLess(finding.threshold, QUIET_NIGHT)
        self.assertGreater(finding.confidence, 0.5)

    def test_the_same_stretch_is_not_reported_twice_as_a_daytime_finding(self) -> None:
        # The night leaked into the resting domain as well (its window was hijacked by the very
        # episode being looked for), and the engine hands it back to the night instead of
        # reporting a seven-hour daytime bradycardia. See AnomalyConfig.nocturnal_low_margin_bpm.
        held = [
            episode
            for episode in self.result.suppressed
            if episode.type == FINDING_SUSTAINED_LOW and episode.context == "rest"
        ]
        attributed = [episode for episode in held if "night" in episode.reason]
        self.assertTrue(attributed, [episode.reason for episode in held])
        self.assertLessEqual(
            attributed[0].mean, QUIET_NIGHT + AnomalyConfig().nocturnal_low_margin_bpm
        )

    def test_the_same_night_is_not_also_reported_as_a_daytime_bradycardia(self) -> None:
        # A long stretch of low rate overnight is the quietest thing about that night, so it
        # becomes the seed of the night's window and the rest of the night falls outside every
        # window - into the resting domain, where 55 bpm against a 75 bpm resting level would
        # read as a seven-hour bradycardia. The engine attributes it to the night and says so.
        held = [
            episode
            for episode in self.result.suppressed
            if episode.type == FINDING_SUSTAINED_LOW and episode.context == "rest"
        ]
        nocturnal = [episode for episode in held if "night" in episode.reason]
        self.assertTrue(nocturnal, [episode.reason for episode in held])
        self.assertLessEqual(
            nocturnal[0].mean, QUIET_NIGHT + AnomalyConfig().nocturnal_low_margin_bpm
        )

    def test_the_night_being_the_night_does_not_fire_it(self) -> None:
        ordinary = detect(
            self.reader(), self.since, self.until, config=AnomalyConfig(provisional_min_days=2)
        )
        clean = scenario_points(DAYS, jitter=self.jitter)
        self.put(clean)
        since, until = span_of(clean)
        result = detect(self.reader(), since, until, config=AnomalyConfig())
        self.assertEqual([f.type for f in result.findings], [])
        self.assertTrue(ordinary.findings)  # the episode above did fire; this one did not


class TestIrregularRhythm(EngineRunTestCase):
    """The scatter rule: an enormous interval-to-interval scatter, personal and rated."""

    episodes = [(2, (1.0, 2.0, irregular_pattern()))]
    #: Deliberately flat apart from the episode. The scatter index is measured on intervals
    #: derived from a coarse epoch, so a jittered fixture raises the *baseline* scatter of
    #: every quiet stretch by about as much as it raises the episode's, and the case stops
    #: being about the rule. The measurement on real corpus data is where the quantisation
    #: actually gets exercised (docs/detection.md records what it did to the threshold).
    jitter = 0.0

    def test_an_alternating_rhythm_over_a_quiet_night_fires(self) -> None:
        findings = self.findings_of(FINDING_IRREGULAR)
        self.assertTrue(findings, [f.as_dict() for f in self.result.findings])
        finding = findings[0]
        self.assertEqual(finding.heuristic, "personal-baseline-scatter-index")
        self.assertGreater(finding.observed, finding.threshold)
        self.assertIn("dimensionless", finding.unit)
        self.assertGreaterEqual(finding.duration_minutes, AnomalyConfig().irregularity_min_minutes)
        self.assertGreaterEqual(
            finding.detail["scatter_points"], AnomalyConfig().irregularity_min_blocks
        )
        # The evidence is the scatter series itself, oldest first: an auditor can see what the
        # finding was made of rather than only the number it was summarised into.
        self.assertTrue(finding.evidence, "the scatter series is the evidence")
        stamps = [item.at for item in finding.evidence]
        self.assertEqual(stamps, sorted(stamps))
        # Which context reports it depends on where the episode left the night's window: a
        # swing this wide truncates the window, and whatever the window no longer covers is
        # resting-domain by the library's own contract. See anomaly.context._split_domain.
        self.assertIn(finding.context, ("sleep", "rest"))
        self.assertAlmostEqual(finding.threshold, AnomalyConfig().irregularity_floor, delta=0.2)

    def test_a_steady_night_does_not_fire_it(self) -> None:
        clean = scenario_points(DAYS, jitter=self.jitter)
        self.put(clean)
        since, until = span_of(clean)
        result = detect(self.reader(), since, until, config=AnomalyConfig())
        self.assertEqual([f.type for f in result.findings], [])

    def test_the_floor_protects_a_wearer_whose_own_scatter_is_tiny(self) -> None:
        # A wearer whose every window measures exactly zero scatter: the personal part of the
        # threshold is meaningless, so the absolute floor alone is what holds the rule.
        flat = scenario_points(DAYS, jitter=0.0)
        self.put(flat)
        since, until = span_of(flat)
        result = detect(self.reader(), since, until, config=AnomalyConfig())
        baseline = result.scatter_baselines["sleep"]
        self.assertTrue(baseline.usable)
        self.assertAlmostEqual(baseline.median_index, 0.0, places=3)
        self.assertEqual(baseline.threshold, AnomalyConfig().irregularity_floor)


class TestExerciseAttribution(EngineRunTestCase):
    """A stretch inside an exercise span is attributed to activity - and says so."""

    episodes = [(2, (13.0, 14.0, 150.0))]

    def test_a_sustained_high_that_looks_like_a_workout_is_attributed_not_reported(self) -> None:
        # Heart rate alone cannot tell a workout from a sustained elevated rate; the engine
        # subtracts the exercise mask in the awake context and records what it set aside,
        # rather than leaving the reader to wonder whether it was blind or just quiet.
        self.assertEqual(self.findings_of(FINDING_SUSTAINED_HIGH), [])
        held = [
            episode
            for episode in self.result.suppressed
            if episode.type == FINDING_SUSTAINED_HIGH and "activity" in episode.reason
        ]
        self.assertEqual(len(held), 1, [episode.as_dict() for episode in self.result.suppressed])
        self.assertEqual(held[0].context, "rest")
        self.assertTrue(held[0].detail["exercise_overlap"])
        self.assertGreaterEqual(held[0].duration_minutes, AnomalyConfig().min_sustained_minutes)
        self.assertGreater(held[0].observed, held[0].threshold)

    def test_the_run_says_in_words_that_it_attributed_something(self) -> None:
        notes = [note for note in self.result.notes if "activity" in note.reason]
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].context, "rest")
        self.assertIn("exercise span", notes[0].reason)


class TestExerciseAttributionAcrossTwoSpans(EngineRunTestCase):
    """Two workouts in one day are two attributions - not one unreportable megafinding.

    The day's masked readings are the concatenation of every span the exercise mask took out,
    and thresholding that concatenation as one stretch lets the centred smoother average the
    last reading of the first span with the first reading of the second: the "episode" then
    spans the quiet hours between them, its coverage collapses, and it fails the coverage gate
    - which in the engine's attribution pass meant it was dropped silently, so a wearer could
    have a sustained elevation set aside by the mask and the run would report neither a
    finding nor a suppression. One probe per contiguous stretch, split at the engine's own
    merge gap, and both attributions appear.
    """

    episodes = [(2, (9.0, 10.0, 150.0)), (2, (13.0, 14.0, 150.0))]
    #: The corpus's grain, and the one this bug needs: at a 1-minute grain the mask's cooldown
    #: leaves enough quiet readings between the spans to break the smoothed run on its own.
    step_minutes = 5

    def attributed(self):
        return [
            episode
            for episode in self.result.suppressed
            if episode.type == FINDING_SUSTAINED_HIGH and "activity" in episode.reason
        ]

    def test_each_exercise_span_is_attributed_on_its_own(self) -> None:
        self.assertEqual(self.findings_of(FINDING_SUSTAINED_HIGH), [])
        held = self.attributed()
        self.assertEqual(len(held), 2, [episode.as_dict() for episode in self.result.suppressed])
        for episode in held:
            # Each stretch is measured on its own hour, not on the five hours between the
            # first reading of one and the last of the other.
            self.assertLessEqual(episode.duration_minutes, 70.0)
            self.assertGreaterEqual(episode.detail["coverage"], AnomalyConfig().min_coverage)

    def test_the_note_counts_both_stretches(self) -> None:
        notes = [note for note in self.result.notes if "activity" in note.reason]
        self.assertEqual(len(notes), 1)
        self.assertIn("2 sustained stretch", notes[0].reason)

    def test_the_split_is_at_the_merge_gap_and_nowhere_else(self) -> None:
        """The unit the fix is: contiguous means next to each other in time, not adjacent in a list."""
        from datetime import UTC, datetime

        from fitbit_monitor.anomaly.engine import _contiguous_runs
        from fitbit_monitor.hr.epochs import Sample

        base = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)

        def stamps(minutes):
            return tuple(Sample(base + timedelta(minutes=m), 140.0) for m in minutes)

        one_run = _contiguous_runs(
            stamps([0, 5, 10, 15, 20]), epoch_seconds=300.0, merge_gap_minutes=20.0
        )
        self.assertEqual([len(run) for run in one_run], [5])
        two_runs = _contiguous_runs(
            stamps([0, 5, 10, 60, 65, 70]), epoch_seconds=300.0, merge_gap_minutes=20.0
        )
        self.assertEqual([len(run) for run in two_runs], [3, 3])
        # A gap shorter than the merge gap stays one stretch: the rule's own merge would call
        # it one episode, and splitting it here would only make the probe myopic.
        still_one = _contiguous_runs(
            stamps([0, 5, 10, 25, 30]), epoch_seconds=300.0, merge_gap_minutes=20.0
        )
        self.assertEqual([len(run) for run in still_one], [5])
        self.assertEqual(_contiguous_runs((), epoch_seconds=300.0, merge_gap_minutes=20.0), [])


class TestEvidenceQuality(EngineRunTestCase):
    """A stretch that is mostly a hole in the data is not a finding."""

    episodes = [(2, (10.0, 11.0, HIGH_EPISODE))]

    def test_a_thin_episode_is_suppressed_with_its_coverage_stated(self) -> None:
        # A stretch where the device only reported every tenth minute: an hour of claimed
        # episode built from six readings, so the finding is mostly inference. The hole has to
        # be a hole in the *data*, not a dip in the rate - a dip is observed, and an episode
        # that dips and recovers is a legitimate sustained finding with a lower mean.
        day = START + timedelta(days=DAYS)
        low_edge = day + timedelta(hours=10)
        high_edge = day + timedelta(hours=11)
        dense = [
            point
            for point in day_points(day, jitter=self.jitter)
            if point[0] < low_edge or point[0] >= high_edge
        ]
        sparse = [(low_edge + timedelta(minutes=10 * step), HIGH_EPISODE) for step in range(6)]
        points = scenario_points(DAYS, jitter=self.jitter) + dense + sparse
        self.put(points)
        since, until = span_of(points)
        result = detect(self.reader(), since, until, config=AnomalyConfig())
        self.assertEqual(
            [f.type for f in result.findings], [], [f.as_dict() for f in result.findings]
        )
        held = [episode for episode in result.suppressed if episode.type == FINDING_SUSTAINED_HIGH]
        self.assertEqual(len(held), 1, [episode.as_dict() for episode in result.suppressed])
        self.assertIn("observed", held[0].reason)
        self.assertLess(held[0].detail["coverage"], AnomalyConfig().min_coverage)
        self.assertGreater(held[0].duration_minutes, AnomalyConfig().min_sustained_minutes)

    def test_a_dip_inside_an_episode_is_observed_not_a_hole(self) -> None:
        # The same shape, but the readings are there and a third of them are just normal: 14
        # minutes at 105, 21 minutes at rest, 14 more at 105. Each half on its own is shorter
        # than the minimum duration, so the question is whether the engine is allowed to call
        # the dip part of one stretch - a config decision, so the case names it.
        dipped = scenario_points(
            DAYS,
            jitter=self.jitter,
            episodes=[(2, (10.0, 10.233333, HIGH_EPISODE)), (2, (10.6, 10.833333, HIGH_EPISODE))],
        )
        self.put(dipped)
        since, until = span_of(dipped)
        store = self.reader()
        strict = detect(store, since, until, config=AnomalyConfig())
        self.assertEqual([f.type for f in strict.findings if f.type == FINDING_SUSTAINED_HIGH], [])
        bridged = detect(store, since, until, config=AnomalyConfig(merge_gap_minutes=40.0))
        high = [finding for finding in bridged.findings if finding.type == FINDING_SUSTAINED_HIGH]
        self.assertEqual(len(high), 1)
        self.assertEqual(high[0].coverage, 1.0)
        self.assertGreaterEqual(high[0].duration_minutes, 45.0)
