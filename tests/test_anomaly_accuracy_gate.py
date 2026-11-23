"""The accuracy gate: labelled fixtures, a confusion matrix, and the floors the build holds.

This is the ticket that decides whether the hr-anomaly detector works or merely sounds
convincing, so nothing here eyeballs a plot. The fixture in
``tests/fixtures/anomaly/hr_anomaly_labels.json`` says, for five synthetic wearers, which
episodes exist, which rule each one exercises and what the shipped policy owes each one - a
finding, or an explanation for not making one - plus every all-night window that contains
nothing and must therefore stay silent. This module runs the engine over that data with the
shipped defaults and fails the build if the result falls through a stated floor
(:class:`~fitbit_monitor.anomaly.accuracy.AccuracyFloors`).

Three things about how it is written:

* **The results are printed as a confusion matrix**, through
  :func:`~fitbit_monitor.anomaly.accuracy.format_report`, on the theory that a wall of
  percentages is how an accuracy suite gets ignored. Run this file alone to see it::

      .venv/bin/python -m pytest tests/test_anomaly_accuracy_gate.py -q -s -k matrix

* **The floors are stated with a margin.** The current measurement is 1.000 recall on every
  alertable label and 1.000 precision; the floors sit at 0.90. That gap is deliberate: a
  floor set at last Tuesday's measurement fails on noise and gets turned off, and a floor
  that fails only when a rule stops working is a floor you can keep.
* **A gate that cannot fail is theatre**, so half of this file is the scorer being shown a
  wrong answer and proving it notices: a missed episode, a silent suppression, a false alarm
  inside a clean night, a confidence score that ranks a false finding above a true one.

The corpus is regenerated from the seed in the fixture (same seed, same end date, same
bytes - see ``test_seed_corpus.py``), which is why the fixture is a few kilobytes of labels
rather than a megabyte of heart rate.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from fitbit_monitor.anomaly import (
    SHIPPED_FLOORS,
    VERDICT_ALERT,
    VERDICT_HELD,
    AccuracyFloors,
    AnomalyConfig,
    DetectionResult,
    FINDING_IRREGULAR,
    FINDING_SUSTAINED_HIGH,
    FINDING_SUSTAINED_LOW,
    Finding,
    LabelledSuite,
    SuppressedEpisode,
    detect,
    format_report,
    score_suite,
)
from fitbit_monitor.anomaly.accuracy import (
    STATUS_ACCOUNTED,
    STATUS_MISSED,
    STATUS_REPORTED,
    STATUS_REPORTED_ANYWAY,
    STATUS_UNACCOUNTED,
    STATUS_VIOLATED,
    EpisodeLabel,
    LabelledCorpus,
    RuleCounts,
    WindowLabel,
    expected_verdict_for_episode,
    rank_confidences,
)

FIXTURE = Path(__file__).parent / "fixtures" / "anomaly" / "hr_anomaly_labels.json"

#: Findings too far from a labelled window to be that episode. Same figure the fixture was
#: recorded with; it lives in the file, and this asserts the two agree.
ONSET = datetime(2026, 5, 9, 2, 15, tzinfo=UTC)

# --- one expensive fixture, shared by every test in this file --------------
_CACHE: dict[str, object] = {}


def suite() -> LabelledSuite:
    if "suite" not in _CACHE:
        _CACHE["suite"] = LabelledSuite.load(FIXTURE)
    return _CACHE["suite"]


def built():
    """Every corpus in the suite, seeded once into one temp directory."""
    if "built" not in _CACHE:
        tmp = TemporaryDirectory(prefix="fm-accuracy-")
        _CACHE["tmp"] = tmp
        _CACHE["built"] = suite().build(Path(tmp.name))
    return _CACHE["built"]


def results_for(config: AnomalyConfig, *, wearers: tuple[str, ...] | None = None) -> dict:
    """One detection run per wearer, cached per configuration."""
    key = "results:" + str(sorted(config.as_dict().items()))
    cached = _CACHE.get(key)
    if isinstance(cached, dict) and (wearers is None or set(wearers) <= set(cached)):
        return {name: cached[name] for name in (wearers or cached)}
    out: dict = {}
    for name, corpus in built().items():
        if wearers is not None and name not in wearers:
            continue
        plan = corpus.plan
        assert plan is not None, "the fixture's corpora are generated, so a plan always exists"
        since = datetime(plan.start.year, plan.start.month, plan.start.day, tzinfo=UTC)
        until = datetime(plan.end.year, plan.end.month, plan.end.day, tzinfo=UTC) + timedelta(days=1)
        out[name] = detect(corpus.store, since, until, config=config)
    merged = {**({k: v for k, v in (cached or {}).items()} if isinstance(cached, dict) else {}), **out}
    _CACHE[key] = merged
    return {name: merged[name] for name in (wearers or merged)}


def report(config: AnomalyConfig | None = None, *, wearers: tuple[str, ...] | None = None, name: str = "shipped defaults"):
    cfg = config or AnomalyConfig()
    return score_suite(
        suite() if wearers is None else _subset(wearers),
        results_for(cfg, wearers=wearers),
        config_name=name,
    )


def _subset(wearers: tuple[str, ...]) -> LabelledSuite:
    return LabelledSuite(
        corpora=tuple(c for c in suite().corpora if c.wearer in wearers),
        tolerance=suite().tolerance,
    )


class AccuracyGateTest(unittest.TestCase):
    """The acceptance criteria, one test each, plus the matrix printed for a human to read."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.report = report()
        cls.floors = SHIPPED_FLOORS

    def test_matrix(self) -> None:
        print("\n" + format_report(self.report, self.floors))
        self.assertTrue(self.report.findings or self.report.alert_labels)

    def test_every_floor_clears(self) -> None:
        failures = self.floors.failures(self.report)
        self.assertEqual(
            failures,
            [],
            f"the shipped defaults fell through a stated accuracy floor:\n{format_report(self.report, self.floors)}",
        )

    def test_zero_false_positives_on_clean_all_night_windows(self) -> None:
        """The criterion that is not a trade-off: a clean night stayed silent."""
        violating = [outcome for outcome in self.report.outcomes if outcome.status == STATUS_VIOLATED]
        self.assertEqual(
            self.report.clean_window_rate_false_positives,
            0,
            f"{len(violating)} clean night window(s) had a sustained-rate finding in them: "
            f"{[outcome.label_id for outcome in violating][:5]}",
        )
        self.assertGreater(self.report.clean_windows, 100, "the fixture should actually cover nights")

    def test_stated_recall_floor_on_labelled_irregular_episodes(self) -> None:
        recall = self.report.recall_for(FINDING_IRREGULAR)
        self.assertIsNotNone(recall, "the fixture has no irregular labels to score")
        self.assertGreaterEqual(recall, SHIPPED_FLOORS.irregular_recall_min)
        self.assertGreaterEqual(
            self.report.recall_for(FINDING_SUSTAINED_LOW), SHIPPED_FLOORS.low_recall_min
        )

    def test_stated_precision_floor(self) -> None:
        self.assertGreaterEqual(self.report.precision, SHIPPED_FLOORS.precision_min)
        self.assertGreaterEqual(self.report.recall, SHIPPED_FLOORS.overall_recall_min)

    def test_confidence_ranks_true_findings_above_false_ones(self) -> None:
        self.assertGreaterEqual(
            self.report.confidence_ranking, SHIPPED_FLOORS.confidence_ranking_min
        )
        self.assertTrue(self.report.best_is_true)

    def test_every_held_label_is_accounted_for(self) -> None:
        """Nothing the policy holds back may vanish: an explanation is required."""
        unaccounted = self.report.outcomes_with(STATUS_UNACCOUNTED, STATUS_REPORTED_ANYWAY)
        self.assertEqual(
            [outcome.label_id for outcome in unaccounted],
            [],
            "held labels must be suppressed with a reason, not silently dropped",
        )
        self.assertGreater(
            len(self.report.outcomes_with(STATUS_ACCOUNTED)), 0, "no held label was ever accounted for"
        )

    def test_a_false_positive_would_be_the_quietest_thing_in_the_run(self) -> None:
        """The margin, in confidence units, between what we report and what we should not have."""
        if not self.report.false_positives:
            self.skipTest("this measurement had no false positives to rank")
        worst_false = max(finding.confidence for finding in self.report.false_positives)
        best_true = min(finding.confidence for finding in self.report.true_positives)
        self.assertLess(worst_false, best_true)


