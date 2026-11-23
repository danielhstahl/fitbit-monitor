"""The built-in ``hr-anomaly`` plugin: the reference example for *alerts*.

The first class below is the shared contract suite - the same one third-party authors
inherit - so the documented example cannot drift from what the host enforces. The rest
of the file tests the promises only this plugin makes, and each one is an acceptance
criterion:

* thresholds are config: the option list is the engine's own, and changing a value in
  ``[plugins.hr-anomaly]`` changes what the run finds, refuses and says;
* every alert frames itself as a non-diagnostic heuristic over a consumer wearable,
  names its evidence (window, rate, confidence) and prints a command that inspects the
  raw readings;
* suppression is never silent: episodes the engine held back, episodes this plugin held
  back, and rules that never ran all reach ``--json``;
* thin coverage means no alerts *and* an explanation, not a quiet run;
* the alerts go out through the standard sinks, and a re-run does not say it twice.
"""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from fitbit_monitor.anomaly import (
    FINDING_IRREGULAR,
    FINDING_SUSTAINED_HIGH,
    FINDING_SUSTAINED_LOW,
    AnomalyConfig,
    Finding,
)
from fitbit_monitor.config import AppConfig
from fitbit_monitor.plugins.builtin.hr_anomaly import (
    ENGINE_OPTIONS,
    NON_DIAGNOSTIC,
    NON_DIAGNOSTIC_CODE,
    POLICY_OPTIONS,
    HeldBack,
    HrAnomalyPlugin,
    finding_summary,
    thin_window_reason,
)
from fitbit_monitor.plugins.models import Alert, Severity
from fitbit_monitor.plugins.registry import PluginRecord, PluginRegistry
from fitbit_monitor.plugins.runner import run_plugins
from fitbit_monitor.plugins.sinks import AlertDispatcher, build_dispatcher
from fitbit_monitor.plugins.testing import (
    PluginContractTestCase,
    fixture_store,
    fixture_window,
    make_config,
    run_fixture_plugin,
)
from fitbit_monitor.store.alerts import AlertHistory
from fitbit_monitor.store.reader import Coverage, ReadOnlyStore
from fitbit_monitor.store.writer import SeriesStore
from fitbit_monitor.timeutil import DateRange

from anomaly_support import HIGH_EPISODE, LOW_EPISODE, START, scenario_points, span_of

#: Words that would turn a description of a signal into a claim about a condition. The
#: engine bans them from findings; the plugin bans them from *alerts*, where the temptation
#: to name a disease is much harder to resist - and says, in their place, what it is not.
BANNED_CLAIMS = (
    "arrhythmia",
    "atrial",
    "fibrillat",
    "afib",
    "tachycardi",
    "bradycardi",
    "patholog",
    "you have",
    "you are having",
    "see a doctor",
    "call 911",
)

ONSET = datetime(2026, 5, 9, 1, 35, tzinfo=UTC)


# --- stores and scenarios ----------------------------------------------------
def store_from_points(directory: str, points) -> Path:
    """A real store holding exactly ``points``; returns its path."""
    path = Path(directory) / "store.sqlite3"
    with SeriesStore(path) as store:
        for stamp, value in points:
            store.put("heart_rate", stamp, {"hr": float(value)})
    return path


def window_for(points) -> DateRange:
    since, until = span_of(points)
    return DateRange(since=since, until=until)


def make_finding(**overrides) -> Finding:
    """One finding, written out longhand so the prose tests assert what they say they do."""
    base: dict = {
        "type": FINDING_SUSTAINED_HIGH,
        "context": "rest",
        "onset": ONSET,
        "offset": ONSET + timedelta(minutes=45),
        "confidence": 0.72,
        "heuristic": "personal-baseline-median-plus-mad",
        "observed": 118.0,
        "threshold": 95.0,
        "unit": "beats per minute",
        "baseline_level": 75.0,
        "baseline_spread": 5.0,
        "baseline_days": 21,
        "baseline_established": True,
        "samples": 10,
        "coverage": 1.0,
        "mean": 110.0,
        "values": (108.0, 112.0, 118.0),
        "anchor": "2026-05-09",
    }
    base.update(overrides)
    return Finding(**base)


