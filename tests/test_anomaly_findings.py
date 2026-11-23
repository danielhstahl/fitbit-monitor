"""The output contract: typed findings, their evidence, their dedup key, and no clinical claims.

The engine's promise to a plugin is mechanical - *this stretch of your own data is far from
your own ordinary values, this long, on these readings* - and the promise is only auditable if
the objects carrying it are strict. Everything here is asserted against a finding produced by
the real engine over a hand-built scenario, because a hand-worked :class:`Finding` would test
the constructor rather than the thing that matters.
"""

from __future__ import annotations

import json
from datetime import timedelta

from fitbit_monitor.anomaly import AnomalyConfig, detect
from fitbit_monitor.anomaly.errors import AnomalyValueError
from fitbit_monitor.anomaly.models import (
    FINDING_IRREGULAR,
    FINDING_SUSTAINED_HIGH,
    FINDING_SUSTAINED_LOW,
    FINDING_TYPES,
    Finding,
    sample_points,
)
from fitbit_monitor.plugins.models import Alert, Severity
from fitbit_monitor.plugins.sinks import AlertDispatcher
from fitbit_monitor.store.alerts import AlertHistory
from hr_support import HrStoreTestCase

from anomaly_support import HIGH_EPISODE, START, scenario_points, span_of

#: Words the library must never put in a finding. The engine measures; the *plugin* that owns
#: the user relationship decides what a measurement is called, and that is the whole reason
#: the wording is kept out of here: a claim in the library is a claim every downstream
#: consumer has to inherit, including ones whose job is only to move rows around.
BANNED_IN_FINDINGS = (
    "arrhythmia",
    "atrial",
    "fibrillat",
    "tachycardi",
    "bradycardi",
    "diagnos",
    "clinical",
    "medical",
    "doctor",
    "physician",
    "condition",
    "disease",
    "afib",
)


def _scenario(days: int = 5, **kwargs) -> list:
    return scenario_points(days, jitter=1.5, episodes=[(2, (10.0, 11.0, HIGH_EPISODE))], **kwargs)