class FixtureIntegrityTest(unittest.TestCase):
    """The answers have to be right before the score means anything."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.suite = suite()

    def test_the_fixture_covers_every_rule_and_plenty_of_clean_nights(self) -> None:
        types = {label.type for label in self.suite.episodes}
        self.assertEqual(types, {FINDING_SUSTAINED_HIGH, FINDING_SUSTAINED_LOW, FINDING_IRREGULAR})
        for name in (FINDING_SUSTAINED_LOW, FINDING_IRREGULAR):
            self.assertGreaterEqual(
                sum(1 for label in self.suite.episodes if label.type == name and label.verdict == VERDICT_ALERT),
                8,
                f"too few alertable {name} labels to state a recall floor",
            )
        self.assertGreaterEqual(len(self.suite.windows), 100)
        for corpus in self.suite.corpora:
            self.assertTrue(corpus.episodes, f"{corpus.wearer} has no episodes")
            self.assertTrue(corpus.is_synthetic)

    def test_no_clean_window_contains_a_labelled_episode(self) -> None:
        for corpus in self.suite.corpora:
            for window in corpus.windows:
                for label in corpus.episodes:
                    self.assertFalse(
                        window.overlaps(label.onset, label.offset),
                        f"{window.label_id} is labelled clear but contains {label.label_id}",
                    )

    def test_the_held_labels_are_the_ones_the_policy_says_to_hold(self) -> None:
        for label in self.suite.episodes:
            expected, _ = expected_verdict_for_episode(
                label.detail.get("kind", label.type), float(label.detail.get("mean_hr", 0.0)), 110.0
            )
            self.assertEqual(label.verdict, expected, label.label_id)
            if label.verdict == VERDICT_HELD:
                self.assertEqual(label.type, FINDING_SUSTAINED_HIGH)

    def test_the_checked_in_fixture_is_what_the_generator_still_produces(self) -> None:
        """One wearer re-derived from the corpus, to catch a silent drift in the fixture.

        The full re-derivation is ``python scripts/dev/record_accuracy_fixtures.py --check``;
        this checks one wearer so the suite notices a drift without paying to reseed and
        re-window the whole suite twice.
        """
        corpus = next(iter(built().values()))
        rederived = corpus.rederived_labels()
        self.assertEqual(
            [label.as_dict() for label in rederived.episodes],
            [label.as_dict() for label in corpus.corpus.episodes],
            "the labelled episodes no longer match what the dev-seed corpus produces",
        )
        self.assertEqual(
            [window.onset for window in rederived.windows],
            [window.onset for window in corpus.corpus.windows],
            "the clean night windows no longer match what the corpus's nights are",
        )


class ExerciseAttributionTradeoffTest(unittest.TestCase):
    """Why `exclude_exercise_in` ships as it does, measured rather than asserted in a comment.

    :attr:`~fitbit_monitor.anomaly.config.AnomalyConfig.exclude_exercise_in` is a knob with a
    documented alternative - subtract nothing in the awake context, and every sustained
    elevation gets reported - and the alternative is what a well-meaning contributor reaches
    for when someone complains that a sustained high went unreported. This is the measurement
    that says why it stays: the labelled episodes come back, and the run drowns in ordinary
    exercise.
    """

    def test_the_wide_net_trades_precision_for_those_recall_points(self) -> None:
        wearers = tuple(corpus.wearer for corpus in suite().corpora)[:2]
        shipped = report(wearers=wearers)
        wide = report(AnomalyConfig(exclude_exercise_in=[]), wearers=wearers, name="wide net")
        self.assertGreater(
            shipped.precision,
            wide.precision,
            "the wide-net configuration is supposed to be worse on precision, or this test is stale",
        )
        self.assertGreaterEqual(wide.false_positives_count, shipped.false_positives_count + 10)
        # ...and every held label becomes a policy breach under it, which is the point.
        self.assertEqual(
            len(shipped.outcomes_with(STATUS_REPORTED_ANYWAY)),
            0,
            "the shipped configuration reported something it is supposed to hold back",
        )


def make_finding(**overrides) -> Finding:
    """One finding, minimal but legal, for scorer tests that are about the scorer."""
    base: dict = {
        "type": FINDING_SUSTAINED_HIGH,
        "context": "rest",
        "onset": ONSET,
        "offset": ONSET + timedelta(minutes=45),
        "confidence": 0.7,
        "heuristic": "personal-baseline-sustained-rate",
        "observed": 130.0,
        "threshold": 95.0,
        "unit": "beats per minute",
        "baseline_level": 70.0,
        "baseline_spread": 6.0,
        "baseline_days": 21,
        "baseline_established": True,
        "samples": 10,
        "coverage": 1.0,
        "mean": 120.0,
        "anchor": "2026-05-09",
    }
    base.update(overrides)
    return Finding(**base)


def make_suppressed(**overrides) -> SuppressedEpisode:
    base: dict = {
        "type": FINDING_SUSTAINED_HIGH,
        "context": "rest",
        "onset": ONSET,
        "offset": ONSET + timedelta(minutes=40),
        "reason": "140 bpm sustained for 40 min inside an exercise span: attributed to activity",
        "observed": 140.0,
        "threshold": 95.0,
        "mean": 132.0,
    }
    base.update(overrides)
    return SuppressedEpisode(**base)


def hand_suite() -> LabelledSuite:
    """One wearer, four labels: a found episode, a missed one, a held one, a clean night."""
    wearer = "handmade"
    return LabelledSuite(
        corpora=(
            LabelledCorpus(
                wearer=wearer,
                source={"kind": "store", "store_path": "/dev/null"},
                episodes=(
                    EpisodeLabel(wearer, FINDING_SUSTAINED_HIGH, ONSET, ONSET + timedelta(minutes=40)),
                    EpisodeLabel(
                        wearer, FINDING_SUSTAINED_LOW, ONSET + timedelta(days=1), ONSET + timedelta(days=1, minutes=30)
                    ),
                    EpisodeLabel(
                        wearer, FINDING_IRREGULAR, ONSET + timedelta(days=2), ONSET + timedelta(days=2, minutes=20),
                        verdict=VERDICT_HELD,
                    ),
                ),
                windows=(
                    WindowLabel(
                        wearer, ONSET - timedelta(days=1), ONSET - timedelta(days=1) + timedelta(hours=6)
                    ),
                ),
            ),
        )
    )


def hand_result(**overrides) -> DetectionResult:
    base: dict = {
        "since": ONSET - timedelta(days=3),
        "until": ONSET + timedelta(days=3),
        "config": AnomalyConfig(),
        "findings": (make_finding(),),
        "suppressed": (make_suppressed(type=FINDING_IRREGULAR, onset=ONSET + timedelta(days=2),
                                       offset=ONSET + timedelta(days=2, minutes=20)),),
    }
    base.update(overrides)
    return DetectionResult(**base)


class ScorerTest(unittest.TestCase):
    """The scorer shown wrong answers, because a gate that cannot fail is not a gate."""

    def setUp(self) -> None:
        self.suite = hand_suite()
        self.wearer = self.suite.corpora[0].wearer

    def score(self, result: DetectionResult):
        return score_suite(self.suite, {self.wearer: result})

    def test_a_clean_run_reports_every_label_the_way_it_should(self) -> None:
        report = self.score(hand_result())
        statuses = {outcome.rule: outcome.status for outcome in report.outcomes}
        self.assertEqual(statuses[FINDING_SUSTAINED_HIGH], STATUS_REPORTED)
        self.assertEqual(statuses[FINDING_SUSTAINED_LOW], STATUS_MISSED)
        self.assertEqual(statuses[FINDING_IRREGULAR], STATUS_ACCOUNTED)
        self.assertEqual(report.precision, 1.0)
        self.assertEqual(report.recall, 0.5)

    def test_a_miss_and_a_false_alarm_move_the_numbers_the_way_they_claim_to(self) -> None:
        report = self.score(hand_result(findings=(make_finding(type=FINDING_IRREGULAR, observed=0.4),)))
        self.assertEqual(report.recall, 0.0, "the only alert label was the one missed")
        self.assertEqual(report.false_positives_count, 1)
        self.assertLess(report.precision, 1.0)
        self.assertFalse(report.best_is_true)

    def test_silence_about_a_held_label_is_itself_a_failure(self) -> None:
        report = self.score(hand_result(suppressed=()))
        self.assertEqual(report.held_shortfall, 1)
        self.assertTrue(SHIPPED_FLOORS.failures(report), "the floors must notice an unaccounted label")

    def test_reporting_a_held_label_counts_against_the_run(self) -> None:
        report = self.score(
            hand_result(
                findings=(
                    make_finding(),
                    make_finding(
                        type=FINDING_IRREGULAR,
                        onset=ONSET + timedelta(days=2),
                        offset=ONSET + timedelta(days=2, minutes=20),
                        observed=0.4,
                        threshold=0.2,
                        unit="irregularity index (dimensionless)",
                        mean=0.3,
                    ),
                )
            )
        )
        held = [outcome for outcome in report.outcomes if outcome.verdict == VERDICT_HELD]
        self.assertEqual([outcome.status for outcome in held], [STATUS_REPORTED_ANYWAY])
        self.assertEqual(report.false_positives_count, 1)

    def test_a_finding_inside_a_clean_window_is_a_violation(self) -> None:
        window = self.suite.corpora[0].windows[0]
        report = self.score(
            hand_result(
                findings=(
                    make_finding(),
                    make_finding(
                        type=FINDING_SUSTAINED_LOW,
                        onset=window.onset + timedelta(minutes=30),
                        offset=window.onset + timedelta(minutes=70),
                    ),
                )
            )
        )
        self.assertEqual(report.clean_window_rate_false_positives, 1)
        self.assertEqual(
            [outcome.status for outcome in report.outcomes if outcome.kind == "window"],
            [STATUS_VIOLATED],
        )
        self.assertTrue(SHIPPED_FLOORS.failures(report))

    def test_a_second_finding_for_one_episode_is_a_repeat_not_a_false_alarm(self) -> None:
        report = self.score(hand_result(findings=(make_finding(), make_finding(confidence=0.6))))
        self.assertEqual(len(report.repeats), 1)
        self.assertEqual(report.false_positives_count, 0)
        self.assertEqual(report.precision, 1.0)

    def test_rank_confidences_and_the_edges_that_come_with_it(self) -> None:
        high = make_finding(confidence=0.9)
        low = make_finding(confidence=0.2)
        self.assertEqual(rank_confidences((high,), (low,)), 1.0)
        self.assertEqual(rank_confidences((low,), (high,)), 0.0)
        self.assertEqual(rank_confidences((high,), (high,)), 0.5, "a tie is half a win")
        self.assertEqual(rank_confidences((), (low,)), 1.0, "nothing to mis-rank")

    def test_labels_reject_the_claims_they_cannot_make(self) -> None:
        with self.assertRaises(Exception):
            EpisodeLabel("w", "nonsense", ONSET, ONSET + timedelta(minutes=5))
        with self.assertRaises(Exception):
            EpisodeLabel(
                "w", FINDING_IRREGULAR, ONSET + timedelta(minutes=5), ONSET, verdict=VERDICT_ALERT
            )
        with self.assertRaises(Exception):
            WindowLabel("w", ONSET, ONSET + timedelta(minutes=5), verdict=VERDICT_ALERT)
        compact = WindowLabel.from_row("w", f"{ONSET.isoformat()}..{(ONSET + timedelta(hours=6)).isoformat()}")
        self.assertEqual(compact.onset, ONSET)
        self.assertEqual(compact.rules, (FINDING_SUSTAINED_HIGH, FINDING_SUSTAINED_LOW))
        with self.assertRaises(Exception):
            WindowLabel.from_row("w", "not-a-window")

    def test_a_report_prints_a_matrix_and_the_floors_under_it(self) -> None:
        text = format_report(self.score(hand_result()), AccuracyFloors())
        self.assertIn("confusion matrix", text)
        self.assertIn("expected", text)
        self.assertIn("clean night windows", text)
        self.assertIn("ok", text)

    def test_rule_counts_are_the_ratios_they_claim_to_be(self) -> None:
        counts = RuleCounts(alert_labels=4, reported_labels=3, true_positive_findings=9, false_positive_findings=1)
        self.assertEqual(counts.recall, 0.75)
        self.assertEqual(counts.precision, 0.9)
        self.assertIsNone(RuleCounts().recall, "a rule with no alert labels has no recall to claim")
        self.assertIsNone(RuleCounts().precision)


class FixtureFormatTest(unittest.TestCase):
    """The format is the artifact, so the format gets tested: it round-trips, and it refuses
    what it cannot score.

    `LabelledCorpus.build_store` has a second kind - somebody's own store, opened rather
    than generated - because the whole point of a format is that a user can add their exported
    episodes later without touching the scorer. It is exercised here against a real store even
    though the shipped fixture does not use it.
    """

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_the_suite_round_trips_through_its_own_file(self) -> None:
        original = suite()
        path = original.save(self.root / "round.json")
        again = LabelledSuite.load(path)
        self.assertEqual(again.as_dict(), original.as_dict())
        self.assertEqual(again.tolerance, original.tolerance)
        self.assertEqual(len(again.windows), len(original.windows))
        # ...and a compact row is what a clean night is written as, so the file stays readable.
        first = original.corpora[0].windows[0]
        self.assertIsInstance(first.as_row(), str)
        self.assertEqual(WindowLabel.from_row(first.wearer, first.as_row()).onset, first.onset)
        rich = WindowLabel(
            "w", ONSET, ONSET + timedelta(hours=1), rules=(FINDING_IRREGULAR,), detail={"why": "test"}
        )
        self.assertIsInstance(rich.as_row(), dict)
        self.assertEqual(WindowLabel.from_row("w", rich.as_dict()).rules, (FINDING_IRREGULAR,))
        self.assertGreater(first.duration_minutes, 0)

    def test_a_wearer_can_be_a_store_somebody_else_produced(self) -> None:
        from fitbit_monitor.store.writer import SeriesStore
        from fitbit_monitor.store.seed import seed_store

        path = self.root / "their-store.sqlite3"
        writer = SeriesStore(path)
        try:
            seed_store(writer, days=3, seed=5, end=datetime(2026, 5, 31).date())
        finally:
            writer.close()
        corpus = LabelledCorpus(
            wearer="theirs", source={"kind": "store", "store_path": str(path)}, episodes=(), windows=()
        )
        built = corpus.build_store(self.root)
        self.addCleanup(built.close)
        self.assertIsNone(built.plan, "nobody generated this corpus, so there is no plan to re-derive from")
        self.assertFalse(built.corpus.is_synthetic)
        with self.assertRaises(Exception):
            built.rederived_labels()
        with self.assertRaises(Exception):
            LabelledCorpus(wearer="x", source={"kind": "telepathy"}).build_store(self.root)

    def test_the_format_refuses_what_it_cannot_score(self) -> None:
        with self.assertRaises(Exception):
            LabelledSuite(corpora=())
        with self.assertRaises(Exception):
            LabelledSuite(corpora=(hand_suite().corpora[0], hand_suite().corpora[0]))
        with self.assertRaises(Exception):
            LabelledSuite.from_mapping({"format": 99, "corpora": []})
        with self.assertRaises(Exception):
            score_suite(hand_suite(), {})

    def test_a_held_label_reported_anyway_shows_up_in_the_rule_counts(self) -> None:
        report = score_suite(
            hand_suite(),
            {
                "handmade": hand_result(
                    findings=(
                        make_finding(),
                        make_finding(
                            type=FINDING_IRREGULAR,
                            onset=ONSET + timedelta(days=2),
                            offset=ONSET + timedelta(days=2, minutes=20),
                        ),
                    )
                )
            },
        )
        counts = report.counts_by_rule()[FINDING_IRREGULAR]
        self.assertEqual((counts.held_labels, counts.held_violations, counts.held_shortfall), (1, 1, 1))
        self.assertIn("irregular_rhythm", report.confusion_matrix())
        self.assertLess(report.counts_by_rule()[FINDING_IRREGULAR].precision, 1.0)