def configured(**values) -> HrAnomalyPlugin:
    """A plugin configured exactly the way the host configures one."""
    config = make_config(HrAnomalyPlugin.name, values).plugin_config(
        HrAnomalyPlugin.name, HrAnomalyPlugin.declared_defaults()
    )
    plugin = HrAnomalyPlugin()
    plugin.configure(config)
    return plugin


def metrics_by_key(outcome) -> dict:
    out: dict = {}
    for metric in outcome.metrics:
        out.setdefault(metric.key, []).append(metric)
    return out


def detail_of(outcome, key: str) -> dict:
    return metrics_by_key(outcome)[key][0].details


class StubContext:
    """Just enough of a :class:`PluginContext` for ``emit_alert``: it only calls ``alert``."""

    def __init__(self) -> None:
        self.alerts: list[Alert] = []

    def alert(self, key: str, title: str, message: str, **kwargs) -> Alert:
        alert = Alert(plugin=HrAnomalyPlugin.name, key=key, title=title, message=message, **kwargs)
        self.alerts.append(alert)
        return alert


# --- the shared contract suite ----------------------------------------------
class HrAnomalyContractTest(PluginContractTestCase):
    """The suite every plugin is held to, against the plugin the docs hold up as the example."""

    plugin_cls = HrAnomalyPlugin

    # --- what an alert says -------------------------------------------------
    def test_every_alert_frames_itself_and_names_its_evidence(self) -> None:
        outcome = self.run_fixture_plugin()
        self.assertTrue(outcome.alerts, "the reference example must have something to show")
        for alert in outcome.alerts:
            message = alert.message
            self.assertIn("not a diagnosis", message)
            self.assertIn("medical advice", message)
            self.assertIn("wrist-worn consumer sensor", message)
            # evidence: the window, the readings, the confidence...
            self.assertIn(alert.occurred_at.strftime("%Y-%m-%d %H:%M"), message)
            self.assertIn(f"{alert.window_end.strftime('%H:%M')} UTC", message)
            self.assertIn("Confidence", message)
            self.assertIn("readings", message)
            # ...and how to look at the raw data yourself.
            self.assertIn("fm store export --dataset heart_rate --since", message)
            self.assertIn("fm alerts list --plugin hr-anomaly", message)
            self.assertEqual(alert.details["history_command"], "fm alerts list --plugin hr-anomaly")
            self.assertEqual(alert.details["framing"], NON_DIAGNOSTIC)
            self.assertIs(alert.details["non_diagnostic"], True)
            self.assertEqual(alert.labels["framing"], NON_DIAGNOSTIC_CODE)
            self.assertEqual(alert.labels["device_class"], "consumer_wrist_wearable")

    def test_no_alert_claims_a_condition(self) -> None:
        outcome = self.run_fixture_plugin()
        self.assertTrue(outcome.alerts)
        for alert in outcome.alerts:
            blob = f"{alert.title} {alert.message}".lower()
            for word in BANNED_CLAIMS:
                self.assertNotIn(word, blob, f"{alert.key}: {word!r} is a claim, not a measurement")

    def test_the_message_quotes_the_numbers_the_finding_carried(self) -> None:
        outcome = self.run_fixture_plugin()
        for alert in outcome.alerts:
            details = alert.details
            self.assertIn(f"{details['duration_minutes']:.0f} min", alert.message)
            self.assertIn(f"{alert.confidence:.2f}", alert.message)
            self.assertIn(f"{details['samples']} readings", alert.message)
            # The rate-ish numbers are printed at the precision their unit deserves -
            # one decimal for bpm, two for a dimensionless index - so compare numerically.
            quoted = {float(text) for text in re.findall(r"\d+\.\d+", alert.message)}
            index = "index" in details["unit"]
            places = 2 if index else 1
            fields = ["threshold", "baseline_level", "observed"]
            if not index:
                fields.append("mean")
            for field in fields:
                tolerance = 0.5 / 10**places
                self.assertTrue(
                    any(abs(value - details[field]) <= tolerance for value in quoted),
                    f"{field}={details[field]} is not quoted in {alert.message!r}",
                )
            self.assertIsNotNone(details.get("confidence_factors"), "the factors are auditable")
            self.assertNotIn(
                "confidence", details, "the probability is Alert.confidence, not a details field"
            )

    def test_dedup_key_is_the_finding_key_so_the_sinks_can_cooldown(self) -> None:
        outcome = self.run_fixture_plugin()
        self.assertTrue(outcome.alerts)
        for alert in outcome.alerts:
            self.assertTrue(alert.dedup_key.startswith(f"{alert.key}/"))
            self.assertEqual(alert.effective_dedup_key, alert.dedup_key)
            self.assertIsNotNone(alert.window_start)
            self.assertIsNotNone(alert.window_end)
            self.assertGreaterEqual(len(alert.evidence), 1)

    # --- thresholds are config ---------------------------------------------
    def test_engine_options_are_the_engine_own_list(self) -> None:
        """The advertised option surface *is* ``AnomalyConfig``, so it cannot drift."""
        specs = AnomalyConfig.option_specs()
        self.assertEqual([o.name for o in ENGINE_OPTIONS], [name for name, _k, _d, _h in specs])
        self.assertEqual([o.default for o in ENGINE_OPTIONS], [d for _n, _k, d, _h in specs])
        # What a fresh install advertises must be a configuration the engine accepts.
        AnomalyConfig.from_mapping({o.name: o.default for o in ENGINE_OPTIONS})
        for option in list(ENGINE_OPTIONS) + list(POLICY_OPTIONS):
            self.assertTrue(option.help, f"{option.name} advertises no help text")

    def test_a_threshold_change_changes_what_is_found(self) -> None:
        baseline = self.run_fixture_plugin()
        self.assertTrue(baseline.alerts, "the fixture must contain something to find")
        strict = self.run_fixture_plugin(
            config_values={"min_sustained_minutes": 6000, "irregularity_min_minutes": 6000}
        )
        self.assertEqual(strict.alerts, [], "a 100-hour duration floor should find nothing")
        self.assertEqual(detail_of(strict, "hr_anomaly_findings")["by_type"][FINDING_SUSTAINED_HIGH], 0)
        self.assertTrue(
            detail_of(strict, "hr_anomaly_suppressed")["episodes"],
            "the episodes the longer floor dropped are still reported",
        )

    def test_a_short_history_is_provisional_and_caps_what_it_claims(self) -> None:
        """Five days of data cannot say "unusual for you" loudly - capped, and informational."""
        points = scenario_points(5, jitter=1.5, episodes=[(2, (10.0, 11.0, HIGH_EPISODE))])
        with tempfile.TemporaryDirectory() as tmp:
            path = store_from_points(tmp, points)
            outcome = run_fixture_plugin(HrAnomalyPlugin, store_path=path, window=window_for(points))
        states = {m.labels["state"] for m in metrics_by_key(outcome)["hr_anomaly_baseline"]}
        self.assertIn("provisional", states)
        self.assertTrue(outcome.alerts, "a provisional baseline still reports, more quietly")
        for alert in outcome.alerts:
            self.assertEqual(alert.severity, Severity.INFO)
            self.assertEqual(alert.labels["baseline"], "provisional")
            self.assertLessEqual(alert.confidence, AnomalyConfig().provisional_confidence_cap)
            self.assertIn("usable day(s) so far", alert.message)

    def test_severity_comes_from_confidence_and_baseline_maturity(self) -> None:
        outcome = self.run_fixture_plugin()
        self.assertIn("critical", {a.severity.value for a in outcome.alerts})
        quiet = self.run_fixture_plugin(
            config_values={"warning_confidence": 0.99, "critical_confidence": 1.0}
        )
        self.assertTrue(quiet.alerts)
        self.assertEqual({a.severity.value for a in quiet.alerts}, {"info"})

    def test_min_confidence_holds_weak_findings_back_and_says_so(self) -> None:
        outcome = self.run_fixture_plugin(config_values={"min_confidence": 0.99})
        self.assertEqual(outcome.alerts, [])
        details = detail_of(outcome, "hr_anomaly_suppressed")
        self.assertGreaterEqual(details["by_code"].get("low_confidence", 0), 1)
        self.assertIn("low_confidence", {episode["code"] for episode in details["episodes"]})

    def test_max_alerts_keeps_the_most_significant_and_reports_the_rest(self) -> None:
        full = self.run_fixture_plugin()
        self.assertGreater(len(full.alerts), 1, "the fixture should give the cap something to do")
        capped = self.run_fixture_plugin(config_values={"max_alerts": 1})
        self.assertEqual(len(capped.alerts), 1)
        self.assertEqual(
            capped.alerts[0].severity,
            max((a.severity for a in full.alerts), key=lambda s: s.rank),
            "the cap should keep the most significant episode, not the earliest",
        )
        self.assertGreaterEqual(
            detail_of(capped, "hr_anomaly_suppressed")["by_code"].get("over_run_cap", 0), 1
        )

    def test_impossible_thresholds_are_a_config_error_not_a_crash(self) -> None:
        outcome = self.run_fixture_plugin(config_values={"min_baseline_days": 90})
        self.assertEqual(outcome.status, "config_error")
        self.assertIn("baseline_window_days", outcome.error)

    def test_impossible_policy_is_a_config_error(self) -> None:
        outcome = self.run_fixture_plugin(
            config_values={"warning_confidence": 0.9, "critical_confidence": 0.2}
        )
        self.assertEqual(outcome.status, "config_error")
        self.assertIn("warning_confidence", outcome.error)

    # --- suppression is never silent ---------------------------------------
    def test_suppression_is_reported_with_who_decided_and_why(self) -> None:
        outcome = self.run_fixture_plugin()
        details = detail_of(outcome, "hr_anomaly_suppressed")
        self.assertEqual(details["by_source"], {"engine": details["listed"]})
        self.assertEqual(sum(details["by_code"].values()), details["listed"])
        for episode in details["episodes"]:
            self.assertIn(episode["decided_by"], ("engine", "hr-anomaly"))
            self.assertTrue(episode["reason"], "a suppression without a reason is just silence")
        self.assertTrue(
            details["rules_that_did_not_run"],
            "the fixture's exercise-attributed stretches should show up as a rule note",
        )
        # ...and all of it is in the machine-readable report the CLI prints.
        blob = json.dumps(outcome.as_dict(), sort_keys=True)
        self.assertIn("hr_anomaly_suppressed", blob)
        self.assertIn("rules_that_did_not_run", blob)

    def test_coverage_and_baselines_are_reported_as_metrics(self) -> None:
        outcome = self.run_fixture_plugin()
        coverage = metrics_by_key(outcome)["hr_anomaly_data_coverage"][0]
        self.assertGreater(coverage.value, 0.0)
        self.assertLessEqual(coverage.value, 1.0)
        self.assertEqual(coverage.confidence, coverage.value)
        self.assertIn("days_present", coverage.details)
        for baseline in metrics_by_key(outcome)["hr_anomaly_baseline"]:
            self.assertIn(baseline.labels["state"], ("established", "provisional", "refused"))
            self.assertIn("high_threshold_bpm", baseline.details)
        findings = metrics_by_key(outcome)["hr_anomaly_findings"][0]
        self.assertEqual(findings.value, len(findings.details["findings"]))
        self.assertTrue(any(item["alerted"] for item in findings.details["findings"]))

    def test_cooldown_collapses_two_episodes_of_the_same_kind(self) -> None:
        """Two nights of the same shape inside the cooldown are one alert - and the other is named."""
        points = scenario_points(
            12, jitter=1.5, episodes=[(5, (1.0, 2.0, LOW_EPISODE)), (6, (1.0, 2.0, LOW_EPISODE))]
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = store_from_points(tmp, points)
            window = window_for(points)
            spread = run_fixture_plugin(
                HrAnomalyPlugin, store_path=path, window=window, config_values={"cooldown": "0s"}
            )
            collapsed = run_fixture_plugin(
                HrAnomalyPlugin, store_path=path, window=window, config_values={"cooldown": "3d"}
            )
        lows = [a.dedup_key for a in spread.alerts if a.key == FINDING_SUSTAINED_LOW]
        self.assertGreaterEqual(len(lows), 2, f"two nights should be two findings: {lows}")
        self.assertLess(len(collapsed.alerts), len(spread.alerts))
        details = detail_of(collapsed, "hr_anomaly_suppressed")
        self.assertGreaterEqual(details["by_code"].get("cooldown", 0), 1)
        self.assertIn("alerts.cooldown", json.dumps(dict(details)))

    # --- thin data ---------------------------------------------------------
    def test_a_thin_window_alerts_on_nothing_and_says_why(self) -> None:
        sparse = scenario_points(2)
        with tempfile.TemporaryDirectory() as tmp:
            path = store_from_points(tmp, sparse)
            window = DateRange(since=START - timedelta(days=28), until=START + timedelta(days=2))
            outcome = run_fixture_plugin(HrAnomalyPlugin, store_path=path, window=window)
        self.assertEqual(outcome.status, "ok")
        self.assertEqual(outcome.alerts, [])
        declined = metrics_by_key(outcome)["hr_anomaly_declined"][0]
        self.assertIn("min_window_coverage", str(declined.value))
        self.assertIn("nothing was analysed", str(declined.value))
        self.assertTrue(detail_of(outcome, "hr_anomaly_data_coverage")["too_thin_to_alert"])
        self.assertEqual(detail_of(outcome, "hr_anomaly_suppressed")["by_code"]["thin_window"], 1)
        self.assertEqual(metrics_by_key(outcome)["hr_anomaly_findings"][0].value, 0)

    def test_a_wider_min_window_coverage_declines_a_sparse_but_real_window(self) -> None:
        outcome = self.run_fixture_plugin(config_values={"min_window_coverage": 0.99})
        self.assertEqual(outcome.alerts, [])
        self.assertIn("min_window_coverage", str(metrics_by_key(outcome)["hr_anomaly_declined"][0].value))

    def test_no_data_at_all_is_the_host_no_data_answer_not_a_crash(self) -> None:
        outcome = self.run_fixture_plugin(window=fixture_window(days=1, end=date(1999, 1, 1)))
        self.assertEqual(outcome.status, "no_data")


# --- the policy and the prose, without a store in the way --------------------
class HrAnomalyProseTest(unittest.TestCase):
    """The wording is the product here, so it gets asserted directly."""

    def setUp(self) -> None:
        self.plugin = configured()

    def test_rate_message_names_window_rate_threshold_and_confidence(self) -> None:
        finding = make_finding()
        message = self.plugin.message_for(finding)
        self.assertIn("2026-05-09 01:35 to 02:20 UTC while you were at rest", message)
        self.assertIn("45 min, 10 readings, 100% of that span actually recorded", message)
        self.assertIn("averaged 110.0 bpm and peaked 118.0 bpm", message)
        self.assertIn(
            "personal bar of 95.0 bpm built from your own usual level of 75.0 bpm", message
        )
        self.assertIn("Confidence 0.72", message)
        self.assertIn("not a probability that anything is wrong", message)
        self.assertIn(NON_DIAGNOSTIC, message)
        self.assertIn("fm store export --dataset heart_rate --since 2026-05-09T01:25:00+00:00", message)
        self.assertEqual(
            self.plugin.title_for(finding), "Heart rate held above your own usual level for 45 min"
        )

    def test_low_and_irregular_findings_are_worded_for_what_they_measure(self) -> None:
        low = make_finding(
            type=FINDING_SUSTAINED_LOW,
            context="sleep",
            observed=40.0,
            mean=43.0,
            threshold=50.0,
            baseline_level=58.0,
        )
        low_message = self.plugin.message_for(low)
        self.assertIn("sat lower than your own usual level", low_message)
        self.assertIn("dropped to 40.0 bpm", low_message)
        self.assertIn("while you were asleep", low_message)
        self.assertIn("below your own usual level", self.plugin.title_for(low))

        irregular = make_finding(
            type=FINDING_IRREGULAR,
            observed=0.28,
            mean=0.24,
            threshold=0.15,
            baseline_level=0.05,
            unit="irregularity index (dimensionless)",
        )
        message = self.plugin.message_for(irregular)
        self.assertIn("scatter index reached 0.28 against your usual 0.05", message)
        self.assertNotIn(" bpm ", message, "an index is not a rate; do not print it as one")
        self.assertIn("unusually uneven for 45 min", self.plugin.title_for(irregular))

    def test_a_provisional_finding_says_its_baseline_is_short(self) -> None:
        finding = make_finding(confidence=0.5, baseline_days=4, baseline_established=False)
        message = self.plugin.message_for(finding)
        self.assertIn("only 4 usable day(s) so far", message)
        self.assertIn("capped at 0.55", message)

    def test_severity_is_a_volume_knob_derived_from_the_engine_s_numbers(self) -> None:
        self.assertEqual(self.plugin.severity_for(make_finding(confidence=0.9)), Severity.CRITICAL)
        self.assertEqual(self.plugin.severity_for(make_finding(confidence=0.5)), Severity.WARNING)
        self.assertEqual(self.plugin.severity_for(make_finding(confidence=0.1)), Severity.INFO)
        self.assertEqual(
            self.plugin.severity_for(make_finding(confidence=0.99, baseline_established=False)),
            Severity.INFO,
            "a finding from a provisional baseline never shouts",
        )
        loud = configured(warning_confidence=0.3, critical_confidence=0.4)
        self.assertEqual(loud.severity_for(make_finding(confidence=0.5)), Severity.CRITICAL)

    def test_no_message_names_a_condition(self) -> None:
        for finding in (
            make_finding(),
            make_finding(type=FINDING_SUSTAINED_LOW, observed=40.0, threshold=50.0),
            make_finding(type=FINDING_IRREGULAR, observed=0.3, threshold=0.15, baseline_level=0.05),
        ):
            blob = f"{self.plugin.title_for(finding)} {self.plugin.message_for(finding)}".lower()
            for word in BANNED_CLAIMS:
                self.assertNotIn(word, blob)

    def test_inspect_command_covers_the_episode_plus_its_edges(self) -> None:
        finding = make_finding()
        self.assertIn("--since 2026-05-09T01:25:00+00:00", self.plugin.inspect_command(finding))
        self.assertIn("--until 2026-05-09T02:30:00+00:00", self.plugin.inspect_command(finding))
        wide = configured(inspect_padding="0s")
        self.assertIn("--since 2026-05-09T01:35:00+00:00", wide.inspect_command(finding))

    def test_configuration_reads_every_option_shape_the_host_can_hand_it(self) -> None:
        self.assertEqual(configured(cooldown="90m").cooldown, timedelta(minutes=90))
        self.assertEqual(configured(cooldown=7200).cooldown, timedelta(hours=2))
        self.assertEqual(configured(cooldown=timedelta(hours=5)).cooldown, timedelta(hours=5))
        self.assertEqual(configured(cooldown="0s").cooldown, timedelta(0))
        with self.assertRaises(Exception) as caught:
            configured(cooldown="soon")
        self.assertIn("soon", str(caught.exception))
        with self.assertRaises(Exception):
            configured(cooldown=True)
        with self.assertRaises(Exception):
            configured(cooldown=timedelta(hours=-1))
        for key, bad in (("min_confidence", 1.5), ("min_window_coverage", -0.2)):
            with self.subTest(key=key), self.assertRaises(Exception):
                configured(**{key: bad})
        with self.assertRaises(Exception):
            configured(max_alerts=0)

    def test_a_refused_baseline_is_a_reported_answer_not_a_missing_metric(self) -> None:
        """Two days of data cannot build a baseline: the run says so instead of inventing one."""
        points = scenario_points(2, jitter=1.5)
        with tempfile.TemporaryDirectory() as tmp:
            path = store_from_points(tmp, points)
            outcome = run_fixture_plugin(
                HrAnomalyPlugin,
                store_path=path,
                window=window_for(points),
                config_values={"min_window_coverage": 0.0},
            )
        self.assertEqual(outcome.status, "ok", outcome.error)
        self.assertEqual(outcome.alerts, [])
        baselines = metrics_by_key(outcome)["hr_anomaly_baseline"]
        self.assertTrue(baselines)
        for metric in baselines:
            self.assertEqual(metric.value, "no baseline")
            self.assertEqual(metric.labels["state"], "refused")
            self.assertIn("no baseline", metric.summary)
            self.assertIsNone(metric.confidence)

    def test_policy_is_reported_as_data_on_everything_it_touched(self) -> None:
        plugin = configured(max_alerts=3, min_confidence=0.4)
        self.assertEqual(plugin.policy()["max_alerts"], 3)
        self.assertEqual(plugin.policy()["cooldown"], "36h")
        context = StubContext()
        plugin.emit_alert(context, make_finding())
        alert = context.alerts[-1]
        self.assertEqual(alert.details["policy"]["min_confidence"], 0.4)
        self.assertEqual(alert.details["context_phase"], "while you were at rest")
        self.assertEqual(alert.labels["severity_source"], "confidence_and_baseline_maturity")
        self.assertEqual(alert.dedup_key, "sustained_high/rest/2026-05-09")

    def test_finding_summary_is_the_finding_without_the_arrays(self) -> None:
        finding = make_finding()
        summary = finding_summary(finding, alerted=True)
        self.assertNotIn("values", summary)
        self.assertNotIn("evidence", summary)
        self.assertTrue(summary["alerted"])
        self.assertEqual(summary["dedup_key"], finding.dedup_key)
        self.assertEqual(summary["confidence"], round(finding.confidence, 3))

    def test_thin_window_reason_says_what_was_missing(self) -> None:
        coverage = Coverage(
            dataset="heart_rate",
            since=ONSET,
            until=ONSET + timedelta(days=30),
            points=10,
            days_expected=30,
            days_present=3,
        )
        reason = thin_window_reason(coverage, 0.5)
        self.assertIn("3 of 30 days", reason)
        self.assertIn("min_window_coverage of 0.5", reason)
        self.assertNotIn("\n", reason, "a metric value is one line in the text report")


class HeldBackTest(unittest.TestCase):
    def test_as_dict_is_json_and_carries_the_reason(self) -> None:
        held = HeldBack.of(make_finding(), "cooldown", "only 3h after the alert this run raised")
        payload = held.as_dict()
        self.assertEqual(json.loads(json.dumps(payload))["code"], "cooldown")
        self.assertEqual(payload["type"], FINDING_SUSTAINED_HIGH)
        self.assertEqual(payload["duration_minutes"], 45.0)


# --- end to end: the standard sinks ------------------------------------------
class HrAnomalySinkTest(unittest.TestCase):
    """``fm plugins run`` delivers these alerts; this is the same path, asserted."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store_path = fixture_store(self.tmp.name)
        self.window = fixture_window()
        self.data_dir = Path(self.tmp.name) / "data"

    def dispatcher(self) -> AlertDispatcher:
        return build_dispatcher(
            AppConfig._from_raw(None, {"alerts": {"cooldown": "36h"}}),
            store_path=self.store_path,
            data_dir=self.data_dir,
            console=False,
            jsonl=True,
            history=True,
        )

    def run_once(self, dispatcher: AlertDispatcher | None):
        record = PluginRecord(
            name=HrAnomalyPlugin.name,
            kind="test",
            location=__name__,
            plugin_class=HrAnomalyPlugin,
        )
        store = ReadOnlyStore(self.store_path)
        self.addCleanup(store.close)
        return run_plugins(
            PluginRegistry(records=[record]),
            store,
            self.window,
            config=make_config(HrAnomalyPlugin.name, {}),
            names=[HrAnomalyPlugin.name],
            dispatcher=dispatcher,
            timeout=60.0,
        )

    def test_alerts_are_stored_delivered_and_then_deduped(self) -> None:
        dispatcher = self.dispatcher()
        self.addCleanup(lambda: dispatcher.history.close())
        outcome = self.run_once(dispatcher).outcomes[0]
        self.assertEqual(outcome.status, "ok", outcome.error)
        self.assertTrue(outcome.alerts)
        self.assertTrue(dispatcher.deliveries)
        self.assertTrue(all(d.is_new for d in dispatcher.deliveries))
        self.assertTrue(all(d.delivered_to == ("jsonl",) for d in dispatcher.deliveries))
        self.assertFalse(dispatcher.failures)

        history = AlertHistory.open(self.store_path)
        self.addCleanup(history.close)
        stored = history.list(plugin=HrAnomalyPlugin.name)
        self.assertEqual(len(stored), len(outcome.alerts))
        self.assertTrue(all(alert.dedup_key for alert in stored))
        written = (self.data_dir / "alerts.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(written), len(outcome.alerts))
        self.assertTrue(all("non_diagnostic" in line for line in written))

        # A second run over the same range is a replay: history keeps the row and the
        # sinks are not asked to say the same thing twice.
        second = self.dispatcher()
        self.addCleanup(lambda: second.history.close())
        again = self.run_once(second).outcomes[0]
        self.assertEqual(len(again.alerts), len(outcome.alerts))
        self.assertTrue(all(not d.is_new for d in again.deliveries))
        self.assertTrue(all(d.suppressed_by == "cooldown" for d in again.deliveries))
        self.assertEqual(len((self.data_dir / "alerts.jsonl").read_text().splitlines()), len(written))

    def test_a_run_that_delivers_nothing_stores_nothing(self) -> None:
        outcome = self.run_once(None).outcomes[0]
        self.assertTrue(outcome.alerts)
        self.assertEqual(outcome.deliveries, [])
        history = AlertHistory.open(self.store_path)
        self.addCleanup(history.close)
        self.assertEqual(history.list(plugin=HrAnomalyPlugin.name), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