class FindingShapeTestCase(HrStoreTestCase):
    """One finding, produced the way a plugin would produce it, examined field by field."""

    def setUp(self) -> None:
        super().setUp()
        self.points = _scenario()
        self.put(self.points)
        self.since, self.until = span_of(self.points)
        self.result = detect(self.reader(), self.since, self.until, config=AnomalyConfig())
        self.finding = next(
            finding for finding in self.result.findings if finding.type == FINDING_SUSTAINED_HIGH
        )

    def test_every_required_field_is_present_and_self_consistent(self) -> None:
        document = self.finding.as_dict()
        for field in (
            "type",
            "context",
            "onset",
            "offset",
            "duration_minutes",
            "confidence",
            "heuristic",
            "observed",
            "threshold",
            "unit",
            "mean",
            "samples",
            "coverage",
            "baseline",
            "anchor",
            "dedup_key",
            "evidence",
            "values",
        ):
            self.assertIn(field, document)
        self.assertEqual(document["type"], FINDING_SUSTAINED_HIGH)
        self.assertIn(document["type"], FINDING_TYPES)
        self.assertEqual(document["unit"], "bpm")
        self.assertAlmostEqual(document["duration_minutes"], 60.0, delta=1.0)
        self.assertTrue(0.0 < document["confidence"] <= AnomalyConfig().confidence_ceiling)
        json.dumps(document)

    def test_the_margin_is_measured_outwards_and_in_the_wearer_s_own_units(self) -> None:
        self.assertGreater(self.finding.margin, 0.0)
        self.assertGreater(self.finding.margin_sigma, 0.0)
        self.assertAlmostEqual(
            self.finding.margin_sigma,
            self.finding.margin / self.finding.baseline_spread,
            places=6,
        )

    def test_the_dedup_key_names_the_episode_and_survives_a_moved_window(self) -> None:
        key = self.finding.dedup_key
        self.assertEqual(
            key, f"sustained_high/rest/{(START + timedelta(days=2)).date().isoformat()}"
        )
        # A narrower window that still contains the episode must arrive with the same key, or
        # the sink's cooldown cannot recognise it as the episode it already told someone about.
        later_start = self.since + timedelta(hours=6)
        again = detect(self.reader(), later_start, self.until, config=AnomalyConfig())
        matches = [finding for finding in again.findings if finding.type == FINDING_SUSTAINED_HIGH]
        self.assertEqual(len(matches), 1, [finding.as_dict() for finding in again.findings])
        self.assertEqual(matches[0].dedup_key, key)

    def test_the_key_distinguishes_the_things_a_reader_would_count_separately(self) -> None:
        base = self.finding
        for change, expected_different in (
            ({"context": "sleep"}, True),
            ({"anchor": "2026-05-05"}, True),
            ({"type": FINDING_SUSTAINED_LOW}, True),
            ({"confidence": base.confidence + 0.01}, False),
            ({"observed": base.observed + 3.0}, False),
        ):
            clone = Finding(**{**_fields(base), **change})
            self.assertEqual(clone.dedup_key != base.dedup_key, expected_different, change)

    def test_confidence_is_a_probability_and_not_a_grade(self) -> None:
        for bad in (-0.1, 1.5):
            with self.assertRaises(AnomalyValueError):
                Finding(**{**_fields(self.finding), "confidence": bad})

    def test_an_episode_with_no_length_or_a_naive_clock_is_rejected(self) -> None:
        base = _fields(self.finding)
        with self.assertRaises(AnomalyValueError):
            Finding(**{**base, "offset": base["onset"]})
        with self.assertRaises(AnomalyValueError):
            Finding(**{**base, "onset": base["onset"].replace(tzinfo=None)})
        with self.assertRaises(AnomalyValueError):
            Finding(**{**base, "type": "probably_fine"})

    def test_no_clinical_claim_appears_anywhere_in_the_finding(self) -> None:
        blob = json.dumps(self.finding.as_dict()).lower()
        for word in BANNED_IN_FINDINGS:
            self.assertNotIn(word, blob, f"{word!r} is a claim about a person, not a measurement")

    def test_the_evidence_series_is_the_sampled_form_of_the_full_series(self) -> None:
        self.assertLessEqual(len(self.finding.evidence), AnomalyConfig().max_evidence_points)
        self.assertGreater(len(self.finding.values), len(self.finding.evidence))
        stamps = [item.at for item in self.finding.evidence]
        self.assertEqual(stamps, sorted(stamps))
        # The sampled form keeps its ends and its extreme: a reader who only ever sees the
        # sampled series must still be able to see when it started and how far it went.
        self.assertEqual(stamps[0], self.finding.onset)
        self.assertEqual(stamps[-1], max(item.at for item in self.finding.evidence))
        self.assertIn(
            self.finding.observed, [round(item.value, 3) for item in self.finding.evidence]
        )

    def test_sampling_is_what_the_evidence_series_is_made_of(self) -> None:
        # The helper is asserted on its own too, because the evidence series a sink carries is
        # this function's output and the contract is "edges and the extreme, in order".
        points = [(START + timedelta(minutes=index), 60.0 + index) for index in range(5)]
        sampled = sample_points(points, limit=12)
        self.assertEqual([item.at for item in sampled], [stamp for stamp, _value in points])
        self.assertEqual(sample_points([], limit=12), ())
        thin = sample_points(points, limit=3)
        self.assertEqual(len(thin), 3)
        self.assertEqual(thin[0].at, points[0][0])
        self.assertEqual(thin[-1].at, points[-1][0])
        peak = sample_points(points, limit=3, keep="max", note="peak")
        self.assertIn("peak", [item.detail for item in peak])
        self.assertEqual(max(item.value for item in peak), points[-1][1])


