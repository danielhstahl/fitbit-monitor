"""Tests for the value models: MetricResult, Alert, Evidence, Severity.

These are the acceptance criteria for fitb-bk1.1 that live in the data types:
required fields, the severity enum, confidence bounded 0..1, timezone-aware
evidence in chronological order.
"""

from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime, timedelta

from fitbit_monitor.plugins.models import (
    Alert,
    Evidence,
    MetricResult,
    Severity,
    ValidationError,
)

T0 = datetime(2026, 5, 1, tzinfo=UTC)


def metric(**kw):
    base = dict(
        plugin="p",
        key="k",
        value=1.0,
        window_start=T0,
        window_end=T0 + timedelta(days=1),
    )
    base.update(kw)
    return MetricResult(**base)


def alert(**kw):
    base = dict(plugin="p", key="k", title="t", message="m", occurred_at=T0)
    base.update(kw)
    return Alert(**base)


class SeverityTest(unittest.TestCase):
    def test_parses_known_spellings(self) -> None:
        for text, expected in [
            ("info", Severity.INFO),
            ("WARN", Severity.WARNING),
            ("warning", Severity.WARNING),
            ("critical", Severity.CRITICAL),
            ("error", Severity.CRITICAL),
            (Severity.WARNING, Severity.WARNING),
        ]:
            self.assertEqual(Severity.parse(text), expected)

    def test_rejects_unknown_severity(self) -> None:
        with self.assertRaises(ValidationError):
            Severity.parse("apocalyptic")
        with self.assertRaises(ValidationError):
            Severity.parse(7)

    def test_ordering(self) -> None:
        self.assertTrue(Severity.CRITICAL.at_least("warning"))
        self.assertFalse(Severity.INFO.at_least("warning"))


class MetricResultTest(unittest.TestCase):
    def test_minimal_valid_metric_round_trips(self) -> None:
        m = metric()
        restored = MetricResult.from_dict(json.loads(m.as_json()))
        self.assertEqual(restored, m)

    def test_requires_plugin_key_and_value(self) -> None:
        for kw in ({"plugin": ""}, {"key": "  "}, {"value": None}, {"value": {"a": 1}}):
            with self.assertRaises(ValidationError, msg=str(kw)):
                metric(**kw)

    def test_value_accepts_int_float_and_string(self) -> None:
        for value in (3, 3.5, "present"):
            self.assertEqual(metric(value=value).value, value)

    def test_rejects_naive_window(self) -> None:
        with self.assertRaises(ValidationError):
            metric(window_start=datetime(2026, 5, 1))
        with self.assertRaises(ValidationError):
            metric(window_end=datetime(2026, 5, 2))

    def test_rejects_inverted_window(self) -> None:
        with self.assertRaises(ValidationError):
            metric(window_start=T0 + timedelta(days=2), window_end=T0)

    def test_confidence_must_be_within_bounds(self) -> None:
        for bad in (-0.01, 1.01, 12, "high", True):
            with self.assertRaises(ValidationError, msg=str(bad)):
                metric(confidence=bad)
        self.assertEqual(metric(confidence=0.0).confidence, 0.0)
        self.assertEqual(metric(confidence=1.0).confidence, 1.0)
        self.assertIsNone(metric(confidence=None).confidence)

    def test_labels_and_details_are_immutable(self) -> None:
        m = metric(labels={"a": "b"}, details={"x": 1})
        with self.assertRaises(TypeError):
            m.labels["a"] = "c"  # type: ignore[index]
        with self.assertRaises(TypeError):
            m.details["x"] = 2  # type: ignore[index]

    def test_is_frozen(self) -> None:
        m = metric()
        with self.assertRaises(Exception):
            m.value = 9  # type: ignore[misc]

    def test_window_in_non_utc_timezone_is_normalised(self) -> None:
        from datetime import timezone

        tz = timezone(timedelta(hours=-5))
        m = metric(window_start=datetime(2026, 5, 1, tzinfo=tz))
        self.assertEqual(m.window_start.tzinfo, UTC)


class AlertTest(unittest.TestCase):
    def test_defaults(self) -> None:
        a = alert()
        self.assertEqual(a.severity, Severity.WARNING)
        self.assertEqual(a.evidence, ())
        self.assertEqual(a.effective_dedup_key, "p/k/warning")
        self.assertEqual(a.occurrences, 1)

    def test_requires_text_fields(self) -> None:
        for kw in ({"title": ""}, {"message": " "}, {"plugin": ""}, {"key": ""}):
            with self.assertRaises(ValidationError, msg=str(kw)):
                alert(**kw)

    def test_occurred_at_must_be_aware(self) -> None:
        with self.assertRaises(ValidationError):
            alert(occurred_at=datetime(2026, 5, 1))

    def test_severity_accepts_string_and_enum(self) -> None:
        self.assertEqual(alert(severity="critical").severity, Severity.CRITICAL)
        self.assertEqual(alert(severity=Severity.INFO).severity, Severity.INFO)
        with self.assertRaises(ValidationError):
            alert(severity="extreme")

    def test_confidence_bounds(self) -> None:
        with self.assertRaises(ValidationError):
            alert(confidence=2)
        self.assertEqual(alert(confidence=0.5).confidence, 0.5)

    def test_evidence_must_be_timeordered(self) -> None:
        early, late = T0, T0 + timedelta(hours=3)
        with self.assertRaises(ValidationError) as ctx:
            alert(evidence=[Evidence(at=late, value=1), Evidence(at=early, value=2)])
        self.assertIn("chronological", str(ctx.exception))

    def test_evidence_accepts_pairs_and_mappings(self) -> None:
        a = alert(evidence=[(T0, 61.0), {"at": T0 + timedelta(minutes=5), "value": 63.0, "dataset": "heart_rate"}])
        self.assertEqual(len(a.evidence), 2)
        self.assertEqual(a.evidence[1].dataset, "heart_rate")

    def test_evidence_values_must_be_numbers(self) -> None:
        with self.assertRaises(ValidationError):
            Evidence(at=T0, value="high")  # type: ignore[arg-type]

    def test_window_order_enforced(self) -> None:
        with self.assertRaises(ValidationError):
            alert(window_start=T0 + timedelta(days=1), window_end=T0)

    def test_custom_dedup_key_wins(self) -> None:
        self.assertEqual(alert(dedup_key="night-2026-05-01").effective_dedup_key, "night-2026-05-01")

    def test_round_trip(self) -> None:
        a = alert(
            severity="critical",
            confidence=0.8,
            evidence=[Evidence(at=T0, value=140.0, dataset="heart_rate", detail="sustained")],
            labels={"device": " Versa"},
        )
        self.assertEqual(Alert.from_dict(json.loads(a.as_json())), a)

    def test_webhook_body_is_the_alert_only(self) -> None:
        a = alert(details={"internal": "secret"}, labels={"a": "b"})
        body = a.as_dict(full=False)
        self.assertNotIn("details", body)
        self.assertNotIn("labels", body)
        self.assertNotIn("alert_id", body)
        self.assertIn("title", body)

    def test_rendered_is_two_human_lines(self) -> None:
        text = alert(title="Resting HR up", message="4-day rise of 8 bpm").rendered()
        self.assertIn("WARNING", text)
        self.assertIn("Resting HR up", text)
        self.assertIn("4-day rise", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