class ResultDocumentTestCase(HrStoreTestCase):
    """The whole run as a document a plugin can print without asking the engine anything."""

    def setUp(self) -> None:
        super().setUp()
        # Two days: under `provisional_min_days`, so every rule has to say why it did not run.
        self.points = scenario_points(2, jitter=1.5)
        self.put(self.points)
        self.since, self.until = span_of(self.points)
        self.result = detect(self.reader(), self.since, self.until, config=AnomalyConfig())

    def test_the_document_is_serialisable_and_complete(self) -> None:
        document = self.result.as_dict()
        json.dumps(document)
        for field in (
            "since",
            "until",
            "timezone",
            "days_scanned",
            "samples_scanned",
            "findings",
            "suppressed",
            "notes",
            "baselines",
            "scatter_baselines",
            "config",
        ):
            self.assertIn(field, document)
        # One count per finding type, so a dashboard does not have to know the type list.
        for finding_type in FINDING_TYPES:
            self.assertEqual(
                document[f"by_type:{finding_type}"], len(self.result.by_type(finding_type))
            )
        self.assertEqual(
            document["config"]["min_sustained_minutes"], AnomalyConfig().min_sustained_minutes
        )

    def test_too_little_history_is_a_reported_note_not_a_quiet_zero(self) -> None:
        self.assertEqual(self.result.findings, ())
        self.assertTrue(self.result.notes)
        for note in self.result.notes:
            self.assertIn(note.rule, FINDING_TYPES)
            self.assertIn("need", note.reason)
        json.dumps([note.as_dict() for note in self.result.notes])

    def test_lookups_and_counts_agree_with_the_tuples(self) -> None:
        self.assertEqual(self.result.by_type(FINDING_SUSTAINED_HIGH), ())
        self.assertEqual(self.result.by_context("rest"), ())
        self.assertIsNone(self.result.highest_confidence)
        counts = self.result.counts()
        self.assertEqual(counts["suppressed"], len(self.result.suppressed))
        self.assertEqual(counts["notes"], len(self.result.notes))


class AlertWiringTestCase(HrStoreTestCase):
    """A finding through :meth:`Finding.alert_args` into the real sinks, once and then twice."""

    def setUp(self) -> None:
        super().setUp()
        points = _scenario()
        self.put(points)
        since, until = span_of(points)
        self.result = detect(self.reader(), since, until, config=AnomalyConfig())
        self.finding = next(
            finding for finding in self.result.findings if finding.type == FINDING_SUSTAINED_HIGH
        )
        self.history = AlertHistory.open(self.path)
        self.emitted: list[str] = []

        class Counter:
            name = "counter"

            def __init__(self, owner):
                self.owner = owner

            def emit(self, alert):
                self.owner.emitted.append(alert.effective_dedup_key)
                return True

        self.dispatcher = AlertDispatcher(
            sinks=[Counter(self)], history=self.history, cooldown=timedelta(hours=36)
        )

    def tearDown(self) -> None:
        self.history.close()
        super().tearDown()

    def alert(self, **overrides) -> Alert:
        """What the hr-anomaly plugin does: technical fields from the engine, prose from itself."""
        args = {**self.finding.alert_args(), **overrides}
        return Alert(
            plugin="hr-anomaly",
            title="A sustained stretch away from your usual rate",
            message="One hour sat well above your own resting level. This is a pattern in your data, not a diagnosis.",
            severity=Severity.WARNING,
            **args,
        )

    def test_alert_args_supplies_every_technical_field_and_no_prose(self) -> None:
        args = self.finding.alert_args()
        self.assertEqual(args["dedup_key"], self.finding.dedup_key)
        self.assertEqual(args["occurred_at"], self.finding.onset)
        self.assertEqual(args["window_start"], self.finding.onset)
        self.assertEqual(args["window_end"], self.finding.offset)
        self.assertEqual(args["labels"]["finding_type"], FINDING_SUSTAINED_HIGH)
        self.assertEqual(args["labels"]["baseline"], "provisional")
        self.assertNotIn("severity", args)
        self.assertNotIn("title", args)
        self.assertNotIn("message", args)
        self.assertNotIn("plugin", args)
        blob = json.dumps(args, default=str).lower()
        for word in BANNED_IN_FINDINGS:
            self.assertNotIn(word, blob)

    def test_the_finding_fits_an_alert_without_the_plugin_inventing_anything(self) -> None:
        alert = self.alert()
        self.assertEqual(alert.effective_dedup_key, self.finding.dedup_key)
        self.assertEqual(alert.confidence, round(self.finding.confidence, 3))
        self.assertEqual(alert.window_end, self.finding.offset)
        self.assertEqual(alert.evidence, self.finding.evidence)
        self.assertEqual(alert.details["observed"], round(self.finding.observed, 2))
        alert.as_json()

    def test_the_same_episode_is_new_once_and_a_replay_is_not(self) -> None:
        first = self.dispatcher.dispatch(self.alert())
        self.assertTrue(first.is_new)
        self.assertEqual(self.emitted, [self.finding.dedup_key])
        # Re-running an overlapping range: the episode is already known and its clock has not
        # moved, so this is a replay - not delivered, and not counted as another occurrence.
        replay = self.dispatcher.dispatch(self.alert())
        self.assertFalse(replay.is_new)
        self.assertEqual(replay.suppressed_by, "cooldown")
        self.assertEqual(self.emitted, [self.finding.dedup_key])

    def test_a_different_episode_on_a_different_day_is_its_own_alert(self) -> None:
        self.dispatcher.dispatch(self.alert())
        points = scenario_points(12, jitter=1.5, episodes=[(9, (10.0, 11.0, HIGH_EPISODE))])
        self.put(points)
        since, until = span_of(points)
        result = detect(self.reader(), since, until, config=AnomalyConfig())
        new = [
            finding
            for finding in result.findings
            if finding.type == FINDING_SUSTAINED_HIGH
            and finding.anchor == (START + timedelta(days=9)).date().isoformat()
        ]
        self.assertEqual(len(new), 1)
        delivery = self.dispatcher.dispatch(self.alert(**new[0].alert_args()))
        self.assertTrue(delivery.is_new)
        self.assertEqual(len(self.emitted), 2)

    def test_the_irregular_finding_travels_the_same_path(self) -> None:
        from anomaly_support import irregular_pattern

        points = scenario_points(5, jitter=0.0, episodes=[(2, (1.0, 2.0, irregular_pattern()))])
        self.put(points)
        since, until = span_of(points)
        result = detect(self.reader(), since, until, config=AnomalyConfig())
        finding = next(item for item in result.findings if item.type == FINDING_IRREGULAR)
        alert = self.alert(**finding.alert_args())
        self.assertEqual(alert.key, FINDING_IRREGULAR)
        self.assertEqual(alert.details["unit"], finding.unit)
        self.assertIn("scatter", json.dumps(dict(alert.details), default=str))
        self.assertTrue(self.dispatcher.dispatch(alert).is_new)


def _fields(finding: Finding) -> dict:
    """A constructor-shaped copy of a finding, for testing what its constructor rejects."""
    return {
        "type": finding.type,
        "context": finding.context,
        "onset": finding.onset,
        "offset": finding.offset,
        "confidence": finding.confidence,
        "heuristic": finding.heuristic,
        "observed": finding.observed,
        "threshold": finding.threshold,
        "unit": finding.unit,
        "baseline_level": finding.baseline_level,
        "baseline_spread": finding.baseline_spread,
        "baseline_days": finding.baseline_days,
        "baseline_established": finding.baseline_established,
        "samples": finding.samples,
        "coverage": finding.coverage,
        "mean": finding.mean,
        "values": finding.values,
        "evidence": finding.evidence,
        "anchor": finding.anchor,
        "detail": finding.detail,
    }
